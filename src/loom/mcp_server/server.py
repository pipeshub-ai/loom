"""MCP protocol adaptation.

The only module in the package that imports ``mcp``. Its single job is binding
the capability functions in :mod:`.tools`, :mod:`.resources`, and
:mod:`.prompts` to a :class:`MCPServer` instance — no business logic, so the
capabilities stay testable without a protocol and this stays testable without a
Runtime.

``MCPServer`` is the high-level API of the official SDK. It derives each tool's
JSON schema from the function signature and docstring, which is why the
signatures below are typed and documented rather than accompanied by
hand-written schemas that would drift from them.

It was called ``FastMCP`` and lived in ``mcp.server.fastmcp`` through 1.x. The
2.0 release did not rename it in place — it removed that module outright — so
there is no version of the SDK that answers to both, and this module is the
only place in LOOM that has to know.

    pip install loomsdk[mcp]
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from loom.mcp_server import authoring, prompts, resources, tools
from loom.mcp_server.authoring import AuthoringGate
from loom.mcp_server.authoring_config import AuthoringConfig

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context, MCPServer

    from loom.facade import RuntimeFacade
    from loom.identity.config import IdentitySettings

__all__ = ["TransportOptions", "build_server", "create_server"]

_Handler = TypeVar("_Handler", bound=Callable[..., Any])


@dataclass(frozen=True)
class TransportOptions:
    """Where a networked transport binds, and how it is secured.

    Carried on the built server as ``server.loom_transport`` so :func:`serve`
    can hand it to ``MCPServer.run``. Frozen, because it describes a decision
    already made: the refusal to bind a non-loopback host without identity
    happens in :func:`serve` *before* the server is built, and a mutable copy
    here would be a second place that decision could be un-made.
    """

    host: str
    port: int
    transport_security: Any | None = None

    def run_kwargs(self) -> dict[str, Any]:
        """What ``run("sse")`` and ``run("streamable-http")`` accept.

        ``stdio`` takes none of it and is passed none of it — it opens no
        socket, which is exactly why it is exempt from the auth requirement.
        ``transport_security`` is omitted rather than passed as ``None`` so the
        SDK applies its own default (it derives one for loopback binds) instead
        of being told there is none.
        """
        options: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.transport_security is not None:
            options["transport_security"] = self.transport_security
        return options


def _registrar(register: Any) -> Callable[..., Callable[[_Handler], _Handler]]:
    """One of ``MCPServer``'s registration decorators, with the handler's type kept.

    ``mcp`` is an optional extra, and the type-check environment installs
    ``[dev]`` only — so there ``server.tool(...)`` is an expression of type
    ``Any``, and ``disallow_untyped_decorators`` erases the signature of every
    handler it wraps. Thirty-odd tools then report as untyped in exactly the
    environment that could not have checked them anyway. An inline
    ``type: ignore`` is the wrong shape for that: ``mcp`` ships ``py.typed``,
    so wherever the extra *is* installed the ignore is unused and strict mode
    fails on that instead. Restating the decorator's shape once is the only
    form that is right in both.
    """
    return cast("Callable[..., Callable[[_Handler], _Handler]]", register)


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

    from loom.identity.facade import AuthorizedFacade
    from loom.identity.principal import ANONYMOUS, Principal

    token = get_access_token()
    principal = Principal.from_access_token(token) if token else ANONYMOUS
    return AuthorizedFacade(base_facade, principal)


def _current_principal() -> Any:
    """The principal behind the request being served, for the authoring gate.

    Separate from :func:`_principal_facade` because the authoring tools are
    deliberately *not* facade-scoped — what toolsets exist and whether code
    compiles are server-wide facts, not per-run data. That reasoning held for
    the three that read a catalogue and did not for the three that execute
    caller-supplied Python, write to the filesystem, and reach third-party APIs
    with this server's credentials. Those need to know who is asking.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    from loom.identity.principal import ANONYMOUS, Principal

    token = get_access_token()
    return Principal.from_access_token(token) if token else ANONYMOUS


