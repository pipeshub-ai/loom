"""Storage protocols.

The engine talks to storage only through these, so the same workflow code runs against an
in-memory store in tests, SQLite on a laptop, and Postgres or Temporal in production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from workflow_builder.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from workflow_builder.runtime.journal import JournalEntry


@runtime_checkable
class ExecutionStore(Protocol):
    """Durable home for execution headers, journals, buffered events, and timers."""

    # -- executions -------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None: ...

    async def get_execution(self, run_id: str) -> ExecutionRecord | None: ...

    async def update_execution(self, record: ExecutionRecord) -> None: ...

    async def list_executions(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        """Query execution history. Metadata filtering is what makes runs findable."""
        ...

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None: ...

    async def delete_execution(self, run_id: str) -> None:
        """Remove an execution and its journal. Used by retention compaction."""
        ...

    # -- journals ---------------------------------------------------------------------

    async def save_journal(self, run_id: str, entries: list[JournalEntry]) -> None:
        """Upsert journal entries by sequence number."""
        ...

    async def load_journal(self, run_id: str) -> list[JournalEntry]: ...

    async def truncate_journal(self, run_id: str, from_path: str) -> None:
        """Drop entries at or after ``from_path``; used by retry-from-failure.

        Paths sort lexicographically in journal order, so a string comparison is
        the same cut a sequence number would make.
        """
        ...

    # -- events -----------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        """Buffer an event, including ones that arrive before the run waits for them."""
        ...

    async def take_event(self, run_id: str, name: str) -> Event | None:
        """Atomically consume the oldest matching buffered event."""
        ...

    async def runs_awaiting_event(self, name: str) -> list[str]: ...

    # -- timers -----------------------------------------------------------------------

    async def due_runs(self, now: datetime, *, limit: int = 100) -> list[str]:
        """Suspended runs whose wake time has passed."""
        ...


@runtime_checkable
class CacheStore(Protocol):
    """Cross-run memoization for steps declaring a :class:`CachePolicy`.

    Also the substrate for anything else keyed and durable — agent sessions and
    the artifact index both live here rather than needing their own storage.
    """

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store ``value`` under ``key``.

        A ``ttl_seconds`` of zero or less means **no expiry**. Reading it as
        "expires immediately" would make ``set(key, value, 0)`` a silent no-op,
        which is never what a caller means.
        """
        ...

    async def delete(self, key: str) -> None: ...


@runtime_checkable
class LockProvider(Protocol):
    """Distributed mutual exclusion, used for leader election and run leases."""

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool: ...

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool: ...

    async def release(self, key: str, owner: str) -> None: ...


@runtime_checkable
class TriggerStore(Protocol):
    """Persists trigger state for the TriggerDispatcher.

    Tracks when each cron/interval trigger last fired and when it
    should fire next. Implementations: in-memory (default), SQLite,
    MongoDB, PostgreSQL.
    """

    async def save_trigger(self, trigger: TriggerRecord) -> None: ...

    async def get_trigger(self, trigger_id: str) -> TriggerRecord | None: ...

    async def list_triggers(
        self, *, workflow: str | None = None
    ) -> list[TriggerRecord]: ...

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]: ...

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None: ...

    async def delete_trigger(self, trigger_id: str) -> None: ...
