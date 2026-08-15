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

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from workflow_builder.core.exceptions import ConfigurationError, RegistryError

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

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


def describe_record(record: Any) -> dict[str, Any]:
    """Render an :class:`ExecutionRecord` as the wire shape."""
    from workflow_builder.core.serde import decode

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
        "metadata": dict(record.metadata),
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
    from workflow_builder.core.serde import encode

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
    from workflow_builder.core.serde import decode

    return {
        "run_id": result.run_id,
        "workflow": result.workflow,
        "status": result.status.value,
        "output": decode(result.output),
        "error": result.error.message if result.error else None,
    }


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


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
    ) -> dict[str, Any]:
        if wait:
            result = await self.runtime.run(
                workflow,
                payload,
                idempotency_key=idempotency_key,
                tags=tags or [],
                metadata=metadata or {},
            )
            run_id = result.run_id
        else:
            run_id = await self.runtime.submit(
                workflow,
                payload,
                idempotency_key=idempotency_key,
                tags=tags or [],
                metadata=metadata or {},
            )
        found = await self.get(run_id)
        return found or {"run_id": run_id, "status": "pending", "workflow": workflow}

    async def get(self, run_id: str) -> dict[str, Any] | None:
        record = await self.runtime.get(run_id)
        return None if record is None else describe_record(record)

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        from workflow_builder.core.models import ExecutionStatus

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

    def _dispatcher(self) -> Any:
        """One dispatcher per Runtime, made on demand.

        Cached on the facade rather than on the Runtime: scheduling is a
        control-plane concern, and the manager tools used to reach in and set
        ``runtime._dispatcher`` themselves — a private attribute on somebody
        else's object, which is what a missing boundary looks like.
        """
        from workflow_builder.runtime.dispatcher import TriggerDispatcher

        if self._trigger_dispatcher is None:
            self._trigger_dispatcher = TriggerDispatcher(self.runtime)
        return self._trigger_dispatcher

    async def schedules(self, workflow: str | None = None) -> list[dict[str, Any]]:
        records = await self._dispatcher()._store.list_triggers(workflow=workflow)
        return [record.model_dump(mode="json") for record in records]

    async def schedule(
        self, workflow: str, cron: str, *, timezone: str = "UTC"
    ) -> dict[str, Any]:
        from workflow_builder.core.ids import new_id
        from workflow_builder.core.models import TriggerKind, TriggerRecord
        from workflow_builder.triggers.specs import Schedule

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
    ) -> dict[str, Any]:
        return await self.client.start(
            workflow,
            payload,
            idempotency_key=idempotency_key,
            tags=tags,
            metadata=metadata,
            wait=wait,
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        from workflow_builder.server.client import LoomClientError

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

    async def publish(self, workflow: str) -> dict[str, Any]:
        raise ConfigurationError(
            "publishing runs where the code is. Drop --server and run it against "
            "the module that defines the workflow."
        )

    async def close(self) -> None:
        await self.client.close()