def _scheduler_lifespan(facade: RuntimeFacade) -> Any:
    """Run the Runtime's timer loop for the lifetime of the server.

    Also sweeps stored OAuth credentials, which matters more here than
    anywhere else: an MCP server stays up for days and may not touch a
    credential for most of that, so "renew on next use" would mean the next
    use is the one that discovers the token died overnight. The sweep runs
    once at startup — the "on restart" case — and then on a timer.

    Returns ``None`` when the facade has no in-process Runtime to drive — a
    ``RemoteFacade`` talks to a server that schedules its own.
    """
    runtime = getattr(facade, "runtime", None)
    if runtime is None:
        return None

    import contextlib
    from collections.abc import AsyncIterator

    from loom.connectors.refresh import service_for

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        await runtime.start_scheduler()
        credentials = service_for(runtime)
        if credentials is not None:
            # Registers itself with runtime.supervise(), so the shutdown below
            # stops it without this block having to remember to.
            await credentials.start()
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

AUTHORING_INSTRUCTIONS = """

4. You can also create new workflows, using your own model plus these tools:
   - search_toolsets / show_toolset / get_tool_contract / get_tool_docs to \
discover integrations and their typed schemas
   - call_read_operation to resolve entity names to ids (a project, a user) \
before generating code that depends on them
   - validate_workflow_code to check generated code against LOOM's rules \
without running it — compile, structure, determinism, imports, grants, \
coverage, entity resolution, lint, types
   - smoke_test_workflow to execute generated code once in a sandbox with \
mocked integrations — no real API calls — and then twice more to check it is \
deterministic
   - save_workflow to write the finished code to a file
   The loop: discover, write code, validate, fix, smoke, fix, save, then \
run_workflow. The create_workflow prompt walks through it in full.

5. Pass the user's own words as `spec=` to validate_workflow_code, every time. \
Two of its stages compare the code against the request rather than against the \
language, and they report themselves skipped without it. They catch the two \
defects that pass every other check: a fetch capped at 100 answering a spec \
that said "all", and a query that fuzzy-matches a word from the spec instead \
of resolving it to the id the system uses. A stage reported `skipped` has \
found nothing — that is not the same as passing.
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
    authoring: AuthoringConfig | None = None,
) -> MCPServer:
    """Bind a facade to an MCP server.

    Takes the facade rather than building one, so the caller decides whether
    this serves an in-process Runtime or a remote one — and so tests can pass a
    fake.

    ``host`` and ``port`` are only consulted by the networked transports; stdio
    ignores them. They are settable here rather than at ``run()`` because that
    is where the SDK reads them from.

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

    ``authoring`` defaults to ``AuthoringConfig.from_env()`` — seven extra
    tools on top of the eighteen run-management ones above. Six of them
    (``get_tool_contract``, ``get_tool_docs``, ``call_read_operation``,
    ``validate_workflow_code``, ``smoke_test_workflow``, ``save_workflow``)
    let a *client's* model generate and verify new workflow code using LOOM's
    toolchain, with no model key in this process. The seventh,
    ``author_workflow``, runs LOOM's own coding agent end to end and therefore
    does need one. Pass ``AuthoringConfig(enabled=False)`` (or set
    ``LOOM_MCP_AUTHORING=0``) for a server that exposes only the
    run-management surface.
    """
    from mcp.server.mcpserver import Context, MCPServer

    from loom.identity.config import IdentitySettings
    from loom.mcp_server.auth import build_mcp_auth
    from loom.toolsets.registry import register_available_toolsets

    # `author_workflow` declares `ctx: Context` so the SDK injects the request
    # context, which is what lets it elicit. Annotations here are strings
    # (`from __future__ import annotations`), and the SDK resolves them against
    # the *module* globals — so a name bound only in this function's scope does
    # not resolve. Published here rather than imported at module level because
    # `mcp` is an optional extra and importing this module must not require it.
    globals().setdefault("Context", Context)

    register_available_toolsets()

    identity = identity if identity is not None else IdentitySettings()
    mcp_auth = build_mcp_auth(identity)
    auth_enabled = mcp_auth is not None and transport != "stdio"
    authoring = authoring if authoring is not None else AuthoringConfig.from_env()

    instructions = INSTRUCTIONS + AUTHORING_INSTRUCTIONS if authoring.enabled else INSTRUCTIONS

    server = MCPServer(
        name,
        instructions=instructions,
        lifespan=_scheduler_lifespan(facade) if scheduler else None,
        # `VerifiedToken` is shaped like the SDK's `AccessToken` and is
        # deliberately not that class, so `loom.identity` imports no `mcp` at
        # all (see identity/verifier.py). The SDK only reads attributes off it
        # and never isinstance-checks it, so the two are interchangeable in
        # fact but nominally distinct — and this is the one module that imports
        # both, so this is where that has to be said. `cast` and not
        # `type: ignore`, for the reason `_registrar` above spells out: the
        # extra is optional, and an ignore is itself an error where `mcp` is
        # absent and mypy cannot see a conflict to ignore.
        token_verifier=cast("Any", mcp_auth.token_verifier) if mcp_auth else None,
        auth=mcp_auth.auth_settings if mcp_auth else None,
    )
    # `host`, `port` and `transport_security` were constructor arguments in
    # 1.x and are run-call arguments in 2.0 — the more honest place, since
    # where a server binds is a property of *serving* it, not of the tool
    # registry. They are recorded rather than dropped: `build_server` is public
    # API and `loom mcp --host` passes through it, so a signature that accepted
    # them and bound somewhere else would be the worst of the three options.
    cast("Any", server).loom_transport = TransportOptions(
        host=host,
        port=port,
        transport_security=mcp_auth.transport_security if mcp_auth else None,
    )
    _register_tools(server, facade, auth_enabled, authoring.enabled)
    _register_resources(server, facade, auth_enabled)
    _register_prompts(server, facade, auth_enabled, authoring.enabled)
    if authoring.enabled:
        _register_authoring_tools(
            server,
            authoring,
            gate=AuthoringGate(
                _current_principal if auth_enabled else None
            ),
        )
    return server


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _register_tools(
    server: MCPServer,
    base_facade: RuntimeFacade,
    auth_enabled: bool,
    authoring_enabled: bool = True,
) -> None:
    """Expose the actions. Signatures are the schema, so keep them typed."""
    from mcp.types import ToolAnnotations

    tool = _registrar(server.tool)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def list_workflows() -> str:
        """List every workflow this server can run, with input schemas."""
        return await tools.list_workflows(_principal_facade(base_facade, auth_enabled))

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def get_workflow_info(workflow: str) -> str:
        """One workflow's description, triggers, and full input schema.

        Use this before run_workflow when the input shape matters —
        list_workflows is capped and may not carry the one you want.

        Args:
            workflow: Name of the workflow.
        """
        return await tools.get_workflow_info(
            _principal_facade(base_facade, auth_enabled), workflow
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        )
    )
    async def schedule_workflow(
        workflow: str, cron: str, timezone: str = "UTC"
    ) -> str:
        """Fire a workflow on a cron expression, durably.

        The trigger is stored, so it survives this server restarting.

        Args:
            workflow: Name of the workflow.
            cron: Cron expression, e.g. "0 9 * * 1-5".
            timezone: IANA timezone the expression is read in.
        """
        return await tools.schedule_workflow(
            _principal_facade(base_facade, auth_enabled), workflow, cron, timezone
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )
    )
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

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def get_run_status(run_id: str) -> str:
        """Get one run's status, input, output, and error.

        Args:
            run_id: The run identifier.
        """
        return await tools.get_run_status(_principal_facade(base_facade, auth_enabled), run_id)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
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

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
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

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=False, openWorldHint=False
        )
    )
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

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
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

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )
    )
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

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def cancel_run(run_id: str) -> str:
        """Cancel a run. Already-finished runs are left alone.

        Args:
            run_id: The run identifier.
        """
        return await tools.cancel_run(_principal_facade(base_facade, auth_enabled), run_id)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )
    )
    async def retry_run(run_id: str) -> str:
        """Re-run a failed execution from its failed step, against current code.

        Everything that already succeeded is reused. Use this after fixing the
        cause of a failure.

        Args:
            run_id: The run identifier.
        """
        return await tools.retry_run(_principal_facade(base_facade, auth_enabled), run_id)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        )
    )
    async def replay_run(run_id: str) -> str:
        """Re-execute a run from its journal, repeating no side effects.

        A rehearsal of what the orchestration did, not a re-run. Use this to
        understand a run, not to fix one.

        Args:
            run_id: The run identifier.
        """
        return await tools.replay_run(_principal_facade(base_facade, auth_enabled), run_id)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def list_pending(run_id: str | None = None) -> str:
        """Runs parked on a person, and what each is being asked.

        Each carries the JSON Schema of the accepted answer, for
        respond_to_run. `delivered: false` means nobody was notified.

        Args:
            run_id: Only this run, instead of every parked one.
        """
        return await tools.list_pending(
            _principal_facade(base_facade, auth_enabled), run_id
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=False
        )
    )
    async def respond_to_run(run_id: str, subject: str, answer_json: str) -> str:
        """Answer a parked human request with a typed payload.

        Use approve_run for a plain yes/no; use this for a choice, a form, or
        an edited draft. list_pending gives the accepted shape.

        Args:
            run_id: The run identifier.
            subject: The request subject, e.g. "refund".
            answer_json: JSON object, e.g. '{"choice": "b"}'.
        """
        return await tools.respond_to_run(
            _principal_facade(base_facade, auth_enabled), run_id, subject, answer_json
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def pause_run(run_id: str) -> str:
        """Hold a run at its next durable step.

        Reversible with unpause_run. Prefer it over cancel_run, which is
        terminal and unwinds compensations.

        Args:
            run_id: The run identifier.
        """
        return await tools.pause_run(_principal_facade(base_facade, auth_enabled), run_id)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def unpause_run(run_id: str) -> str:
        """Release a run held by pause_run.

        Args:
            run_id: The run identifier.
        """
        return await tools.unpause_run(
            _principal_facade(base_facade, auth_enabled), run_id
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def pin_run(run_id: str, module: str = "") -> str:
        """Turn a run into a pytest file that reproduces it, from its journal.

        Returns the source; writes nothing.

        Args:
            run_id: The run to pin.
            module: Import path for the workflow, e.g. "flows.digest".
                Omitted, the file carries a TODO instead of an import.
        """
        return await tools.pin_run(
            _principal_facade(base_facade, auth_enabled), run_id, module
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def search_nodes(query: str = "", category: str | None = None) -> str:
        """Search the node catalogue — typed, versioned units of workflow work.

        Args:
            query: Keywords, e.g. "approval". Empty with a category lists it.
            category: human | guard | control | transform | io | browser |
                agent | custom.
        """
        return await tools.search_nodes(
            _principal_facade(base_facade, auth_enabled), query, category
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        )
    )
    async def connect_credential(
        credential: str,
        client_id: str = "",
        client_secret: str = "",
        device: bool = False,
    ) -> str:
        """Obtain a credential so this deployment's toolsets can be called.

        Args:
            credential: The name from list_connections, e.g. "jira".
            client_id: The OAuth app's client id, when one is needed.
            client_secret: Its secret, for a confidential client.
            device: Use the device-code flow, for a host with no browser.
        """
        return await tools.connect_credential(
            _principal_facade(base_facade, auth_enabled),
            credential, client_id, client_secret, device,
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def disconnect_credential(credential: str) -> str:
        """Forget a stored credential.

        Args:
            credential: The name to forget, e.g. "jira".
        """
        return await tools.disconnect_credential(
            _principal_facade(base_facade, auth_enabled), credential
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def list_connections(toolset: str = "") -> str:
        """Which integrations are configured here, and what the rest need.

        Args:
            toolset: One toolset id, e.g. "jira". Empty lists every one.
        """
        return await tools.list_connections(
            _principal_facade(base_facade, auth_enabled), toolset
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def show_node(node_id: str) -> str:
        """One node's contract: the exact code to write to call it.

        Args:
            node_id: A node id, e.g. "human.approval".
        """
        return await tools.show_node(
            _principal_facade(base_facade, auth_enabled), node_id
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def publish_workflow(workflow: str) -> str:
        """Record a workflow in the durable catalog.

        Args:
            workflow: The workflow name.
        """
        return await tools.publish_workflow(
            _principal_facade(base_facade, auth_enabled), workflow
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def artifact_history(name: str) -> str:
        """Every version of one named artifact, newest first.

        Args:
            name: The artifact name.
        """
        return await tools.artifact_history(
            _principal_facade(base_facade, auth_enabled), name
        )

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def search_toolsets(query: str) -> str:
        """Search registered toolsets (Jira, Gmail, Slack, ...) by keyword.

        Args:
            query: Keywords to search for, e.g. "jira" or "calendar".
        """
        return await tools.search_toolsets(query)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def show_toolset(toolset_id: str, group: str | None = None) -> str:
        """List the operations one toolset exposes, optionally filtered to a group.

        Args:
            toolset_id: A toolset id from search_toolsets, e.g. "jira".
            group: Only this operation group, e.g. "issues".
        """
        return await tools.show_toolset(toolset_id, group)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def list_artifacts() -> str:
        """List named artifacts stored on this runtime, latest version of each."""
        return await tools.list_artifacts(_principal_facade(base_facade, auth_enabled))

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=False, openWorldHint=False
        )
    )
    async def get_artifact_url(
        name: str, version: int | None = None, expires_in: int = 3600
    ) -> str:
        """Mint a time-limited download URL for a named artifact.

        Args:
            name: Artifact name, e.g. "daily-report.md".
            version: Specific version; omit for latest.
            expires_in: URL lifetime in seconds (default 3600).
        """
        return await tools.get_artifact_url(
            _principal_facade(base_facade, auth_enabled), name, version, expires_in
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        )
    )
    async def put_artifact(
        name: str, content_b64: str, mime: str = "application/octet-stream"
    ) -> str:
        """Publish small content as a named artifact.

        Args:
            name: Artifact name.
            content_b64: File bytes, base64-encoded. Keep this small.
            mime: Content type.
        """
        return await tools.put_artifact(
            _principal_facade(base_facade, auth_enabled), name, content_b64, mime
        )


    if authoring_enabled:
        # Facade-scoped, unlike the primitives in `_register_authoring_tools`:
        # this runs loom's own agent through `RuntimeFacade.author`, so an
        # `AuthorizedFacade` checks `workflows:author` before it spends a
        # token. Still behind the authoring flag, because an operator who
        # turned the authoring tools off did not mean "except the one that
        # does all of it at once".

        @tool(
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            )
        )
        async def author_workflow(
            ctx: Context,
            spec: str,
            packages_json: str = "[]",
            workflow_input_json: str = "",
            observe: bool = True,
        ) -> str:
            """Write a whole workflow from a description, using loom's agent.

            The one-shot counterpart to the authoring tools beside it: those
            hand you the pieces and you drive the loop; this runs loom's —
            discovery, observation, generation, the verification pipeline, and
            repair — and returns the code with what it found.

            `openWorldHint` is set because it always calls a model, and with
            *observe* on it reads systems the spec names.

            Args:
                spec: What the workflow should do. Name the systems it touches:
                    a spec saying "a URL" without giving one leaves the agent
                    inventing a target to look at.
                packages_json: JSON array of third-party packages the target
                    environment has, e.g. '["httpx"]'. Generated code importing
                    anything else is rejected.
                workflow_input_json: Input for the verification run. Worth
                    giving — an invented input makes an empty result
                    impossible to judge.
                observe: Let the agent look at the systems the spec names
                    before writing code against them.
            """
            # A server never reads stdin — under stdio that descriptor *is*
            # the protocol channel — so it asks through the client, which is
            # what `elicitation/create` is for. Capability-gated inside
            # `_can_ask`: a client that declared none simply gets an agent with
            # no `ask_user` tool.
            from loom.agents.interaction import ElicitationUserInteraction

            return await tools.author_workflow(
                _principal_facade(base_facade, auth_enabled),
                spec,
                packages_json,
                workflow_input_json,
                observe,
                interaction=ElicitationUserInteraction(ctx),
            )

        @tool(
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            )
        )
        async def edit_workflow(
            ctx: Context,
            source: str,
            instruction: str,
            packages_json: str = "[]",
            workflow_input_json: str = "",
            observe: bool = True,
        ) -> str:
            """Change an existing workflow by describing the change.

            Prefer this over author_workflow whenever a file already exists.
            The result is verified by the same checks a new workflow gets.

            Returns the whole file, a unified diff, and `graph_changes`
            (`+summarise -fetch`). `changed: false` means the model declined
            rather than guess — the file you passed is still correct. Writes
            nothing.

            Args:
                source: Current contents of the workflow file.
                instruction: What to change. Steps are never renamed: a name is
                    what the journal records, so changing one strands runs in
                    flight.
                packages_json: JSON array of packages the target environment
                    has, e.g. '["httpx"]'.
                workflow_input_json: Input for the verification run.
                observe: Let it look at the systems the instruction names.
            """
            from loom.agents.interaction import ElicitationUserInteraction

            return await tools.edit_workflow(
                _principal_facade(base_facade, auth_enabled),
                source,
                instruction,
                packages_json,
                workflow_input_json,
                observe,
                interaction=ElicitationUserInteraction(ctx),
            )

# ---------------------------------------------------------------------------
# Authoring tools
# ---------------------------------------------------------------------------





def _register_authoring_tools(
    server: MCPServer,
    config: AuthoringConfig,
    *,
    gate: AuthoringGate | None = None,
) -> None:
    """Expose LOOM's coding-agent toolchain so a client can author, not just
    run, workflows.

    Not facade-scoped, like ``search_toolsets``/``show_toolset`` above: what
    toolsets exist, whether code compiles, and whether it smoke-tests clean
    are server-wide facts, not per-run data.

    ``_seen`` is one dict for the life of this server instance, shared by
    every call to ``call_read_operation`` — the same repeat-limit protection
    ``build_coding_tools()`` gives the coding agent's own ReAct loop, so a
    client stuck re-asking the same lookup gets told to stop rather than
    burning calls against a real API.
    """
    from mcp.types import ToolAnnotations

    tool = _registrar(server.tool)
    _seen: dict[str, int] = {}
    #: An unconfigured gate refuses nothing, which is the compatibility
    #: contract every other identity check here follows: an install with no
    #: ``LOOM_AUTH_*`` var behaves exactly as it did before.
    checkpoint = gate or AuthoringGate()

    def _too_large(code: str) -> str | None:
        size = len(code.encode("utf-8"))
        if size <= config.max_code_size:
            return None
        return authoring._json(
            {
                "error": f"code is {size} bytes, over the {config.max_code_size} "
                "byte limit for this tool",
            }
        )

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def get_tool_contract(op_path: str) -> str:
        """Full typed contract for one toolset operation: schema, scopes,
        effect, and the import line generated code needs.

        Args:
            op_path: Dotted path like "jira.issues.search" — a toolset id
                from search_toolsets, then an operation id from show_toolset.
        """
        return await authoring.get_tool_contract(op_path)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def get_tool_docs(toolset_id: str) -> str:
        """Usage documentation for a toolset: import lines, signatures,
        and worked examples, where available.

        Args:
            toolset_id: e.g. "jira", "confluence".
        """
        return await authoring.get_tool_docs(toolset_id)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )
    )
    async def call_read_operation(op_path: str, arguments_json: str = "{}") -> str:
        """Execute a READ-ONLY toolset operation, to resolve a name to an id
        before writing code that depends on it.

        A real call against connected credentials. Write and destructive
        operations are refused. Use this instead of guessing what a spec's
        entity names resolve to.

        Args:
            op_path: e.g. "jira.projects.list".
            arguments_json: The operation's arguments, JSON-encoded object.
        """
        refused = checkpoint.refusal("call_read_operation")
        if refused is not None:
            return refused
        return await authoring.call_read_operation(op_path, arguments_json, seen=_seen)

    @tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def validate_workflow_code(
        code: str, allowed_packages: str | None = None, spec: str = ""
    ) -> str:
        """Check generated workflow code against LOOM's rules — compile,
        structure, determinism, imports, grants, coverage, entity resolution,
        lint, types. Does not execute it.

        Args:
            code: Complete Python source.
            allowed_packages: Comma-separated third-party packages the target
                environment has, e.g. "httpx,pandas". Omit to skip that check.
            spec: The user's own words for what this workflow should do.
                Always pass it: without it the coverage and resolution stages
                are skipped, and those are the two that catch code which
                compiles and validates cleanly while answering a different
                question than the one asked.
        """
        error = _too_large(code)
        if error is not None:
            return error
        return await authoring.validate_workflow_code(code, allowed_packages, spec)

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=False, openWorldHint=False
        )
    )
    async def smoke_test_workflow(
        code: str, workflow_input_json: str = "null"
    ) -> str:
        """Run generated workflow code once in a sandbox — no real network
        or credentials, every toolset faked.

        Args:
            code: Complete Python source; must contain an @workflow function.
            workflow_input_json: Input, JSON-encoded. "null" derives one from
                the workflow's declared type.
        """
        refused = checkpoint.refusal("smoke_test_workflow")
        if refused is not None:
            return refused
        error = _too_large(code)
        if error is not None:
            return error
        return await authoring.smoke_test_workflow(
            code, workflow_input_json, timeout=config.smoke_timeout
        )

    @tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        )
    )
    async def save_workflow(code: str, path: str) -> str:
        """Write generated workflow code to a file.

        Refuses an absolute path, a '..' component, or a non-.py extension.

        Args:
            code: Complete Python source.
            path: Relative file path, e.g. "flows/overdue_tickets.py".
        """
        refused = checkpoint.refusal("save_workflow")
        if refused is not None:
            return refused
        error = _too_large(code)
        if error is not None:
            return error
        return await authoring.save_workflow(code, path)



# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def _register_resources(server: MCPServer, base_facade: RuntimeFacade, auth_enabled: bool) -> None:
    """Expose the read-only documents a client can pull into context."""

    resource = _registrar(server.resource)

    @resource("loom://workflows")
    async def workflows() -> str:
        """Every workflow this server can run."""
        return await resources.read_workflows(_principal_facade(base_facade, auth_enabled))

    @resource("loom://workflows/{name}")
    async def workflow(name: str) -> str:
        """One workflow's definition and input schema."""
        return await resources.read_workflow(_principal_facade(base_facade, auth_enabled), name)

    @resource("loom://runs/{run_id}")
    async def run(run_id: str) -> str:
        """One run's status, input, output, and error."""
        return await resources.read_run(_principal_facade(base_facade, auth_enabled), run_id)

    @resource("loom://runs/{run_id}/journal")
    async def run_journal(run_id: str) -> str:
        """The durable operations a run recorded."""
        return await resources.read_run_journal(
            _principal_facade(base_facade, auth_enabled), run_id
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _register_prompts(
    server: MCPServer,
    base_facade: RuntimeFacade,
    auth_enabled: bool,
    authoring_enabled: bool = False,
) -> None:
    """Expose the reusable task templates."""

    prompt = _registrar(server.prompt)

    @prompt()
    def create_workflow(description: str) -> str:
        """Draft a LOOM workflow from a plain-English description."""
        return prompts.build_create_workflow_prompt(
            description, authoring_enabled=authoring_enabled
        )

    @prompt()
    async def debug_run(run_id: str) -> str:
        """Diagnose a failed run from its status and journal."""
        facade = _principal_facade(base_facade, auth_enabled)
        status = await facade.get(run_id) or {"error": f"No run '{run_id}'."}
        journal = await facade.journal(run_id) if "error" not in status else []
        return prompts.build_debug_run_prompt(status, journal)

    @prompt()
    async def explain_workflow(workflow: str) -> str:
        """Explain what a workflow does, step by step."""
        facade = _principal_facade(base_facade, auth_enabled)
        match = next(
            (e for e in await facade.workflows() if e["name"] == workflow), None
        )
        return prompts.build_explain_workflow_prompt(workflow, match)

    @prompt()
    def optimize_workflow(workflow: str) -> str:
        """Suggest durability and performance improvements for a workflow."""
        return prompts.build_optimize_prompt(workflow)

    @prompt()
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
    ``loom.cli.targets.resolve()``, which is what ``loom mcp`` does.
    """
    from loom.facade import LocalFacade
    from loom.runtime.engine import Runtime
    from loom.stores.factory import from_url

    return build_server(LocalFacade(Runtime(store=from_url(store_url))), name=name)
