"""MCP protocol adaptation.

The only module in the package that imports ``mcp``. Its single job is binding
the capability functions in :mod:`.tools`, :mod:`.resources`, and
:mod:`.prompts` to a :class:`FastMCP` instance — no business logic, so the
capabilities stay testable without a protocol and this stays testable without a
Runtime.

FastMCP is the high-level API of the official SDK. It derives each tool's JSON
schema from the function signature and docstring, which is why the signatures
below are typed and documented rather than accompanied by hand-written schemas
that would drift from them.

    pip install workflow-builder[mcp]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflow_builder.mcp_server import prompts, resources, tools

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from workflow_builder.facade import RuntimeFacade
    from workflow_builder.identity.config import IdentitySettings

__all__ = ["build_server", "create_server"]


def _principal_facade(base_facade: RuntimeFacade, auth_enabled: bool) -> RuntimeFacade:
    """The facade one tool/resource/prompt call should use.

    Unwrapped when identity is not configured — no ``AuthorizedFacade``
    construction, no scope check, byte-identical to how every capability in
    ``mcp_server/tools.py`` behaved before this module existed. Wrapped
    per-call when it is, because the principal differs by request while
    ``base_facade`` is bound once, at server-build time, in the closures
    below — there is no other point at which a per-request value could
    reach a handler without widening every tool's own signature, which is
    exactly what ``AuthorizedFacade`` composition avoids doing to the port.
    """
    if not auth_enabled:
        return base_facade

    from mcp.server.auth.middleware.auth_context import get_access_token

    from workflow_builder.identity.facade import AuthorizedFacade
    from workflow_builder.identity.principal import ANONYMOUS, Principal

    token = get_access_token()
    principal = Principal.from_access_token(token) if token else ANONYMOUS
    return AuthorizedFacade(base_facade, principal)


def _scheduler_lifespan(facade: RuntimeFacade) -> Any:
    """Run the Runtime's timer loop for the lifetime of the server.

    Returns ``None`` when the facade has no in-process Runtime to drive — a
    ``RemoteFacade`` talks to a server that schedules its own.
    """
    runtime = getattr(facade, "runtime", None)
    if runtime is None:
        return None

    import contextlib
    from collections.abc import AsyncIterator

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        await runtime.start_scheduler()
        try:
            yield
        finally:
            await runtime.shutdown()

    return lifespan

INSTRUCTIONS = """\
LOOM runs durable workflows: every step is journaled, so a run survives crashes \
and resumes exactly where it stopped.

Three things to know before using these tools:

