"""In-memory store: the default for tests and single-process development."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loom.core.exceptions import ConcurrentUpdateError, ValidationError
from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry, path_order
from loom.stores.base import as_utc


def _by_time(item: tuple[datetime, str, Any]) -> tuple[datetime, str]:
    """Order by timestamp, then run id — so ties are stable rather than arbitrary."""
    return item[0], item[1]


class MemoryStore:
    """Implements :class:`ExecutionStore`, :class:`CacheStore`, and :class:`LockProvider`.

    Everything is deep-copied on the way in and out so callers cannot mutate stored state
    by accident — the same isolation a real database would give, which keeps tests honest
    about what actually got persisted.
    """

    def __init__(self) -> None:
        self._executions: dict[str, ExecutionRecord] = {}
        self._journals: dict[str, dict[str, JournalEntry]] = defaultdict(dict)
        self._events: dict[tuple[str, str], deque[Event]] = defaultdict(deque)
        self._delivered: dict[str, float] = {}
        """Claimed event-delivery keys, to their expiry. Bounded by a sweep on
        write rather than a timer: the map only grows on delivery."""
        self._cache: dict[str, tuple[float | None, Any]] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._idempotency: dict[str, str] = {}
        self._triggers: dict[str, TriggerRecord] = {}
        self._mutex = asyncio.Lock()

    # -- executions -------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        async with self._mutex:
            if record.idempotency_key:
                won = self._idempotency.setdefault(
                    record.idempotency_key, record.run_id
                )
                if won != record.run_id:
                    # Refuse, the way every persistent store does: the key is
                    # UNIQUE in SQLite and Postgres and a unique partial index
                    # in Mongo. Absorbing it here instead — keeping the first
                    # id in the index while storing the second record anyway —
                    # left the store holding two runs for one key, so a
                    # scheduled occurrence that was correctly deduplicated
                    # everywhere else fired twice on the default store.
                    raise ValidationError(
                        f"idempotency key {record.idempotency_key!r} already "
                        f"belongs to run {won}"
                    )
            self._executions[record.run_id] = record.model_copy(deep=True)

    async def get_execution(self, run_id: str) -> ExecutionRecord | None:
        async with self._mutex:
            found = self._executions.get(run_id)
            return found.model_copy(deep=True) if found else None

    async def update_execution(
        self, record: ExecutionRecord, *, expected_status: ExecutionStatus | None = None
    ) -> None:
        async with self._mutex:
            if expected_status is not None:
                current = self._executions.get(record.run_id)
                actual = current.status if current is not None else None
                if actual != expected_status:
                    raise ConcurrentUpdateError(
                        record.run_id,
                        expected=expected_status.value,
                        actual=actual.value if actual is not None else None,
                    )
            self._executions[record.run_id] = record.model_copy(deep=True)

    async def delete_execution(self, run_id: str) -> None:
        async with self._mutex:
            record = self._executions.pop(run_id, None)
            self._journals.pop(run_id, None)
            if record is not None and record.idempotency_key:
                # Otherwise the key would keep resolving to a run that is gone.
                self._idempotency.pop(record.idempotency_key, None)

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
        async with self._mutex:
            rows = sorted(self._executions.values(), key=lambda r: r.run_id, reverse=True)

        def keep(record: ExecutionRecord) -> bool:
            if workflow is not None and record.workflow != workflow:
                return False
            if status is not None and record.status is not status:
                return False
            if tags and not set(tags).issubset(record.tags):
                return False
            return not (
                metadata and any(record.metadata.get(k) != v for k, v in metadata.items())
            )

        return [r.model_copy(deep=True) for r in rows if keep(r)][offset : offset + limit]

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        async with self._mutex:
            run_id = self._idempotency.get(key)
            if run_id is None:
                return None
            found = self._executions.get(run_id)
            return found.model_copy(deep=True) if found else None

    # -- journals ---------------------------------------------------------------------

    async def save_journal(self, run_id: str, entries: list[JournalEntry]) -> None:
        async with self._mutex:
            target = self._journals[run_id]
            for entry in entries:
                target[entry.path] = entry.model_copy(deep=True)

    async def load_journal(self, run_id: str) -> list[JournalEntry]:
        async with self._mutex:
            target = self._journals.get(run_id, {})
            ordered = sorted(target, key=path_order)
            return [target[path].model_copy(deep=True) for path in ordered]

    async def truncate_journal(self, run_id: str, from_path: str) -> None:
        async with self._mutex:
            target = self._journals.get(run_id, {})
            boundary = path_order(from_path)
            for path in [p for p in target if path_order(p) >= boundary]:
                del target[path]

    # -- events -----------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        async with self._mutex:
            event = event.model_copy(deep=True)
            event.received_at = event.received_at or datetime.now(UTC)
            self._events[(event.run_id or "", event.name)].append(event)

    async def claim_event_delivery(
        self, key: str, *, ttl_seconds: float = 604800.0
    ) -> bool:
        async with self._mutex:
            now = time.time()
            # Sweep here rather than on a timer: the map only grows on delivery,
            # so the cheapest place to bound it is the same call.
            self._delivered = {
                claimed: expiry
                for claimed, expiry in self._delivered.items()
                if expiry > now
            }
            if key in self._delivered:
                return False
            self._delivered[key] = now + ttl_seconds
            return True

    async def take_event(self, run_id: str, name: str) -> Event | None:
        async with self._mutex:
            queue = self._events.get((run_id, name))
            if queue:
                return queue.popleft()
            broadcast = self._events.get(("", name))
            if broadcast:
                return broadcast.popleft()
            return None

    async def runs_awaiting_event(self, name: str) -> list[str]:
        async with self._mutex:
            return [
                record.run_id
                for record in self._executions.values()
                if record.status is ExecutionStatus.SUSPENDED and record.awaiting_event == name
            ]

    # -- timers -----------------------------------------------------------------------

    async def due_runs(self, now: datetime, *, limit: int = 100) -> list[str]:
        # Both sides normalised, as due_triggers already does: a naive wake_at
        # against an aware `now` raises TypeError, which turns a caller passing
        # `datetime(2026, 8, 17, 9, 30)` into a crashed scheduler tick rather
        # than a woken run. See loom.stores.base.as_utc.
        now = as_utc(now)
        async with self._mutex:
            due = [
                (as_utc(record.wake_at), record.run_id)
                for record in self._executions.values()
                if record.status is ExecutionStatus.SUSPENDED
                and record.wake_at is not None
                and as_utc(record.wake_at) <= now
            ]
        # Ordered by wake time, not by run_id: callers take the head of this
        # list, so the run that has been waiting longest has to come first.
        return [run_id for _wake, run_id in sorted(due)][:limit]

    async def due_leases(
        self,
        before: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        before = as_utc(before)
        wanted = set(statuses)
        async with self._mutex:
            found = [
                (as_utc(r.lease_expires_at), r.run_id, r)
                for r in self._executions.values()
                if r.status in wanted
                and r.lease_expires_at is not None
                and as_utc(r.lease_expires_at) <= before
            ]
        return [r.model_copy(deep=True) for _at, _id, r in sorted(found, key=_by_time)][:limit]

    async def terminal_before(
        self,
        cutoff: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        cutoff = as_utc(cutoff)
        wanted = set(statuses)
        found = []
        async with self._mutex:
            for r in self._executions.values():
                if r.status not in wanted:
                    continue
                # created_at stands in for a record that never recorded a
                # finish — the same fallback the caller used to apply itself.
                finished = r.finished_at or r.created_at
                if finished is not None and as_utc(finished) < cutoff:
                    found.append((as_utc(finished), r.run_id, r))
        return [r.model_copy(deep=True) for _at, _id, r in sorted(found, key=_by_time)][:limit]

    # -- cache ------------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        # A None expiry means the entry never goes stale — see CacheStore.set.
        if expires_at is not None and expires_at < time.time():
            self._cache.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
        self._cache[key] = (expires_at, value)

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    # -- locks ------------------------------------------------------------------------

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        async with self._mutex:
            held = self._locks.get(key)
            if held is not None and held[0] != owner and held[1] > time.time():
                return False
            self._locks[key] = (owner, time.time() + ttl_seconds)
            return True

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool:
        async with self._mutex:
            held = self._locks.get(key)
            if held is None or held[0] != owner:
                return False
            self._locks[key] = (owner, time.time() + ttl_seconds)
            return True

    async def release(self, key: str, owner: str) -> None:
        async with self._mutex:
            held = self._locks.get(key)
            if held is not None and held[0] == owner:
                del self._locks[key]

    # -- TriggerStore -----------------------------------------------------------------

    async def save_trigger(self, trigger: TriggerRecord) -> None:
        async with self._mutex:
            self._triggers[trigger.trigger_id] = trigger.model_copy(deep=True)

    async def get_trigger(self, trigger_id: str) -> TriggerRecord | None:
        async with self._mutex:
            t = self._triggers.get(trigger_id)
            return t.model_copy(deep=True) if t else None

    async def list_triggers(
        self, *, workflow: str | None = None
    ) -> list[TriggerRecord]:
        async with self._mutex:
            results = list(self._triggers.values())
            if workflow is not None:
                results = [t for t in results if t.workflow == workflow]
            return [t.model_copy(deep=True) for t in results]

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]:
        # Ensure timezone-aware comparison
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        async with self._mutex:
            due = []
            for t in self._triggers.values():
                if not t.enabled or t.next_fire_at is None:
                    continue
                fire_at = t.next_fire_at
                if fire_at.tzinfo is None:
                    fire_at = fire_at.replace(tzinfo=UTC)
                if fire_at <= now:
                    due.append(t.model_copy(deep=True))
            due.sort(key=lambda t: t.next_fire_at or now)
            return due[:limit]

    async def claim_due_triggers(
        self,
        now: datetime,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        limit: int = 50,
    ) -> list[TriggerRecord]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        until = now + timedelta(seconds=lease_seconds)
        async with self._mutex:
            # One critical section for select-and-mark: the whole point is that
            # no other caller can observe a due trigger between the two.
            won: list[TriggerRecord] = []
            for trigger in sorted(
                self._triggers.values(), key=lambda t: t.next_fire_at or now
            ):
                if len(won) >= limit:
                    break
                if not trigger.enabled or trigger.next_fire_at is None:
                    continue
                fire_at = trigger.next_fire_at
                if fire_at.tzinfo is None:
                    fire_at = fire_at.replace(tzinfo=UTC)
                if fire_at > now:
                    continue
                held = trigger.claimed_until
                if held is not None:
                    if held.tzinfo is None:
                        held = held.replace(tzinfo=UTC)
                    if held > now:
                        continue
                claimed = trigger.model_copy(
                    update={"claimed_by": owner, "claimed_until": until}
                )
                self._triggers[trigger.trigger_id] = claimed
                won.append(claimed.model_copy(deep=True))
            return won

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None:
        async with self._mutex:
            t = self._triggers.get(trigger_id)
            if t is not None:
                self._triggers[trigger_id] = t.model_copy(update={
                    "last_fire_at": last_fire,
                    "next_fire_at": next_fire,
                    "run_count": t.run_count + 1,
                    # Release the claim: a lease that outlives the fire blocks
                    # the next occurrence whenever it is longer than the
                    # interval.
                    "claimed_by": "",
                    "claimed_until": None,
                })

    async def delete_trigger(self, trigger_id: str) -> None:
        async with self._mutex:
            self._triggers.pop(trigger_id, None)

    # -- helpers ----------------------------------------------------------------------

    def clear(self) -> None:
        self._executions.clear()
        self._journals.clear()
        self._events.clear()
        self._cache.clear()
        self._locks.clear()
        self._idempotency.clear()
        self._triggers.clear()
