"""Durability backend abstraction.

The ``DurabilityBackend`` protocol is the single integration point between the
orchestration engine and whatever stores durable state.  The embedded backend
wraps ``ExecutionStore`` + ``Journal``; external backends (Temporal, DBOS,
Restate) implement the same protocol against a remote API.

This separation is a one-way door: once the engine talks only to
``DurabilityBackend``, adding a new backend is a new class, not a fork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from workflow_builder.core.models import Event, ExecutionRecord, ExecutionStatus
from workflow_builder.runtime.journal import JournalEntry

# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


class Capability(StrEnum):
    """Features that a backend may or may not support."""

    JOURNAL = "journal"
    """Append-only operation log with deterministic replay."""
    TIMERS = "timers"
    """Durable sleep / wake-at scheduling."""
    EVENTS = "events"
    """Buffered event delivery and await."""
    CHILD_WORKFLOWS = "child_workflows"
    """Hierarchical workflow composition."""
    CONTINUE_AS_NEW = "continue_as_new"
    """Terminate + restart with fresh history to bound journal size."""
    KV = "kv"
    """Per-run key-value state accessible from steps."""
    SIGNALS = "signals"
    """Cross-run signaling."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this backend can do."""

    supported: frozenset[Capability] = field(default_factory=frozenset)
    max_journal_size: int | None = None
    """Advisory limit on journal entries before ``continue_as_new`` is recommended."""

    def has(self, cap: Capability) -> bool:
        return cap in self.supported

    def require(self, cap: Capability, backend_name: str) -> None:
        """Raise ``BackendCapabilityError`` if the capability is missing."""
        if cap not in self.supported:
            from workflow_builder.core.exceptions import BackendCapabilityError

            raise BackendCapabilityError(
                f"'{backend_name}' does not support {cap.value}",
                capability=cap.value,
                backend=backend_name,
            )


# ---------------------------------------------------------------------------
# Reference types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRef:
    """Minimal reference to an in-flight execution."""

    run_id: str
    workflow: str
    version: int = 1


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DurabilityBackend(Protocol):
    """The single integration point for any durable backend.

    Implementations range from the embedded engine (wraps ``ExecutionStore`` +
    ``Journal``) to Temporal or DBOS adapters that delegate to a remote server.
    """

    @property
    def name(self) -> str:
        """Human-readable backend identifier (e.g. ``"embedded"``, ``"temporal"``)."""
        ...

    def capabilities(self) -> Capabilities:
        """Declare what this backend supports so the engine can fail fast."""
        ...

    # -- execution lifecycle ----------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None: ...

    async def get_execution(self, run_id: str) -> ExecutionRecord | None: ...

    async def update_execution(self, record: ExecutionRecord) -> None: ...

    async def list_executions(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionRecord]: ...

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None: ...

    # -- journal ----------------------------------------------------------------------

    async def save_journal(self, run_id: str, entries: list[JournalEntry]) -> None: ...

    async def load_journal(self, run_id: str) -> list[JournalEntry]: ...

    async def truncate_journal(self, run_id: str, from_path: str) -> None: ...

    # -- events -----------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None: ...

    async def take_event(self, run_id: str, name: str) -> Event | None: ...

    async def runs_awaiting_event(self, name: str) -> list[str]: ...

    # -- timers -----------------------------------------------------------------------

    async def due_runs(self, now: datetime, *, limit: int = 100) -> list[str]: ...


# ---------------------------------------------------------------------------
# Embedded implementation
# ---------------------------------------------------------------------------


class EmbeddedBackend:
    """The built-in durable backend: wraps ``ExecutionStore`` for journal + state.

    This is what you get when you ``pip install workflow-builder`` and pass
    ``MemoryStore`` or ``SQLiteStore``. No external infrastructure required.
    """

    def __init__(self, store: Any) -> None:
        from workflow_builder.state.base import ExecutionStore

        if not isinstance(store, ExecutionStore):
            raise TypeError(
                f"EmbeddedBackend requires an ExecutionStore, got {type(store).__name__}"
            )
        self._store = store

    @property
    def name(self) -> str:
        return "embedded"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supported=frozenset(
                {
                    Capability.JOURNAL,
                    Capability.TIMERS,
                    Capability.EVENTS,
                    Capability.CHILD_WORKFLOWS,
                    Capability.SIGNALS,
                }
            ),
            max_journal_size=10_000,
        )

    # -- execution lifecycle ----------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        await self._store.create_execution(record)

    async def get_execution(self, run_id: str) -> ExecutionRecord | None:
        return await self._store.get_execution(run_id)

    async def update_execution(self, record: ExecutionRecord) -> None:
        await self._store.update_execution(record)

    async def list_executions(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        return await self._store.list_executions(
            workflow=workflow, status=status, limit=limit, offset=offset
        )

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        return await self._store.find_by_idempotency_key(key)

    # -- journal ----------------------------------------------------------------------

    async def save_journal(self, run_id: str, entries: list[JournalEntry]) -> None:
        await self._store.save_journal(run_id, entries)

    async def load_journal(self, run_id: str) -> list[JournalEntry]:
        return await self._store.load_journal(run_id)

    async def truncate_journal(self, run_id: str, from_path: str) -> None:
        await self._store.truncate_journal(run_id, from_path)

    # -- events -----------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        await self._store.enqueue_event(event)

    async def take_event(self, run_id: str, name: str) -> Event | None:
        return await self._store.take_event(run_id, name)

    async def runs_awaiting_event(self, name: str) -> list[str]:
        return await self._store.runs_awaiting_event(name)

    # -- timers -----------------------------------------------------------------------

    async def due_runs(self, now: datetime, *, limit: int = 100) -> list[str]:
        return await self._store.due_runs(now, limit=limit)