1. A run can end in `suspended`. That is not failure — it is parked on a human \
approval, an external event, or a timer, and costs nothing while it waits. The \
response tells you what it is waiting for and which tool answers it.
2. `get_run_journal` is the artifact to read when asked why a run behaved as it \
did. Each entry is a step that actually executed.
3. `retry` re-runs a failure from the failed step against current code; `replay` \
re-executes from the journal and repeats no side effects. They are not \
interchangeable.
"""


def build_server(
    facade: RuntimeFacade,
    *,
    name: str = "loom",
    host: str = "127.0.0.1",
    port: int = 8000,
    scheduler: bool = True,
    identity: IdentitySettings | None = None,
    transport: str = "stdio",
) -> FastMCP:
    """Bind a facade to a FastMCP server.

    Takes the facade rather than building one, so the caller decides whether
    this serves an in-process Runtime or a remote one — and so tests can pass a
    fake.

    ``host`` and ``port`` are only consulted by the networked transports; stdio
    ignores them. They are settable here rather than at ``run()`` because that
    is where FastMCP reads them from.

    ``scheduler`` runs the Runtime's timer loop for as long as the server is up.
    Without it a run that calls ``ctx.sleep()`` parks and never wakes, because
    nothing is scanning for due timers — the workflow looks broken when it is
    merely unattended. Off for a remote facade, whose server keeps its own.

    ``identity`` defaults to ``IdentitySettings()`` (read from ``LOOM_AUTH_*``
    env vars) when not passed. When it configures no verifier, every tool,
    resource, and prompt below runs against *facade* exactly as it always
    did — this parameter existing changes nothing for an install that never
    sets a ``LOOM_AUTH_*`` var. When it does, every capability runs against
    an ``AuthorizedFacade`` built fresh from that request's bearer token —
    *except* over ``transport="stdio"`` (the default), which stays
    env-credentialed no matter what ``identity`` says.

    That last exception is deliberate, not incidental: stdio carries no
    HTTP headers, so there is no bearer token for ``get_access_token()`` to
    ever return, and treating a missing token there as "anonymous" would
    turn every tool call into an ``InsufficientScope`` the moment a
    ``LOOM_AUTH_*`` var happens to be set in the environment — a machine
    also running an authenticated HTTP LOOM server, say — breaking the
    stdio connection Claude Desktop/Code depend on for no security benefit,
    since stdio was never reachable by anyone but the process that spawned
    it in the first place.
    """
    from mcp.server.fastmcp import FastMCP

    from workflow_builder.identity.config import IdentitySettings
    from workflow_builder.mcp_server.auth import build_mcp_auth
    from workflow_builder.toolsets.registry import register_available_toolsets

    register_available_toolsets()

    identity = identity if identity is not None else IdentitySettings()
    mcp_auth = build_mcp_auth(identity)
    auth_enabled = mcp_auth is not None and transport != "stdio"

    server = FastMCP(
        name,
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        lifespan=_scheduler_lifespan(facade) if scheduler else None,
        token_verifier=mcp_auth.token_verifier if mcp_auth else None,
        auth=mcp_auth.auth_settings if mcp_auth else None,
        transport_security=mcp_auth.transport_security if mcp_auth else None,
    )
    _register_tools(server, facade, auth_enabled)
    _register_resources(server, facade, auth_enabled)
    _register_prompts(server, facade, auth_enabled)
    return server


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _register_tools(server: FastMCP, base_facade: RuntimeFacade, auth_enabled: bool) -> None:
    """Expose the actions. Signatures are the schema, so keep them typed."""

    @server.tool()
    async def list_workflows() -> str:
        """List every workflow this server can run, with input schemas."""
        return await tools.list_workflows(_principal_facade(base_facade, auth_enabled))

    @server.tool()
    async def run_workflow(
        workflow: str, input_json: str = "null", idempotency_key: str | None = None
    ) -> str:
        """Start a workflow and wait for it to finish or park.

        Args:
            workflow: Name of the workflow, as given by list_workflows.
            input_json: The input, JSON-encoded. Use "null" for none.
            idempotency_key: Reuse to make a repeated call return the original
                run instead of starting a second one.
        """
        return await tools.run_workflow(
            _principal_facade(base_facade, auth_enabled),
            workflow,
            input_json,
            idempotency_key=idempotency_key,
        )

    @server.tool()
    async def get_run_status(run_id: str) -> str:
        """Get one run's status, input, output, and error.

        Args:
            run_id: The run identifier.
        """
        return await tools.get_run_status(_principal_facade(base_facade, auth_enabled), run_id)

    @server.tool()
    async def list_runs(
        workflow: str | None = None, status: str | None = None, limit: int = 20
    ) -> str:
        """List recent runs, newest first.

        Args:
            workflow: Only runs of this workflow.
            status: One of pending, running, suspended, completed, failed,
                cancelled.
            limit: Maximum rows to return.
        """
        return await tools.list_runs(
            _principal_facade(base_facade, auth_enabled), workflow, status, limit
        )

    @server.tool()
    async def get_run_journal(run_id: str, offset: int = 0) -> str:
        """Read the durable operations a run recorded, in order.

        Use this to explain why a run behaved as it did — each entry is a step
        that actually executed, with its status and attempt count.

        Args:
            run_id: The run identifier.
            offset: Skip this many entries; a capped response's next_offset
                names where to resume.
        """
        return await tools.get_run_journal(
            _principal_facade(base_facade, auth_enabled), run_id, offset
        )

    @server.tool()
    async def get_run_progress(run_id: str, offset: int = 0) -> str:
        """Read what a run has narrated while running.

        Use this for "what is it doing right now?" — the journal says what a run
        durably did, this says what it reported along the way. A step that takes
        minutes is one journal entry and can be many reports.

        Args:
            run_id: The run identifier.
            offset: Skip this many reports; pass back ``next_offset`` to poll.
        """
        return await tools.get_run_progress(
            _principal_facade(base_facade, auth_enabled), run_id, offset
        )

    @server.tool()
    async def approve_run(run_id: str, subject: str, approved: bool = True) -> str:
        """Answer a human approval that a suspended run is waiting on.

        Args:
            run_id: The run identifier.
            subject: The approval subject, as named in the run's next_action.
            approved: False to reject instead of approving.
        """
        return await tools.approve_run(
            _principal_facade(base_facade, auth_enabled), run_id, subject, approved
        )

    @server.tool()
    async def send_event(run_id: str, event: str, payload_json: str = "null") -> str:
        """Deliver an event to a run parked on it.

        Args:
            run_id: The run identifier.
            event: The event name the run is awaiting.
            payload_json: The payload, JSON-encoded.
        """
        return await tools.send_event(
            _principal_facade(base_facade, auth_enabled), run_id, event, payload_json
        )

    @server.tool()
    async def cancel_run(run_id: str) -> str:
        """Cancel a run. Already-finished runs are left alone.

        Args:
            run_id: The run identifier.
        """
        return await tools.cancel_run(_principal_facade(base_facade, auth_enabled), run_id)

    @server.tool()
    async def retry_run(run_id: str) -> str:
        """Re-run a failed execution from its failed step, against current code.

        Everything that already succeeded is reused. Use this after fixing the
        cause of a failure.

        Args:
            run_id: The run identifier.
        """
        return await tools.retry_run(_principal_facade(base_facade, auth_enabled), run_id)

    @server.tool()
    async def replay_run(run_id: str) -> str:
        """Re-execute a run from its journal, repeating no side effects.

        A rehearsal of what the orchestration did, not a re-run. Use this to
        understand a run, not to fix one.

        Args:
            run_id: The run identifier.
        """
        return await tools.replay_run(_principal_facade(base_facade, auth_enabled), run_id)

    @server.tool()
    async def search_toolsets(query: str) -> str:
        """Search registered toolsets (Jira, Gmail, Slack, ...) by keyword.

        Args:
            query: Keywords to search for, e.g. "jira" or "calendar".
        """
        return await tools.search_toolsets(query)

    @server.tool()
    async def show_toolset(toolset_id: str, group: str | None = None) -> str:
        """List the operations one toolset exposes, optionally filtered to a group.

        Args:
            toolset_id: A toolset id from search_toolsets, e.g. "jira".
            group: Only this operation group, e.g. "issues".
        """
        return await tools.show_toolset(toolset_id, group)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def _register_resources(server: FastMCP, base_facade: RuntimeFacade, auth_enabled: bool) -> None:
    """Expose the read-only documents a client can pull into context."""

    @server.resource("loom://workflows")
    async def workflows() -> str:
        """Every workflow this server can run."""
        return await resources.read_workflows(_principal_facade(base_facade, auth_enabled))

    @server.resource("loom://workflows/{name}")
    async def workflow(name: str) -> str:
        """One workflow's definition and input schema."""
        return await resources.read_workflow(_principal_facade(base_facade, auth_enabled), name)

    @server.resource("loom://runs/{run_id}")
    async def run(run_id: str) -> str:
        """One run's status, input, output, and error."""
        return await resources.read_run(_principal_facade(base_facade, auth_enabled), run_id)

    @server.resource("loom://runs/{run_id}/journal")
    async def run_journal(run_id: str) -> str:
        """The durable operations a run recorded."""
        return await resources.read_run_journal(
            _principal_facade(base_facade, auth_enabled), run_id
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _register_prompts(server: FastMCP, base_facade: RuntimeFacade, auth_enabled: bool) -> None:
    """Expose the reusable task templates."""

    @server.prompt()
    def create_workflow(description: str) -> str:
        """Draft a LOOM workflow from a plain-English description."""
        return prompts.build_create_workflow_prompt(description)

    @server.prompt()
    async def debug_run(run_id: str) -> str:
        """Diagnose a failed run from its status and journal."""
        facade = _principal_facade(base_facade, auth_enabled)
        status = await facade.get(run_id) or {"error": f"No run '{run_id}'."}
        journal = await facade.journal(run_id) if "error" not in status else []
        return prompts.build_debug_run_prompt(status, journal)

    @server.prompt()
    async def explain_workflow(workflow: str) -> str:
        """Explain what a workflow does, step by step."""
        facade = _principal_facade(base_facade, auth_enabled)
        match = next(
            (e for e in await facade.workflows() if e["name"] == workflow), None
        )
        return prompts.build_explain_workflow_prompt(workflow, match)

    @server.prompt()
    def optimize_workflow(workflow: str) -> str:
        """Suggest durability and performance improvements for a workflow."""
        return prompts.build_optimize_prompt(workflow)

    @server.prompt()
    def review_workflow(workflow_code: str) -> str:
        """Review workflow source for correctness and durability."""
        return prompts.build_review_prompt(workflow_code)


# ---------------------------------------------------------------------------
# Backwards-compatible entry point
# ---------------------------------------------------------------------------


def create_server(store_url: str = "memory://", name: str = "loom") -> Any:
    """Build a server over a bare Runtime with the given store.

    Kept for the original API. It exposes no workflows, because nothing has
    imported any — prefer :func:`build_server` with a facade from
    ``workflow_builder.cli.targets.resolve()``, which is what ``loom mcp`` does.
    """
    from workflow_builder.facade import LocalFacade
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.state.factory import from_url

    return build_server(LocalFacade(Runtime(store=from_url(store_url))), name=name)
