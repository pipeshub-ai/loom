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

from workflow_builder.core.exceptions import ConfigurationError

__all__ = [
    "LocalFacade",
    "RemoteFacade",
    "RuntimeFacade",
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

    async def workflows(self) -> list[dict[str, Any]]:
        """Available workflows, each with ``name``/``version``/``executable``."""
        ...

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
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

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        """Deliver an event, resuming the run if it was parked on it."""
        ...

    async def cancel(self, run_id: str) -> dict[str, Any]: ...

    async def retry(self, run_id: str) -> dict[str, Any]: ...

    async def replay(self, run_id: str) -> dict[str, Any]: ...

    async def publish(self, workflow: str) -> dict[str, Any]:
        """Record a workflow in the durable catalog."""
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

    async def workflows(self) -> list[dict[str, Any]]:
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
        for record in await self.runtime.published():
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
        wait: bool = True,
    ) -> dict[str, Any]:
        if wait:
            result = await self.runtime.run(
                workflow, payload, idempotency_key=idempotency_key
            )
            run_id = result.run_id
        else:
            run_id = await self.runtime.submit(
                workflow, payload, idempotency_key=idempotency_key
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
        return [
            {
                "seq": entry.seq,
                "step_id": entry.name,
                "kind": entry.kind,
                "status": entry.status.value,
                "attempts": entry.attempts,
                "error": entry.error.message if entry.error else None,
            }
            for entry in await self.runtime.history(run_id)
        ]

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

    async def close(self) -> None:
        await self.runtime.shutdown()
        store_close = getattr(self.runtime.store, "close", None)
        if store_close is not None:
            await store_close()


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------


@dataclass
class RemoteFacade:
    """Drives a running LOOM server through :class:`LoomClient`."""

    client: Any

    async def workflows(self) -> list[dict[str, Any]]:
        return await self.client.workflows()

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        return await self.client.start(
            workflow, payload, idempotency_key=idempotency_key, wait=wait
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
        # The HTTP route names the step "name"; normalise to the local shape so
        # callers see one journal format regardless of which facade they hold.
        return [
            {**entry, "step_id": entry.get("step_id") or entry.get("name", "")}
            for entry in await self.client.journal(run_id)
        ]

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        await self.client.send_event(run_id, name, payload)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self.client.cancel(run_id)

    async def retry(self, run_id: str) -> dict[str, Any]:
        return await self.client.retry(run_id)

    async def replay(self, run_id: str) -> dict[str, Any]:
        return await self.client.replay(run_id)

    async def publish(self, workflow: str) -> dict[str, Any]:
        raise ConfigurationError(
            "publishing runs where the code is. Drop --server and run it against "
            "the module that defines the workflow."
        )

    async def close(self) -> None:
        await self.client.close()
