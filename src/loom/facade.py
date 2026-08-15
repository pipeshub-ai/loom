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
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.core.exceptions import ConfigurationError, RegistryError
from loom.core.ids import new_id
from loom.core.models import ExecutionStatus, StepStatus, TriggerKind, TriggerRecord
from loom.core.serde import decode, encode

__all__ = [
    "LocalFacade",
    "RemoteFacade",
    "RuntimeFacade",
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

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        """The run's durable operations, in order, keyed by ``step_id``."""
        ...

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        """What the run has said about itself, from *offset* onward.

        Distinct from the journal: the journal is what a run durably *did*, and
        this is what it chose to narrate while doing it. A four-minute step is
        one journal entry and can be a dozen reports.
        """
        ...

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        """Deliver an event, resuming the run if it was parked on it."""
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


def describe_record(record: Any) -> dict[str, Any]:
    """Render an :class:`ExecutionRecord` as the wire shape."""

    return {
        "run_id": record.run_id,
        "workflow": record.workflow,
        "status": record.status.value,
        "input": decode(record.input),
        "output": decode(record.output),
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


def describe_artifact(version: Any) -> dict[str, Any]:
    """Render an :class:`ArtifactVersion` as the wire shape."""
    return version.model_dump(mode="json")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class LocalFacade:
    """Drives an in-process :class:`Runtime`.

    The store comes from ``$LOOM_STORE`` via ``Runtime.from_env()``, so the same
    adapter reads memory in a test, SQLite on a laptop, and Postgres in
    production without changing.
    """

    runtime: Any
    loaded: list[str] = field(default_factory=list)
    """Module specs that were imported to populate this Runtime."""
    _trigger_dispatcher: Any = field(default=None, repr=False, compare=False)
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
        return None if record is None else describe_record(record)

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:

        records = await self.runtime.list_runs(
            workflow=workflow,
            status=ExecutionStatus(status) if status else None,
            limit=limit,
        )
        return [describe_record(record) for record in records]

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        entries = await self.runtime.history(run_id)
        return [describe_entry(entry) for entry in entries]

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

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        await self.runtime.send_event(run_id, name, payload)
        # Deliver it: in process, an event only advances the run if something
        # re-enters the body, and nothing else is driving it here.
        await self.runtime.resume(run_id)

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

        tickets: dict[str, Any] = {}
        for entry in entries:
            if entry.kind == "step" and entry.name.startswith("deliver:"):
                try:
                    ticket = HumanTicket.model_validate(entry.output)
                except Exception:
                    continue
                tickets[ticket.request.subject] = ticket

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

    async def nodes(
        self, query: str = "", *, category: str | None = None
    ) -> list[dict[str, Any]]:
        cards = self.runtime.nodes.search(query or "", category=category, limit=200)
        return [card.model_dump(mode="json") for card in cards]

    async def node(self, node_id: str) -> dict[str, Any]:
        detail = self.runtime.nodes.show(node_id).model_dump(mode="json")
        detail["contract"] = self.runtime.nodes.contract(node_id)
        return detail

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
        session = await urls.create_upload_session(
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
_NO_REMOTE_CREDENTIALS = (
    "credentials= cannot be sent over HTTP — a live token would leave the "
    "process. Run 'loom connect <name>' on the server, or start the run "
    "in-process."
)


@dataclass
class RemoteFacade:
    """Drives a running LOOM server through :class:`LoomClient`."""

    client: Any

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

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        # An older server may predate ``describe_entry`` and send only "name".
        return [
            {**entry, "step_id": entry.get("step_id") or entry.get("name", "")}
            for entry in await self.client.journal(run_id)
        ]

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        return await self.client.reports(run_id, offset=offset)

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        await self.client.send_event(run_id, name, payload)

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
