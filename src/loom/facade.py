"""One port onto a Runtime, for every adapter that is not Python.

The CLI and the MCP server need the same handful of operations: list workflows,
start a run, read a journal, deliver an event, cancel or retry or replay. Each
had grown its own copy — ``CliBackend`` and ``RuntimeBridge`` — with the same
methods under different key names, which is two places to fix every bug.

This module owns the port instead. It belongs to neither adapter, so both depend
on an abstraction that neither controls, and a third adapter costs an
implementation rather than a rewrite.

Two implementations:

:class:`LocalFacade`
    An in-process :class:`Runtime`. The store comes from ``$LOOM_STORE``.
:class:`RemoteFacade`
    A running LOOM server, over :class:`LoomClient`.

Both return the *same* dictionary shapes, so a caller never needs to know which
one it was handed.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loom.core.exceptions import ConfigurationError, RegistryError
from loom.core.ids import new_id
from loom.core.models import ExecutionStatus, StepStatus, TriggerKind, TriggerRecord
from loom.core.redaction import DEFAULT_REDACT_KEYS, redact
from loom.core.serde import decode, encode

if TYPE_CHECKING:  # the seams themselves, named for the checker only —
    # importing them at runtime would make this module depend on the
    # engine and on the optional server extra just to be imported.
    from loom.blobs.artifact import ArtifactVersion
    from loom.blobs.signed_urls import UploadSession
    from loom.nodes.catalog import NodeDetail
    from loom.runtime.engine import Runtime
    from loom.server.client import LoomClient

__all__ = [
    "GraphProjection",
    "LocalFacade",
    "RemoteFacade",
    "RuntimeFacade",
    "VersionSurface",
    "describe_entry",
    "describe_record",
    "describe_result",
]


@runtime_checkable
class RuntimeFacade(Protocol):
    """The operations an adapter needs, however the Runtime is reached.

    Every method returns plain JSON-compatible data. Nothing here leaks a
    ``Runtime``, an ``ExecutionRecord``, or an HTTP response — that is what lets
    one caller serve both implementations.
    """

    async def workflows(self, *, published: bool = True) -> list[dict[str, Any]]:
        """Available workflows, each with ``name``/``version``/``executable``.

        ``published=False`` narrows the answer to what *this* process imported,
        and can therefore actually start.
        """
        ...

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
        env: dict[str, str] | None = None,
        credentials: dict[str, str] | Any | None = None,
    ) -> dict[str, Any]:
        """Start a run. ``wait=False`` returns as soon as it is recorded."""
        ...

    async def get(self, run_id: str) -> dict[str, Any] | None:
        """One run, or ``None`` when there is no such run."""
        ...

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def journal(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        """The run's durable operations, in order, keyed by ``step_id``.

        *offset* skips entries a caller has already seen — the shape
        :meth:`reports` already has, and for the same reason. A follower that
        refetches the whole journal every poll does quadratic work over the
        length of the run, and pays for it again in transfer against a remote
        Runtime.
        """
        ...

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        """What the run has said about itself, from *offset* onward.

        Distinct from the journal: the journal is what a run durably *did*, and
        this is what it chose to narrate while doing it. A four-minute step is
        one journal entry and can be a dozen reports.
        """
        ...

    async def send_event(
        self, run_id: str, name: str, payload: Any, *, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        """Deliver an event, resuming the run if it was parked on it.

        *dedupe_key* protects an at-least-once sender: a repeated key is
        recorded and dropped. Returns the delivery so a consumer can tell
        "delivered" from "already delivered" and ack correctly rather than
        retrying forever.
        """
        ...

    async def cancel(self, run_id: str) -> dict[str, Any]: ...

    async def retry(self, run_id: str) -> dict[str, Any]: ...

    async def replay(self, run_id: str) -> dict[str, Any]: ...

    async def publish(self, workflow: str) -> dict[str, Any]:
        """Record a workflow in the durable catalog."""
        ...

    async def schedules(self, workflow: str | None = None) -> list[dict[str, Any]]:
        """Registered schedules, newest first, optionally for one workflow."""
        ...

    async def schedule(
        self, workflow: str, cron: str, *, timezone: str = "UTC"
    ) -> dict[str, Any]:
        """Fire *workflow* on a cron expression. Returns the trigger."""
        ...

    async def unschedule(self, trigger_id: str) -> bool:
        """Remove a schedule. ``False`` when there was no such trigger."""
        ...

    async def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Every run parked on a person, and what each is being asked.

        Derived from the journal rather than a second store: a waiting run *is*
        a suspended ``approval:<subject>`` entry, and the sibling delivery entry
        holds the request. Keeping a parallel table would be one more thing to
        drift out of step with the run it describes.
        """
        ...

    async def respond(
        self, run_id: str, subject: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer a parked human request with a typed payload.

        ``approve`` is the yes/no shortcut over this; a choice, a form, or an
        edited draft needs the whole answer.
        """
        ...

    async def author(
        self,
        spec: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
        turns: int | None = None,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        resume: str = "",
    ) -> dict[str, Any]:
        """Write a workflow from a natural-language *spec*.

        On the port rather than in the CLI because the coding agent had no
        surface at all: every capability it grew was reachable only by writing
        a Python driver, which is why its loop went so long without pressure on
        it. Here, one method gives the CLI, the MCP server and anything else
        built on this port the same authoring, and ``test_surface_parity``
        fails the build if an adapter implements less than the whole thing.

        *packages* is what the target environment has installed, enforced
        against the generated imports. *smoke_input* is what the verification
        run passes in — worth supplying, since an invented input makes an empty
        result unjudgeable. *observe* lets the agent look at systems the spec
        names before writing code against them; off, it works from the spec
        alone, as it did before probes existed.

        Returns the code and everything a caller needs to decide whether to
        keep it: remaining issues, the code-or-judgement plan, repair count,
        and what the verification run did.
        """
        ...

    async def edit(
        self,
        source: str,
        instruction: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
    ) -> dict[str, Any]:
        """Change a workflow that already exists, and verify the result.

        The half of authoring the product did not have. ``author`` was the only
        entry point, so every change meant regenerating from a spec — while
        every comparable platform has shipped conversational editing. Teams
        iterate on workflows; they do not re-describe them.

        On the port for the same reason ``author`` is: one implementation, and
        ``test_surface_parity`` fails the build when an adapter implements less
        than the whole thing.

        Returns the edited source, a unified diff, the node-level delta
        projected from both versions, and whatever the verification pipeline
        still has to say. ``changed`` is ``False`` when the model declined —
        which is the answer it is asked to give when an instruction cannot be
        satisfied, and is better than a plausible edit that does the wrong
        thing.
        """
        ...

    async def pause(self, run_id: str) -> dict[str, Any]:
        """Hold a run at its next durable boundary.

        There was no way to do this. A run parked only when its own code said
        so, so the only things an operator could do to a misbehaving run were
        cancel it — terminal, unwinding compensations — or watch it.
        """
        ...

    async def unpause(self, run_id: str) -> dict[str, Any]:
        """Release a held run and let it continue."""
        ...

    async def pin(
        self, run_id: str, *, module: str = ""
    ) -> dict[str, Any]:
        """Generate a regression test that reproduces this run from its journal.

        Closes the loop durable execution exists for: a production failure
        becomes a committed test in one command. Values are redacted on the way
        out, because a step's *outputs* were never redacted into the journal and
        nobody expected them in a file somebody commits.
        """
        ...

    async def nodes(
        self, query: str = "", *, category: str | None = None
    ) -> list[dict[str, Any]]:
        """Catalogued nodes, optionally narrowed by query or category."""
        ...

    async def node(self, node_id: str) -> dict[str, Any]:
        """One node in full, including the code to call it."""
        ...

    async def list_artifacts(self) -> list[dict[str, Any]]:
        """Latest version of every named artifact."""
        ...

    async def artifact_history(self, name: str) -> list[dict[str, Any]]:
        """Every version of *name*, oldest first."""
        ...

    async def artifact_url(
        self, name: str, version: int | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        """Presigned download URL for an artifact version."""
        ...

    async def read_artifact(
        self, name: str, version: int | None = None
    ) -> dict[str, Any]:
        """Artifact bytes as base64, for backends that cannot sign URLs."""
        ...

    async def put_artifact(
        self,
        name: str,
        content_b64: str,
        *,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish small content under *name*. For MCP and CLI, not large files."""
        ...

    async def upload_url(
        self,
        name: str,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        """Create a presigned PUT session for *name*."""
        ...

    async def confirm_upload(
        self,
        upload_id: str,
        name: str,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify a presigned upload and publish it as an artifact."""
        ...

    async def read_blob(
        self, ref: str, expires: int, signature: str, method: str = "GET"
    ) -> dict[str, Any]:
        """Serve a locally signed blob after HMAC verification."""
        ...

    async def write_blob(
        self,
        ref: str,
        expires: int,
        signature: str,
        content_b64: str,
        mime: str = "application/octet-stream",
        method: str = "PUT",
    ) -> dict[str, Any]:
        """Accept a locally signed PUT after HMAC verification."""
        ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


_REDACT_METADATA = frozenset({"loom.env"})


def _public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata safe to put on the wire — env overrides are not."""
    out = dict(metadata or {})
    for key in _REDACT_METADATA:
        out.pop(key, None)
    return out


def describe_record(
    record: Any, redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS
) -> dict[str, Any]:
    """Render an :class:`ExecutionRecord` as the wire shape.

    A run's input and output are redacted **here** rather than in storage,
    unlike a journal entry's recorded input. The difference is that this value
    is replayed: the body receives it on every re-entry, so a workflow whose
    input carries a credential needs that credential intact at rest and absent
    from anything a person or an HTTP client is shown. Same reasoning as
    ``_public_metadata`` just above, one field over.
    """

    return {
        "run_id": record.run_id,
        "workflow": record.workflow,
        "status": record.status.value,
        "input": redact(decode(record.input), redact_keys),
        "output": redact(decode(record.output), redact_keys),
        "error": record.error.message if record.error else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "awaiting_event": record.awaiting_event,
        "metadata": _public_metadata(record.metadata),
        # Not secret and not new information — embedded callers already read
        # ``record.metadata`` directly — but exposing it here is what lets
        # ``AuthorizedFacade`` check a run's pinned owner identically whether
        # it wraps a ``LocalFacade`` or a ``RemoteFacade`` over HTTP.
    }


def describe_entry(entry: Any) -> dict[str, Any]:
    """Render a :class:`JournalEntry` as the wire shape.

    Carries the step's identity under both ``step_id`` and ``name``. That is one
    value under two keys, which is normally a smell — here it is deliberate: the
    HTTP surface published ``name`` and the CLI reads ``step_id``, and the point
    of routing both through one function is that neither can drift again. New
    callers should read ``step_id``.
    """

    return {
        "seq": entry.seq,
        "step_id": entry.name,
        "name": entry.name,
        "kind": entry.kind,
        "status": entry.status.value,
        "attempts": entry.attempts,
        "output": encode(entry.output),
        "error": entry.error.message if entry.error else None,
    }


def describe_result(result: Any) -> dict[str, Any]:
    """Render an :class:`ExecutionResult` as the wire shape."""

    return {
        "run_id": result.run_id,
        "workflow": result.workflow,
        "status": result.status.value,
        "output": decode(result.output),
        "error": result.error.message if result.error else None,
    }


def describe_artifact(version: ArtifactVersion) -> dict[str, Any]:
    """Render an :class:`ArtifactVersion` as the wire shape."""
    return version.model_dump(mode="json")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")



@runtime_checkable
class GraphProjection(Protocol):
    """The graph a workflow projects, and a run overlaid on it.

    **A separate protocol, not more methods on :class:`RuntimeFacade`.** That
    one is ``runtime_checkable``, so a Protocol is all-or-nothing: adding a
    method would make every host's existing facade stop being a
    ``RuntimeFacade`` at all. A capability nobody had yesterday must not be able
    to invalidate what already shipped — the same rule
    :class:`~loom.stores.base.IndexedScans` follows.

    Everything behind it was already built and correct — WGIR extraction, the
    React Flow projection with real source spans, the journal overlay — and
    reachable from nowhere but the CLI. A canvas and a run inspector are the two
    things a studio is made of, and both were blocked on two methods.
    """

    async def graph(self, workflow: str) -> dict[str, Any]:
        """The workflow's structure, as React Flow nodes and edges.

        Projected from the code, never authored: the decorators declare the
        graph and the AST pass fills in the control flow, so it cannot drift
        from what runs. Nodes carry ``data.source`` line spans, which is what
        lets a canvas jump from a box to the line that produced it.
        """
        ...

    async def trace(self, run_id: str) -> dict[str, Any]:
        """The same graph with this run's journal overlaid on it.

        One call rather than "fetch the graph, fetch the journal, join them in
        the client": the join is by node id against WGIR, and a client that
        reimplements it will get a slightly different answer than ``loom show``.
        """
        ...


@runtime_checkable
class VersionSurface(Protocol):
    """The version chain, and which of it is being served.

    Optional and separate for the same reason as :class:`GraphProjection`:
    ``RuntimeFacade`` is ``runtime_checkable``, so a member added there stops
    every host's existing facade from being one.

    Versions were Runtime-only — ``rt.versions`` and nothing else. Committing is
    already reachable through ``rt.publish``; what had no surface at all was
    *reading the chain* and *choosing which entry is live*, which is the half a
    control plane needs. Without it, "roll back to version 3" means re-committing
    version 3's source as version 6, which loses the fact of what is actually
    being served and inflates the chain with duplicates of code that already
    exists.
    """

    async def versions(self, workflow: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The committed chain, newest first, each marked ``active`` or not."""
        ...

    async def activate_version(self, workflow: str, version: int) -> dict[str, Any]:
        """Make *version* the served one. A pointer move, never a commit."""
        ...

    async def version_source(self, workflow: str, version: int) -> str:
        """The source a version was committed from."""
        ...

@dataclass
class LocalFacade:
    """Drives an in-process :class:`Runtime`.

    The store comes from ``$LOOM_STORE`` via ``Runtime.from_env()``, so the same
    adapter reads memory in a test, SQLite on a laptop, and Postgres in
    production without changing.
    """

    runtime: Runtime
    loaded: list[str] = field(default_factory=list)
    """Module specs that were imported to populate this Runtime."""
    _trigger_dispatcher: Any = field(default=None, repr=False, compare=False)
    user_interaction: Any = None
    """How the coding agent asks the person who wrote the spec a question.

    A :class:`~loom.agents.interaction.UserInteraction`. ``None`` — the default
    — omits the ``ask_user`` tool entirely rather than offering one that always
    answers "not configured", the rule ``observe_target`` already follows.

    On the **local** adapter rather than on ``author()``, for two reasons that
    agree. It is an object and not a payload, so it could not cross
    ``RemoteFacade`` even if the port carried it — and ``RemoteFacade.author``
    already refuses, because authoring runs where the code will run. And a
    library that reads stdin because it was imported is the ambient behaviour
    ``Runtime`` avoids everywhere else: the CLI opts in, a server does not.
    """
    hooks: Any = None
    """Optional :class:`~loom.runtime.hooks.HookRegistry` for authoring runs.

    Here for the same reasons ``user_interaction`` is: it is an object rather
    than a payload, so it cannot cross ``RemoteFacade`` — which refuses to
    author anyway — and a surface that wants progress opts into it rather than
    every caller getting a renderer it did not ask for.

    Threaded into the coding agent, whose runner brackets each turn, model call
    and tool call with the agent hook family. Without it an authoring run
    emitted nothing between the first prompt and the final answer.
    """
    on_stage: Any = None
    """Optional ``(check, result) -> None`` called around each verification
    stage. Not a hook: a stage reaches no model, so the agent family has
    nothing to say about it, and inventing an event for a plain loop would be a
    mechanism where a callback is the thing."""
    _authoring: Any = field(default=None, repr=False, compare=False)
    last_session_id: str = ""
    """The authoring job this facade last opened.

    Recorded as the agent is built rather than returned with the result,
    because the moment it is needed is the moment there *is* no result: a
    caller interrupted four minutes in has lost the id along with everything
    else, and reading it back out of the store during a cancellation is the
    least reliable thing that could be done at that point."""
    async def workflows(self, *, published: bool = True) -> list[dict[str, Any]]:
        available = {
            definition.name: {
                "name": definition.name,
                "version": definition.version,
                "description": definition.description,
                "code_hash": definition.code_hash,
                "triggers": [spec.name for spec in definition.triggers],
                "input_schema": definition.input_schema(),
                "executable": True,
            }
            for definition in self.runtime.workflows.values()
        }
        catalogued = await self.runtime.published() if published else []
        for record in catalogued:
            if record.name in available:
                available[record.name]["source_file"] = record.source_file
                continue
            available[record.name] = {
                "name": record.name,
                "version": record.version,
                "description": record.description,
                "code_hash": record.code_hash,
                "triggers": record.triggers,
                "source_file": record.source_file,
                # Published but not imported here, so this process cannot run it.
                "executable": False,
            }
        return sorted(available.values(), key=lambda entry: entry["name"])

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
        env: dict[str, str] | None = None,
        credentials: dict[str, str] | Any | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if env:
            extra["env"] = env
        if credentials is not None:
            extra["credentials"] = credentials
        if wait:
            result = await self.runtime.run(
                workflow,
                payload,
                idempotency_key=idempotency_key,
                tags=tags or [],
                metadata=metadata or {},
                **extra,
            )
            run_id = result.run_id
        else:
            run_id = await self.runtime.submit(
                workflow,
                payload,
                idempotency_key=idempotency_key,
                tags=tags or [],
                metadata=metadata or {},
                **extra,
            )
        found = await self.get(run_id)
        return found or {"run_id": run_id, "status": "pending", "workflow": workflow}

    async def get(self, run_id: str) -> dict[str, Any] | None:
        record = await self.runtime.get(run_id)
        return (
            None
            if record is None
            else describe_record(record, self.runtime.redact_keys)
        )

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:

        records = await self.runtime.list_runs(
            workflow=workflow,
            status=ExecutionStatus(status) if status else None,
            limit=limit,
        )
        return [
            describe_record(record, self.runtime.redact_keys) for record in records
        ]

    async def journal(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        entries = await self.runtime.history(run_id)
        return [describe_entry(entry) for entry in entries[max(offset, 0) :]]

    # -- graph projection (GraphProjection) -----------------------------------

    async def graph(self, workflow: str) -> dict[str, Any]:
        return _react_flow(self._wgir(workflow))

    async def trace(self, run_id: str) -> dict[str, Any]:
        from loom.graph.trace import overlay_journal

        record = await self.runtime.get(run_id)
        if record is None:
            raise RegistryError(f"no execution with id '{run_id}'")
        graph = self._wgir(record.workflow)
        # The store, not `history()`: that projects StepRecords for display,
        # and the overlay joins on the raw JournalEntry paths.
        overlay = overlay_journal(
            graph, await self.runtime.store.load_journal(run_id), run_id=run_id
        )
        payload = _react_flow(graph, trace=overlay)
        payload["run"] = {
            "run_id": run_id,
            "workflow": record.workflow,
            "status": record.status.value,
        }
        return payload

    def _wgir(self, workflow: str) -> Any:
        """The WGIR graph for a registered workflow, read from its source file.

        Extraction reads the file rather than the live object because the AST
        pass is what supplies the control flow — branches, loops, joins — that
        decorators alone cannot describe. A workflow this process did not import
        has no source to read, and saying so beats returning an empty canvas
        that looks like a workflow with no steps.
        """
        import inspect
        from pathlib import Path

        from loom.graph.pipeline import build_graph

        definition = self.runtime.workflows.get(workflow)
        if definition is None:
            raise RegistryError(
                f"workflow {workflow!r} is not registered in this process, so its "
                "source cannot be read. Graph projection needs the code, not just "
                "the catalogue entry."
            )
        source = inspect.getsourcefile(definition.fn)
        if not source:
            raise RegistryError(
                f"workflow {workflow!r} has no source file on disk — defined in a "
                "REPL or an exec'd string, so there is nothing to extract from."
            )
        return build_graph(Path(source), flow_id=workflow)


    # -- versions (VersionSurface) --------------------------------------------

    async def versions(self, workflow: str, *, limit: int = 50) -> list[dict[str, Any]]:
        chain = await self.runtime.versions.history(workflow, limit=limit)
        live = await self._active_number(workflow)
        return [_describe_version(v, active=v.version == live) for v in chain]

    async def activate_version(self, workflow: str, version: int) -> dict[str, Any]:
        await self.runtime.versions.activate(workflow, version)
        served = await self.runtime.versions.active(workflow)
        if served is None:  # pragma: no cover - activate() raises first
            raise RegistryError(f"workflow {workflow!r} has no active version")
        return _describe_version(served, active=True)

    async def version_source(self, workflow: str, version: int) -> str:
        for entry in await self.runtime.versions.history(workflow, limit=1000):
            if entry.version == version:
                source: str = await self.runtime.versions.source_of(entry)
                return source
        raise RegistryError(
            f"workflow {workflow!r} has no version {version}"
        )

    async def _active_number(self, workflow: str) -> int | None:
        """Which version is served, or ``None`` when nobody has declared one.

        Deliberately not falling back to ``latest()``: that fallback is the
        conflation the pointer exists to prevent, and it would make a rollback
        un-roll itself on the next publish.
        """
        # Imported here, not at module scope: this module stays importable
        # without the engine, which is what lets one caller serve both facades.
        from loom.runtime.versions import VersionActivation

        store = self.runtime.versions
        if not isinstance(store, VersionActivation):
            return None
        served = await store.active(workflow)
        return None if served is None else served.version

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        if await self.get(run_id) is None:
            raise RegistryError(f"no execution with id '{run_id}'")
        since = getattr(self.runtime.stream, "since", None)
        if since is None:
            # A host stream that only accepts reports and does not serve them
            # back. Empty is the honest answer; pretending otherwise would make
            # "this run said nothing" and "I cannot see what it said" the same.
            return []
        return [report.describe() for report in since(run_id, offset)]

    async def send_event(
        self, run_id: str, name: str, payload: Any, *, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        delivery = await self.runtime.send_event(
            run_id, name, payload, dedupe_key=dedupe_key
        )
        if delivery.delivered:
            # Deliver it: in process, an event only advances the run if
            # something re-enters the body, and nothing else is driving it here.
            await self.runtime.resume(run_id)
        return delivery.model_dump(mode="json")

    async def cancel(self, run_id: str) -> dict[str, Any]:
        await self.runtime.cancel(run_id)
        return await self.get(run_id) or {}

    async def retry(self, run_id: str) -> dict[str, Any]:
        return describe_result(await self.runtime.retry(run_id))

    async def replay(self, run_id: str) -> dict[str, Any]:
        return describe_result(await self.runtime.replay(run_id))

    async def publish(self, workflow: str) -> dict[str, Any]:
        record = await self.runtime.publish(workflow)
        return record.model_dump(mode="json")

    # -- human requests -----------------------------------------------------

    async def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:

        if run_id is not None:
            record = await self.runtime.get(run_id)
            records = [record] if record is not None else []
        else:
            records = await self.runtime.list_runs(
                status=ExecutionStatus.SUSPENDED, limit=200
            )

        waiting: list[dict[str, Any]] = []
        for record in records:
            waiting.extend(await self._waiting_on_a_person(record))
        return waiting

    async def _waiting_on_a_person(self, record: Any) -> list[dict[str, Any]]:
        """The requests this run is parked on, read out of its journal.

        Against :class:`StepRecord`, which is the *public* view of the journal
        and a coarser vocabulary than the entries the engine writes: ``kind`` is
        a plain string and ``EntryStatus.SUSPENDED`` surfaces as
        ``StepStatus.RUNNING``. Filtering on the engine's own enums here matched
        nothing and returned an empty list — which reads as "nothing is waiting",
        the one wrong answer this command must never give.
        """
        from loom.nodes.human.asking import HumanTicket

        entries = await self.runtime.history(record.run_id)

        tickets: dict[str, HumanTicket] = {}
        for entry in entries:
            if entry.kind == "step" and entry.name.startswith("deliver:"):
                try:
                    delivered = HumanTicket.model_validate(entry.output)
                except Exception:
                    continue
                tickets[delivered.request.subject] = delivered

        #: A journalled wait that has not resolved. The engine writes SUSPENDED;
        #: the public view reports it as RUNNING or PENDING.
        unresolved = {StepStatus.RUNNING, StepStatus.PENDING}

        found: list[dict[str, Any]] = []
        for entry in entries:
            if entry.kind != "event" or entry.status not in unresolved:
                continue
            if not entry.name.startswith("approval:"):
                continue
            subject = entry.name.removeprefix("approval:")
            ticket = tickets.get(subject)
            request = ticket.request if ticket else None
            found.append(
                {
                    "run_id": record.run_id,
                    "workflow": record.workflow,
                    "subject": subject,
                    # A request raised by ctx.wait_for_approval has no ticket —
                    # it is still waiting on a person and still belongs here,
                    # with the fields a node would have supplied left empty.
                    "prompt": request.prompt if request else "",
                    "node_id": request.node_id if request else "",
                    "assignees": list(request.assignees) if request else [],
                    "context": dict(request.context) if request else {},
                    "response_schema": dict(request.response_schema) if request else {},
                    "expires_at": (
                        request.expires_at.isoformat()
                        if request and request.expires_at
                        else None
                    ),
                    "delivered": ticket.receipt.delivered if ticket else False,
                    "channel": ticket.receipt.channel if ticket else "",
                    "next_action": f"loom respond {record.run_id} {subject} --approve",
                }
            )
        return found

    async def respond(
        self, run_id: str, subject: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        await self.send_event(run_id, f"approval:{subject}", answer)
        return await self.get(run_id) or {}

    # -- nodes --------------------------------------------------------------

    async def author(
        self,
        spec: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
        turns: int | None = None,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        resume: str = "",
    ) -> dict[str, Any]:
        agent = await self._coding_agent(
            packages=packages,
            smoke_input=smoke_input,
            observe=observe,
            turns=turns,
            max_tokens=max_tokens,
            max_cost=max_cost,
            resume=resume,
        )
        result = await agent.generate(spec)

        # Flattened here rather than at each surface: the CLI renders it, the
        # MCP server serialises it, and a shared shape is what keeps the two
        # from drifting into different vocabularies for the same run.
        return {
            "code": result.code,
            "clean": result.is_clean,
            "repairs": result.repair_attempts,
            "model": result.model_used,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "issues": [
                {
                    "category": issue.category,
                    "severity": issue.severity,
                    "message": issue.message,
                }
                for issue in result.issues
            ],
            "plan": [
                {"node": node.node, "kind": node.kind, "why": node.why}
                for node in result.plan
            ],
            "tools_used": [name for name, _ in result.tool_calls],
            # The answers are inputs to this build, so they travel with its
            # output: `loom author --answers` replays them, and the same spec
            # and the same answers reproduce the same file.
            "questions": [asked.model_dump(mode="json") for asked in result.questions],
            # The id `--resume` takes. Reported on every job, not only a failed
            # one: whoever is going to be interrupted does not know it yet.
            "session_id": agent.session_id,
            "smoke": None
            if result.smoke is None
            else {
                "ok": result.smoke.ok,
                "phase": result.smoke.phase,
                "status": result.smoke.status,
                "steps_executed": result.smoke.steps_executed,
                "error": result.smoke.error,
            },
        }

    async def edit(
        self,
        source: str,
        instruction: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
    ) -> dict[str, Any]:
        agent = await self._coding_agent(
            packages=packages, smoke_input=smoke_input, observe=observe
        )
        result = await agent.edit(source, instruction)
        return {
            "code": result.code,
            "changed": result.changed,
            "clean": result.is_clean,
            "explanation": result.explanation,
            "diff": result.diff,
            "graph_changes": result.graph_changes,
            "questions": [asked.model_dump(mode="json") for asked in result.questions],
            # The id `--resume` takes. Reported on every job, not only a failed
            # one: whoever is going to be interrupted does not know it yet.
            "session_id": agent.session_id,
            "repairs": result.repair_attempts,
            "model": result.model_used,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "issues": [
                {
                    "category": issue.category,
                    "severity": issue.severity,
                    "message": issue.message,
                }
                for issue in result.issues
            ],
            "smoke": None
            if result.smoke is None
            else {
                "ok": result.smoke.ok,
                "phase": result.smoke.phase,
                "error": result.smoke.error,
            },
        }

    async def pause(self, run_id: str) -> dict[str, Any]:
        await self.runtime.pause(run_id)
        return await self._describe(run_id)

    async def unpause(self, run_id: str) -> dict[str, Any]:
        await self.runtime.unpause(run_id)
        return await self._describe(run_id)

    async def pin(self, run_id: str, *, module: str = "") -> dict[str, Any]:
        from loom.testing.pin import pin_run

        record = await self.runtime.get(run_id)
        if record is None:
            raise RegistryError(f"no run {run_id}")
        journal = await self.runtime.store.load_journal(run_id)
        pinned = pin_run(record, journal, module=module)
        return {
            "run_id": run_id,
            "workflow": record.workflow,
            "source": pinned.source,
            "filename": pinned.filename,
            "seeded": pinned.seeded,
            "notes": pinned.notes,
        }

    async def _describe(self, run_id: str) -> dict[str, Any]:
        record = await self.runtime.get(run_id)
        if record is None:
            raise RegistryError(f"no run {run_id}")
        return describe_record(record)

    async def _coding_agent(
        self,
        *,
        packages: list[str] | None,
        smoke_input: Any,
        observe: bool,
        turns: int | None = None,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        resume: str = "",
    ) -> Any:
        """One place that builds the agent, for authoring and for editing.

        Extracted rather than duplicated: the two entry points must agree about
        which toolsets, nodes and probes the agent may see, or an edit would be
        verified against a different world than the one that wrote the file.
        """
        from loom.agents import providers
        from loom.agents.coding_agent import WorkflowCodingAgent

        model = providers.from_env()
        if model is None:
            raise ConfigurationError(
                "authoring needs a model. Set one of "
                + ", ".join(providers.env_keys())
                + " and install that provider's extra."
            )

        probes = None
        if observe:
            from loom.agents.probes import default_probes

            probes = default_probes()

        # Typed `Any` because a `**dict[str, int]` splat makes mypy check that
        # value against every keyword the constructor has.
        extra: dict[str, Any] = {}
        if turns:
            extra["max_discovery_turns"] = turns
        # Ceilings on the *job*, not on one call. They existed on the agent and
        # reached no surface, so `max_cost_usd` bounded nothing anybody could
        # set. A dollar ceiling on a model with no price on file is refused at
        # construction, which is the right place for it.
        if max_tokens:
            extra["max_total_tokens"] = max_tokens
        if max_cost:
            extra["max_cost_usd"] = max_cost

        # Snapshots live in the same store the runs do, so an interrupted
        # authoring job is recoverable from the project you were standing in
        # — the same place `loom runs` looks.
        from loom.agents.session_store import StoreBackedSessionStore

        sessions = StoreBackedSessionStore(self.runtime.store)
        snapshot = await sessions.load(resume) if resume else None
        if resume and snapshot is None:
            raise RegistryError(
                f"no authoring session {resume!r}. It may have expired — they "
                "are kept for a week."
            )

        agent = WorkflowCodingAgent(
            model,
            hooks=self.hooks,
            on_stage=self.on_stage,
            session_store=sessions,
            resume=snapshot,
            tool_registry=self.runtime.toolsets,
            node_registry=self.runtime.nodes,
            probes=probes,
            allowed_packages=set(packages) if packages else None,
            smoke_input=smoke_input,
            user_interaction=self.user_interaction,
            # The same "now" the runs read. A Runtime under `ManualClock` and
            # an authoring job reading the wall clock would disagree about the
            # date in the one place that has to agree with it.
            clock=self.runtime.clock,
            # Only when asked. A spec naming several systems needs more turns
            # than the default, and a run that ends "exceeded its budget"
            # produced nothing having spent everything.
            **extra,
        )
        # Read back rather than predicted: the agent mints the id, and a second
        # opinion about what it is would be a second implementation of it.
        self.last_session_id = agent.session_id
        self._authoring = agent
        return agent

    @property
    def resumable(self) -> bool:
        """Whether the last authoring job left something to resume."""
        return bool(getattr(getattr(self, "_authoring", None), "resumable", False))

    async def nodes(
        self, query: str = "", *, category: str | None = None
    ) -> list[dict[str, Any]]:
        cards = self.runtime.nodes.search(query or "", category=category, limit=200)
        return [card.model_dump(mode="json") for card in cards]

    async def node(self, node_id: str) -> dict[str, Any]:
        # ``Runtime.nodes`` is declared loosely on the engine so a host can pass
        # its own catalog; naming what this method actually relies on keeps the
        # dependency checkable from here rather than only at runtime.
        detail: NodeDetail = self.runtime.nodes.show(node_id)
        payload = detail.model_dump(mode="json")
        payload["contract"] = self.runtime.nodes.contract(node_id)
        return payload

    def _dispatcher(self) -> Any:
        """One dispatcher per Runtime, made on demand.

        Cached on the facade rather than on the Runtime: scheduling is a
        control-plane concern, and the manager tools used to reach in and set
        ``runtime._dispatcher`` themselves — a private attribute on somebody
        else's object, which is what a missing boundary looks like.
        """
        from loom.runtime.dispatcher import TriggerDispatcher

        if self._trigger_dispatcher is None:
            self._trigger_dispatcher = TriggerDispatcher(self.runtime)
        return self._trigger_dispatcher

    async def schedules(self, workflow: str | None = None) -> list[dict[str, Any]]:
        records = await self._dispatcher()._store.list_triggers(workflow=workflow)
        return [record.model_dump(mode="json") for record in records]

    async def schedule(
        self, workflow: str, cron: str, *, timezone: str = "UTC"
    ) -> dict[str, Any]:
        from loom.triggers.specs import Schedule

        definition = self.runtime.resolve_workflow(workflow)
        spec = Schedule(cron, timezone=timezone)

        # The spec computes its own first fire time. Recomputing it here would
        # be a second place to get a timezone wrong, and the two would disagree
        # only for the schedules nobody watches.
        record = TriggerRecord(
            trigger_id=new_id("trg"),
            workflow=definition.name,
            kind=TriggerKind.SCHEDULE,
            spec=spec.describe(),
            next_fire_at=spec.next_fire(self.runtime.clock.now()),
            timezone=timezone,
        )
        await self._dispatcher()._store.save_trigger(record)
        return record.model_dump(mode="json")

    async def unschedule(self, trigger_id: str) -> bool:
        store = self._dispatcher()._store
        if await store.get_trigger(trigger_id) is None:
            return False
        await store.delete_trigger(trigger_id)
        return True

    async def list_artifacts(self) -> list[dict[str, Any]]:
        service = self.runtime.require_artifacts()
        latest = []
        for name in await service.names():
            latest.append(describe_artifact(await service.get(name)))
        return latest

    async def artifact_history(self, name: str) -> list[dict[str, Any]]:
        service = self.runtime.require_artifacts()
        return [describe_artifact(item) for item in await service.history(name)]

    async def artifact_url(
        self, name: str, version: int | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        service = self.runtime.require_artifacts()
        url = await service.url(name, version, expires_in=expires_in)
        resolved = await service.get(name, version)
        return {
            "url": url,
            "name": resolved.name,
            "version": resolved.version,
            "expires_in": expires_in,
            "mime": resolved.mime,
        }

    async def read_artifact(
        self, name: str, version: int | None = None
    ) -> dict[str, Any]:
        service = self.runtime.require_artifacts()
        resolved = await service.get(name, version)
        data = await service.read(resolved.name, resolved.version)
        disposition = resolved.content_disposition or resolved.name
        return {
            "content_b64": _b64(data),
            "mime": resolved.mime,
            "size": resolved.size,
            "name": resolved.name,
            "version": resolved.version,
            "sha256": resolved.sha256,
            "content_disposition": disposition,
        }

    async def put_artifact(
        self,
        name: str,
        content_b64: str,
        *,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        service = self.runtime.require_artifacts()
        data = base64.b64decode(content_b64)
        extra = dict(metadata or {})
        version = await service.put(name, data, mime=mime, **extra)
        return describe_artifact(version)

    async def upload_url(
        self,
        name: str,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        urls = self.runtime.require_signed_urls()
        session: UploadSession = await urls.create_upload_session(
            name, mime=mime, max_size=max_size, expires_in=expires_in
        )
        return session.model_dump(mode="json")

    async def confirm_upload(
        self,
        upload_id: str,
        name: str,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        urls = self.runtime.require_signed_urls()
        session = await urls._load_session(upload_id)
        if session.name != name:
            raise ConfigurationError(
                f"upload '{upload_id}' was created for {session.name!r}, not {name!r}"
            )
        version = await urls.confirm_upload(
            upload_id,
            artifacts=self.runtime.require_artifacts(),
            run_id=run_id,
            metadata=metadata,
        )
        return describe_artifact(version)

    async def read_blob(
        self, ref: str, expires: int, signature: str, method: str = "GET"
    ) -> dict[str, Any]:
        from loom.blobs.blob import BlobNotFoundError
        from loom.security.rbac import AuthorizationError

        blobs = self.runtime.blobs
        if blobs is None:
            raise ConfigurationError(
                "blob downloads need blob storage. Pass blobs=BlobService(...) to Runtime()."
            )
        backend = blobs.backend
        verify = getattr(backend, "verify_signed_url", None)
        if verify is None or not verify(ref, expires, signature, method):
            raise AuthorizationError("invalid or expired blob signature")
        try:
            data = await backend.get(ref)
        except BlobNotFoundError:
            raise AuthorizationError("invalid or expired blob signature") from None
        mime = "application/octet-stream"
        head = getattr(backend, "head", None)
        if head is not None:
            with contextlib.suppress(Exception):
                mime = (await head(ref)).mime or mime
        return {
            "content_b64": _b64(data),
            "mime": mime,
            "size": len(data),
            "ref": ref,
        }

    async def write_blob(
        self,
        ref: str,
        expires: int,
        signature: str,
        content_b64: str,
        mime: str = "application/octet-stream",
        method: str = "PUT",
    ) -> dict[str, Any]:
        from loom.security.rbac import AuthorizationError

        blobs = self.runtime.blobs
        if blobs is None:
            raise ConfigurationError(
                "blob uploads need blob storage. Pass blobs=BlobService(...) to Runtime()."
            )
        backend = blobs.backend
        verify = getattr(backend, "verify_signed_url", None)
        if verify is None or not verify(ref, expires, signature, method):
            raise AuthorizationError("invalid or expired blob signature")
        data = base64.b64decode(content_b64)
        await backend.put(ref, data, mime)
        return {"ref": ref, "size": len(data), "mime": mime}

    async def close(self) -> None:
        await self.runtime.shutdown()
        store_close = getattr(self.runtime.store, "close", None)
        if store_close is not None:
            await store_close()


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------


#: Why scheduling is local-only for now. Stated once so both the error and any
#: future HTTP route point at the same reason.
_NO_REMOTE_SCHEDULING = (
    "scheduling is not exposed over HTTP yet. Run the command against the "
    "process that owns the store — drop --server — or add the routes."
)
#: Why the human-request and node views are local-only for now. The same shape
#: as scheduling: the capability exists on the port, the HTTP routes do not yet,
#: and saying which is missing beats a NotImplementedError.
_NO_REMOTE_HUMAN = (
    "pending human requests are not exposed over HTTP yet. Run the command "
    "against the process that owns the store — drop --server — or add the "
    "routes. Answering one already works remotely: use 'loom approve'."
)
_NO_REMOTE_NODES = (
    "the node catalog is not exposed over HTTP yet, and a remote server's "
    "catalog is the one that matters. Drop --server to browse this process's."
)
_NO_REMOTE_AUTHORING = (
    "authoring runs where the code will run, not on the server. It reads this "
    "process's toolsets, nodes and probes to decide what the workflow may "
    "call, and it needs a model key — a server's would be spending someone "
    "else's budget on your behalf. Drop --server."
)
_NO_REMOTE_DEDUPE = (
    "dedupe_key is not carried over HTTP yet, and dropping it would let a "
    "redelivered event resume a run twice while the caller saw success. Run "
    "the consumer against the process that owns the store — drop --server — or "
    "add the parameter to the route."
)
_NO_REMOTE_CREDENTIALS = (
    "credentials= cannot be sent over HTTP — a live token would leave the "
    "process. Run 'loom connect <name>' on the server, or start the run "
    "in-process."
)


@dataclass
class RemoteFacade:
    """Drives a running LOOM server through :class:`LoomClient`."""

    client: LoomClient

    async def workflows(self, *, published: bool = True) -> list[dict[str, Any]]:
        return await self.client.workflows(published=published)

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
        env: dict[str, str] | None = None,
        credentials: dict[str, str] | Any | None = None,
    ) -> dict[str, Any]:
        if credentials is not None:
            raise ConfigurationError(_NO_REMOTE_CREDENTIALS)
        return await self.client.start(
            workflow,
            payload,
            idempotency_key=idempotency_key,
            tags=tags,
            metadata=metadata,
            wait=wait,
            env=env,
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        from loom.server.client import LoomClientError

        try:
            return await self.client.get(run_id)
        except LoomClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self.client.list_runs(
            workflow=workflow, status=status, limit=limit
        )

    async def journal(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        # An older server may predate ``describe_entry`` and send only "name".
        # It may also predate ``offset``, and would then ignore the parameter
        # and send the whole journal — so the slice is applied here as well.
        # Belt and braces on purpose: silently replaying entries the caller has
        # already rendered is worse than one redundant slice.
        entries = await self.client.journal(run_id, offset=offset)
        # Whether the server honoured *offset* is readable from the answer: a
        # server that did starts at the entry we asked for, one that predates
        # the parameter starts at zero. Re-slicing on that is what keeps an
        # older server from replaying entries the caller has already rendered.
        first = entries[0].get("seq") if entries else None
        if offset > 0 and isinstance(first, int) and first < offset:
            entries = entries[offset:]
        return [
            {**entry, "step_id": entry.get("step_id") or entry.get("name", "")}
            for entry in entries
        ]

    async def versions(self, workflow: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self.client.versions(workflow, limit=limit)

    async def activate_version(self, workflow: str, version: int) -> dict[str, Any]:
        return await self.client.activate_version(workflow, version)

    async def version_source(self, workflow: str, version: int) -> str:
        return await self.client.version_source(workflow, version)

    async def graph(self, workflow: str) -> dict[str, Any]:
        return await self.client.graph(workflow)

    async def trace(self, run_id: str) -> dict[str, Any]:
        return await self.client.trace(run_id)

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        return await self.client.reports(run_id, offset=offset)

    async def send_event(
        self, run_id: str, name: str, payload: Any, *, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        if dedupe_key is not None:
            # Refuse rather than silently drop the key: a caller passing one
            # believes redeliveries are being suppressed, and a server that
            # ignores it would resume the run twice while the client saw
            # success. The route can carry it once the HTTP surface does.
            raise ConfigurationError(_NO_REMOTE_DEDUPE)
        await self.client.send_event(run_id, name, payload)
        return {"delivered": True, "reason": "", "run_ids": [], "dedupe_key": ""}

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self.client.cancel(run_id)

    async def retry(self, run_id: str) -> dict[str, Any]:
        return await self.client.retry(run_id)

    async def replay(self, run_id: str) -> dict[str, Any]:
        return await self.client.replay(run_id)

    async def schedules(self, workflow: str | None = None) -> list[dict[str, Any]]:
        raise ConfigurationError(_NO_REMOTE_SCHEDULING)

    async def schedule(
        self, workflow: str, cron: str, *, timezone: str = "UTC"
    ) -> dict[str, Any]:
        raise ConfigurationError(_NO_REMOTE_SCHEDULING)

    async def unschedule(self, trigger_id: str) -> bool:
        raise ConfigurationError(_NO_REMOTE_SCHEDULING)

    async def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        raise ConfigurationError(_NO_REMOTE_HUMAN)

    async def respond(
        self, run_id: str, subject: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        # This one *does* work remotely: answering is an ordinary event.
        await self.client.send_event(run_id, f"approval:{subject}", answer)
        return await self.client.get(run_id) or {}

    async def author(
        self,
        spec: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
        turns: int | None = None,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        resume: str = "",
    ) -> dict[str, Any]:
        raise ConfigurationError(_NO_REMOTE_AUTHORING)

    async def pause(self, run_id: str) -> dict[str, Any]:
        raise ConfigurationError(
            "pausing a run is not exposed over HTTP yet — hold it from the "
            "process that owns the store, or add the route to your own server."
        )

    async def unpause(self, run_id: str) -> dict[str, Any]:
        raise ConfigurationError(
            "releasing a paused run is not exposed over HTTP yet — the "
            "underlying event is: send 'resume:<run_id>' to it."
        )

    async def pin(self, run_id: str, *, module: str = "") -> dict[str, Any]:

        record = await self.client.get(run_id)
        if record is None:
            raise RegistryError(f"no run {run_id}")
        journal = await self.client.journal(run_id)
        # Works remotely: everything it needs is in the journal the server
        # already serves, and the generation itself is local text assembly.
        return _pinned_payload(run_id, record, journal, module)

    async def edit(
        self,
        source: str,
        instruction: str,
        *,
        packages: list[str] | None = None,
        smoke_input: Any = None,
        observe: bool = True,
    ) -> dict[str, Any]:
        # Refused for the reason authoring is: editing reads *this* process's
        # toolsets, nodes and probes to decide what the workflow may call, and
        # spends model tokens doing it. A server's key would be spending
        # somebody else's budget on a decision made against the wrong catalogue.
        raise ConfigurationError(_NO_REMOTE_AUTHORING)

    async def nodes(
        self, query: str = "", *, category: str | None = None
    ) -> list[dict[str, Any]]:
        raise ConfigurationError(_NO_REMOTE_NODES)

    async def node(self, node_id: str) -> dict[str, Any]:
        raise ConfigurationError(_NO_REMOTE_NODES)

    async def list_artifacts(self) -> list[dict[str, Any]]:
        return await self.client.list_artifacts()

    async def artifact_history(self, name: str) -> list[dict[str, Any]]:
        return await self.client.artifact_history(name)

    async def artifact_url(
        self, name: str, version: int | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        return await self.client.artifact_url(name, version, expires_in)

    async def read_artifact(
        self, name: str, version: int | None = None
    ) -> dict[str, Any]:
        return await self.client.read_artifact(name, version)

    async def put_artifact(
        self,
        name: str,
        content_b64: str,
        *,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.put_artifact(
            name, content_b64, mime=mime, metadata=metadata
        )

    async def upload_url(
        self,
        name: str,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        return await self.client.upload_url(
            name, mime=mime, max_size=max_size, expires_in=expires_in
        )

    async def confirm_upload(
        self,
        upload_id: str,
        name: str,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.confirm_upload(
            upload_id, name, run_id=run_id, metadata=metadata
        )

    async def read_blob(
        self, ref: str, expires: int, signature: str, method: str = "GET"
    ) -> dict[str, Any]:
        return await self.client.read_blob(ref, expires, signature, method)

    async def write_blob(
        self,
        ref: str,
        expires: int,
        signature: str,
        content_b64: str,
        mime: str = "application/octet-stream",
        method: str = "PUT",
    ) -> dict[str, Any]:
        return await self.client.write_blob(
            ref, expires, signature, content_b64, mime=mime, method=method
        )

    async def publish(self, workflow: str) -> dict[str, Any]:
        raise ConfigurationError(
            "publishing runs where the code is. Drop --server and run it against "
            "the module that defines the workflow."
        )

    async def close(self) -> None:
        await self.client.close()


def _react_flow(graph: Any, *, trace: Any = None) -> dict[str, Any]:
    """The React Flow projection, laid out. Shared by ``graph`` and ``trace``.

    One helper so the two never drift on direction or layout — a canvas and a
    run inspector showing the same workflow with different geometry is the kind
    of difference nobody reports and everybody notices.
    """
    from loom.graph.reactflow import to_react_flow

    return to_react_flow(graph, trace=trace)

def _describe_version(version: Any, *, active: bool) -> dict[str, Any]:
    """One version as plain JSON. ``active`` is the question a control plane asks.

    ``content_hash`` and ``code_hash`` are both carried because they answer
    different questions: the first identifies the source a human committed, the
    second is what a finished run records — and dropping either leaves "show me
    this version's source" or "which version produced this run" unanswerable.
    """
    return {
        "workflow": version.workflow,
        "version": version.version,
        "parent_version": version.parent_version,
        "content_hash": version.content_hash,
        "code_hash": version.code_hash,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "active": active,
    }


def _pinned_payload(
    run_id: str, record: Any, journal: Any, module: str
) -> dict[str, Any]:
    """Shared rendering for a pinned test, whatever served the journal.

    The remote facade gets JSON rows where the local one gets models, so the
    rows are coerced here rather than in either adapter — a second copy of this
    is how the two surfaces drift into different tests for the same run.
    """
    from loom.runtime.journal import JournalEntry
    from loom.testing.pin import pin_run

    entries = [
        row if isinstance(row, JournalEntry) else JournalEntry.model_validate(row)
        for row in journal
    ]
    described = (
        record
        if not isinstance(record, dict)
        else type("Row", (), {
            "run_id": record.get("run_id", run_id),
            "workflow": record.get("workflow", "workflow"),
            "input": record.get("input"),
            "status": type("S", (), {"value": record.get("status", "unknown")})(),
        })()
    )
    pinned = pin_run(described, entries, module=module)
    return {
        "run_id": run_id,
        "workflow": getattr(described, "workflow", "workflow"),
        "source": pinned.source,
        "filename": pinned.filename,
        "seeded": pinned.seeded,
        "notes": pinned.notes,
    }
