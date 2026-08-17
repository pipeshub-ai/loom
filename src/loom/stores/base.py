"""Storage protocols.

The engine talks to storage only through these, so the same workflow code runs against an
in-memory store in tests, SQLite on a laptop, and Postgres or Temporal in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry


def as_utc(value: datetime) -> datetime:
    """*value* as an aware UTC datetime, reading a naive one as UTC.

    The instant-comparing backends need this for the same reason the
    text-comparing ones need :func:`utc_iso`: a stored timestamp and the ``now``
    it is compared against must be the same kind. Memory compared them directly
    and raised ``TypeError: can't compare offset-naive and offset-aware
    datetimes``; Postgres handed a naive value to a ``timestamptz`` column and
    quietly returned the wrong set. Four backends, four answers, for an input
    every one of them accepts.

    Reading naive as UTC rather than as local time matches
    :meth:`Context.sleep_until`, which is where a wake time normally comes from.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def utc_iso(value: datetime | None) -> str | None:
    """ISO-8601 for *value*, normalised to UTC first.

    The backends that keep timestamps as TEXT — SQLite and Mongo — answer
    ``wake_at <= now`` and ``next_fire_at <= now`` by comparing those strings
    lexicographically. That is only the same question as comparing the instants
    when every string carries the same offset. It does not:
    ``2026-08-17T15:00:00+05:30`` is *earlier* than ``2026-08-17T12:00:00+00:00``
    as an instant and *later* as a string, so a run parked on a tz-aware
    non-UTC wake time is never found by ``due_runs`` and sleeps forever — with
    no error, and looking exactly like a run that is patiently waiting.

    Postgres and Memory compare real instants and are unaffected, which is what
    made this invisible: the divergence only appears on two of the four
    backends, and the conformance suite built every timestamp from
    ``datetime.now(UTC)``, where the two answers agree.

    A naive datetime is read as UTC, matching :meth:`Context.sleep_until`.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


@runtime_checkable
class ExecutionStore(Protocol):
    """Durable home for execution headers, journals, buffered events, and timers."""

    # -- executions -------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None: ...

    async def get_execution(self, run_id: str) -> ExecutionRecord | None: ...

    async def update_execution(
        self, record: ExecutionRecord, *, expected_status: ExecutionStatus | None = None
    ) -> None:
        """Persist *record*.

        When `expected_status` is given, the write is conditional: it must
        raise :class:`~loom.core.exceptions.ConcurrentUpdateError` rather
        than write if the currently-stored record's status is not
        `expected_status`. This is what lets the engine detect two processes
        both resuming the same suspended run from the same delivered event
        — the loser must not overwrite a journal the winner is concurrently
        appending to.
        """
        ...

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

    async def claim_event_delivery(self, key: str, *, ttl_seconds: float = 604800.0) -> bool:
        """Claim *key* for the first caller. ``False`` for every caller after.

        Kafka, Redis Streams, SQS — every event bus worth using is at-least-once,
        so a redelivered message must not resume a run a second time.
        ``submit()`` has had an idempotency key since the beginning and events
        did not, so the *trigger* path was protected and the *event* path was
        not.

        **Atomic, or it is nothing.** A read-then-write in the engine lets two
        consumers both observe the key as unclaimed, which is the exact race
        this prevents — so the claim belongs in the store, where a unique index
        or an INSERT can enforce it, and the conformance suite runs it
        concurrently on every backend.

        *ttl_seconds* bounds how long the memory lasts. A week by default:
        long enough that no realistic redelivery slips past, short enough that
        the table does not grow without limit.
        """
        ...

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

    async def claim_due_triggers(
        self,
        now: datetime,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        limit: int = 50,
    ) -> list[TriggerRecord]:
        """Take ownership of due triggers, returning only the ones won here.

        ``due_triggers`` reads; this one *takes*. The difference matters because
        the dispatcher's sequence is read, submit, advance — so two dispatchers
        reading the same due row both act on it. The occurrence key already
        makes that harmless (one run, not two), which leaves this doing the
        other half of the job: stopping two processes from doing the same work,
        and from both advancing one record.

        Three properties every implementation must have:

        - **Exclusive.** Two concurrent callers never both receive one record.
        - **Leased, not locked.** A claim lapses after *lease_seconds*, so a
          dispatcher that dies mid-tick delays one occurrence rather than
          stranding the trigger forever.
        - **Does not advance.** Claiming is not firing. ``update_after_fire``
          stays the only thing that moves ``next_fire_at``, so a claim that is
          never acted on simply expires and the occurrence comes back.
        """
        ...

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None: ...

    async def delete_trigger(self, trigger_id: str) -> None: ...


@runtime_checkable
class IndexedScans(Protocol):
    """Predicate-pushdown reads, kept **out** of :class:`ExecutionStore` on purpose.

    Both queries here filter on a field that used to live inside the record's
    JSON payload, where no index reaches it. Filtering after a
    ``list_executions`` page therefore meant filtering after a *newest-first*
    page — and the records both callers want are the oldest ones, so past a few
    hundred live runs neither ever saw them and both reported "nothing to do".
    Migration 1 lifts the two fields into indexed columns; these are how the
    engine asks for them.

    This is a **separate protocol** because a ``runtime_checkable`` one is
    all-or-nothing: adding a method to ``ExecutionStore`` would make every
    host's existing store fail ``isinstance`` and stop being an execution store
    at all. A capability nobody had yesterday must not be able to invalidate
    what shipped. Callers probe with ``getattr(store, "due_leases", None)`` and
    fall back to paging ``list_executions`` to exhaustion, which is correct —
    only linear.
    """

    async def due_leases(
        self,
        before: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """Unfinished runs whose lease expired before *before*, oldest first.

        What ``reclaim_orphans`` acts on: a worker died holding these, and no
        timer covers them, because they are not waiting for one.
        """
        ...

    async def terminal_before(
        self,
        cutoff: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """Terminal runs that finished before *cutoff*, oldest first.

        What retention compacts.
        """
        ...
