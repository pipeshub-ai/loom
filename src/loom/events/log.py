"""The event log, and where subscribers have read to.

Two ports, deliberately not one. A Kafka adapter implements both (consumer
groups already store offsets); a Postgres adapter implements the log and takes
the default checkpoints; a read-only provider source implements neither and is
a producer. Fusing them would force every adapter to implement storage it does
not own.

**The package ships no broker.** It ships these protocols, one reference
implementation that adds no backend-specific code, and a conformance kit
(:mod:`loom.testing.conformance`) so a host's own adapter can prove itself. Each
adapter shipped is one that must be tested against a real server forever, and
four brokers times four databases is a maintenance surface nobody funds in year
three.

The reference implementation is built only on :class:`~loom.stores.base.CacheStore`
and :class:`~loom.stores.base.LockProvider`, which every LOOM store already
implements — the same move ``ctx.state`` made, for the same reason: whatever
backs the journal backs this, so a laptop gets a durable resumable log out of one
SQLite file and a deployment gets one out of the database it already runs.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loom.core.exceptions import ConfigurationError
from loom.events.models import (
    Checkpoint,
    EventRecord,
    Position,
    RetentionPolicy,
    StoredEvent,
)

if TYPE_CHECKING:
    from loom.runtime.clock import Clock

__all__ = [
    "Checkpoints",
    "EventLog",
    "StoreBackedCheckpoints",
    "StoreBackedEventLog",
]

logger = logging.getLogger("workflow.events")


@runtime_checkable
class EventLog(Protocol):
    """An append-only, resumable record of what the world said."""

    async def append(
        self, topic: str, records: Sequence[EventRecord]
    ) -> Sequence[Position]:
        """Durably record *records*, returning each one's position, in order.

        **Idempotent on ``event_id``.** Appending a record whose id is already
        in this topic returns the position it already has and stores nothing
        new. That is what lets a producer retry after a crash without a
        duplicate, and it is why ``event_id`` must be derived rather than
        random.

        **Not atomic across the batch.** A partitioned backend writes per
        partition and can fail part-way. A caller that sees an exception must
        assume some records landed and retry the whole batch; idempotency makes
        that safe. Promising atomicity would be a promise only the
        single-writer implementations could keep.
        """
        ...

    async def read(
        self, topic: str, *, after: Position | None, limit: int
    ) -> Sequence[StoredEvent]:
        """Records this reader has not seen, given *after*.

        ``after=None`` starts at the beginning of what is still retained — which
        is not necessarily the beginning of time, and the difference is a gap
        the caller should be told about rather than left to infer.
        """
        ...

    async def head(self, topic: str) -> Position | None:
        """The newest position, or ``None`` for a topic with nothing in it.

        For a subscriber starting at ``LATEST``: the position it should claim to
        have already read.
        """
        ...

    async def retain(self, topic: str, policy: RetentionPolicy) -> int:
        """Discard what *policy* allows. Returns how many records went.

        The adapter decides how, and may decide *not to* — a Kafka adapter
        honours the broker's retention setting and returns ``0``, which is a
        more useful answer than pretending to have enforced something.
        """
        ...

    async def wait_for(
        self, topic: str, *, after: Position | None, timeout: float
    ) -> bool:
        """Block until something exists after *after*, or *timeout* elapses.

        Optional in spirit: the default implementation below polls, so every
        adapter is valid without overriding it. Adapters that can do better
        should — Postgres ``LISTEN``, Redis ``XREAD BLOCK``, a Kafka consumer's
        own blocking poll — because the dominant cost in a quiet system is not
        events, it is thousands of reads per second discovering that nothing
        happened.
        """
        ...


@runtime_checkable
class Checkpoints(Protocol):
    """Where each subscriber has read to. The resume primitive."""

    async def commit(self, subscriber: str, topic: str, position: Position) -> None:
        """Record that *subscriber* has finished everything through *position*.

        Committed **after** the work it covers is durable, never before. The
        asymmetry is the whole argument: committing early loses events
        permanently, committing late costs rework that the dispatch idempotency
        key already absorbs.
        """
        ...

    async def load(self, subscriber: str, topic: str) -> Position | None:
        """Where *subscriber* got to, or ``None`` if it has never committed."""
        ...

    async def active(self, topic: str) -> Mapping[str, Checkpoint]: ...

    async def forget(self, subscriber: str, topic: str) -> None:
        """Retire a subscriber. Explicit, because the alternative — letting an
        abandoned subscriber pin retention forever — is how a log grows without
        bound."""
        ...


# ---------------------------------------------------------------------------
# The reference implementation
# ---------------------------------------------------------------------------

#: Namespaces inside the shared cache keyspace, so a log cannot collide with a
#: step's cache entry or with ``ctx.state``.
_HEAD = "eventlog:head"
_REC = "eventlog:rec"
_IDX = "eventlog:idx"
_TOPICS = "eventlog:topics"

#: How long one append may hold a topic's lock. Long enough for a slow store to
#: write a batch, short enough that a writer killed mid-append delays the next
#: one by seconds rather than minutes.
_APPEND_LEASE = 30.0

#: Cache entries never expire on their own: this is a log, not a cache. A record
#: that vanished on a timer would be indistinguishable from one that was never
#: appended, which is the failure retention exists to make deliberate.
_NO_TTL = 0.0


class StoreBackedEventLog:
    """A durable, resumable log over any LOOM store. **The default.**

    Uses only ``CacheStore`` (``get``/``set``/``delete``) and ``LockProvider``,
    both of which memory, SQLite, Postgres and Mongo already implement — so this
    lands with no new store methods, no migrations, and nothing added to
    ``stores/``.

    Layout, per topic:

    ==========================  ====================================
    ``eventlog:head:{topic}``   the highest *committed* sequence
    ``eventlog:rec:{topic}:N``  the record at sequence N
    ``eventlog:idx:{topic}:ID`` ``event_id`` -> sequence, for dedupe
    ==========================  ====================================

    **Why records are written before the head advances.** An append takes the
    topic lock, writes each record, and only then moves the head. A writer
    killed in between leaves records at sequences the head does not cover — and
    since reads are bounded by the head, they are invisible, and the next append
    reserves the same sequences and overwrites them. So a crashed append is
    exactly an append that never happened, which is the right answer: it had not
    returned, so nobody was told it succeeded, and the producer will retry.

    Advancing the head first would be the opposite trade — visible holes that
    stall every reader — and recovering from those needs tombstones and a
    timeout, machinery this ordering makes unnecessary.

    **Ceiling, stated rather than discovered.** One lock acquisition per append
    serialises writers per topic. That is ample for provider webhooks and for
    ``ctx.publish``; it is not a firehose. A host that outgrows it supplies an
    adapter — which is the point at which they should be choosing Kafka anyway,
    and by then they know why.

    This implementation gives a *total* order within a topic, which is stronger
    than the port promises. Callers must not rely on that: the promise is
    per-key ordering, and code written against the stronger guarantee breaks the
    day it moves to a partitioned backend.
    """

    def __init__(
        self, store: Any, *, owner: str = "", clock: Clock | None = None
    ) -> None:
        for capability in ("get", "set", "delete"):
            if not hasattr(store, capability):
                raise ConfigurationError(
                    f"StoreBackedEventLog needs a CacheStore; {type(store).__name__} "
                    f"has no '{capability}'. Pass a LOOM store, or supply your own "
                    "EventLog — see loom.testing.conformance to prove it correct."
                )
        from loom.runtime.clock import SystemClock

        self._store = store
        self._lock = store if hasattr(store, "acquire") else None
        self._owner = owner or f"eventlog-{secrets.token_hex(4)}"
        self._poll_interval = 0.05
        self._guards: dict[str, tuple[Any, Any]] = {}
        #: The same ``Clock`` port the engine reads, so that "this record is
        #: three days old" and "this subscriber has not committed in a week"
        #: are testable at all. Retention was the one place in the backbone
        #: that could only be exercised by waiting: an age cutoff read from
        #: ``datetime.now(UTC)`` means a test for a seven-day policy takes
        #: seven days, so nothing tested it.
        self._clock: Clock = clock or SystemClock()

    # -- writing -------------------------------------------------------------

    async def append(
        self, topic: str, records: Sequence[EventRecord]
    ) -> Sequence[Position]:
        if not records:
            return []

        lock_key = f"eventlog:append:{topic}"
        async with _AppendLock(
            self._lock, lock_key, self._owner, _APPEND_LEASE, self._guard_for(topic)
        ):
            head = int(await self._store.get(f"{_HEAD}:{topic}") or 0)
            positions: list[Position] = []
            written = 0

            for record in records:
                index_key = f"{_IDX}:{topic}:{record.event_id}"
                existing = await self._store.get(index_key)
                if existing is not None:
                    # Already appended. Return where it went rather than adding
                    # a second copy: this is what makes a producer's retry after
                    # a crash a no-op instead of a duplicate.
                    positions.append(str(existing))
                    continue

                seq = head + written + 1
                await self._store.set(
                    f"{_REC}:{topic}:{seq}",
                    _encode(record, seq, self._clock.now()),
                    _NO_TTL,
                )
                await self._store.set(index_key, seq, _NO_TTL)
                positions.append(str(seq))
                written += 1

            if written:
                # Last, and only now are the records readable. See the class
                # docstring for why this ordering removes the need for holes.
                await self._store.set(f"{_HEAD}:{topic}", head + written, _NO_TTL)
                await self._remember_topic(topic)

        return positions

    def _guard_for(self, topic: str) -> Any:
        """This instance's in-process lock for *topic*, bound to *this* loop.

        Rebuilt whenever the running loop changes. An ``asyncio.Lock`` awaited
        from a loop other than the one that first used it either raises or
        hangs, and a long-lived log — LOOM's ``get_default_*`` singletons are
        exactly this shape — outlives any one loop.

        Rebinding is also semantically right rather than a workaround: a lock
        only ever coordinates coroutines within one loop, so a different loop is
        a different concurrency domain. Two loops in two threads are serialised
        by the store's ``LockProvider``, which is the layer that can.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        guard, bound = self._guards.get(topic, (None, None))
        if guard is None or bound is not loop:
            guard = asyncio.Lock()
            self._guards[topic] = (guard, loop)
        return guard

    async def _remember_topic(self, topic: str) -> None:
        """Track topic names so retention and diagnostics can enumerate them.

        A key-value store cannot scan, so the set is maintained explicitly.
        """
        known = set(await self._store.get(_TOPICS) or [])
        if topic not in known:
            known.add(topic)
            await self._store.set(_TOPICS, sorted(known), _NO_TTL)

    # -- reading -------------------------------------------------------------

    async def read(
        self, topic: str, *, after: Position | None, limit: int
    ) -> Sequence[StoredEvent]:
        if limit <= 0:
            return []
        head = int(await self._store.get(f"{_HEAD}:{topic}") or 0)
        start = (int(after) if after is not None else 0) + 1

        found: list[StoredEvent] = []
        seq = start
        while seq <= head and len(found) < limit:
            raw = await self._store.get(f"{_REC}:{topic}:{seq}")
            if raw is not None:
                found.append(_decode(raw, seq))
            # A missing sequence below the head is a retained-away record, not a
            # stall: skip it. Holes from a crashed append cannot occur below the
            # head, by construction.
            seq += 1
        return found

    async def head(self, topic: str) -> Position | None:
        head = int(await self._store.get(f"{_HEAD}:{topic}") or 0)
        return str(head) if head else None

    async def wait_for(
        self, topic: str, *, after: Position | None, timeout: float
    ) -> bool:
        """Poll until something is there. Adapters with a native block override.

        Deliberately naive: its job is to make every adapter valid without
        implementing this, not to be efficient. Efficiency here is
        backend-specific and belongs in the backend.

        Both the deadline and the poll go through the ``Clock``, not the event
        loop's timer. On a ``ManualClock`` the loop's timer runs at wall speed
        while everything else in the test runs at virtual speed, so a wait
        measured against it is the one place where an idle dispatcher would
        still cost real seconds — and a `timeout` a test believed it had
        skipped past.
        """
        deadline = self._clock.now().timestamp() + timeout
        target = int(after) if after is not None else 0
        while True:
            head = int(await self._store.get(f"{_HEAD}:{topic}") or 0)
            if head > target:
                return True
            if self._clock.now().timestamp() >= deadline:
                return False
            await self._clock.sleep(self._poll_interval)

    # -- retention -----------------------------------------------------------

    async def retain(self, topic: str, policy: RetentionPolicy) -> int:
        """Discard records past the policy, never past a live subscriber.

        The floor is the *slowest active* checkpoint. Dropping records a live
        reader has not seen is silent permanent loss that surfaces as "that
        workflow just never ran", so this refuses — and a log that grows because
        a subscriber is stuck is a visible problem, which is the right way
        round. A subscriber whose checkpoint has gone stale past
        ``subscriber_ttl_seconds`` stops holding the floor.
        """
        head = int(await self._store.get(f"{_HEAD}:{topic}") or 0)
        if not head:
            return 0

        now = self._clock.now()
        floor = head
        if self._checkpoints is not None:
            for mark in (await self._checkpoints.active(topic)).values():
                if mark.is_stale(now=now, ttl_seconds=policy.subscriber_ttl_seconds):
                    continue
                floor = min(floor, int(mark.position))

        # One reading for both the staleness test and the age cutoff. Two calls
        # to `now()` could straddle a subscriber's TTL, so a checkpoint could be
        # counted live for the floor and stale for the cutoff in one pass.
        cutoff = now.timestamp() - policy.max_age_seconds

        # A count ceiling is a *second*, independent reason to discard, and it
        # deliberately ignores the age floor — that is what makes it a ceiling
        # rather than a suggestion. It does **not** ignore the subscriber floor:
        # an operator who wants to drop records a live reader has not seen
        # quarantines that reader first, which is a decision with a name on it
        # rather than a number in a config.
        over_by = 0
        if policy.max_records is not None and policy.max_records >= 0:
            over_by = max(0, head - policy.max_records)

        removed = 0
        for seq in range(1, floor + 1):
            raw = await self._store.get(f"{_REC}:{topic}:{seq}")
            if raw is None:
                continue
            stored = _decode(raw, seq)
            if stored.appended_at.timestamp() > cutoff and seq > over_by:
                # Ordered by append time, so the first record that is both young
                # enough and inside the count ceiling ends the walk.
                break
            await self._store.delete(f"{_REC}:{topic}:{seq}")
            await self._store.delete(f"{_IDX}:{topic}:{stored.event_id}")
            removed += 1
        return removed

    _checkpoints: Checkpoints | None = None

    def with_checkpoints(self, checkpoints: Checkpoints) -> StoreBackedEventLog:
        """Tell retention which subscribers to respect.

        Separate from the constructor because the two ports are independent: a
        log with no checkpoints attached still works, it just cannot honour the
        slowest-reader floor — so it is better to have said so explicitly than
        to have a log silently discard what a subscriber needed.
        """
        self._checkpoints = checkpoints
        return self

    async def topics(self) -> list[str]:
        """Every topic this log has ever been appended to.

        Deliberately **not** on the :class:`EventLog` protocol. Enumerating
        topics is cheap here because the set is maintained explicitly, and is
        expensive or impossible on a broker that multiplexes logical topics
        onto physical ones — requiring it would force an adapter to either
        scan or lie. ``loom events`` probes for it and degrades to the topics
        it can name from subscriptions.
        """
        return sorted(await self._store.get(_TOPICS) or [])

    def __repr__(self) -> str:
        return f"<StoreBackedEventLog owner={self._owner!r}>"


class StoreBackedCheckpoints:
    """Subscriber positions over any LOOM store. **The default.**

    Rides ``CacheStore`` alone, which is why it needs no new backend code: a
    checkpoint is a small value keyed by ``(subscriber, topic)``, which is
    exactly what ``ctx.state`` already stores.

    An adapter whose backend has native offsets — Kafka consumer groups,
    Salesforce ``ManagedSubscribe`` — implements :class:`Checkpoints` itself and
    is used instead. That is the whole reason the two are separate ports.
    """

    PREFIX = "eventlog:ckpt"
    ROSTER = "eventlog:subs"

    def __init__(self, store: Any, *, clock: Clock | None = None) -> None:
        from loom.runtime.clock import SystemClock

        self._store = store
        #: ``updated_at`` is stamped here and read by ``Checkpoint.is_stale``,
        #: by ``StoreBackedEventLog.retain``, and by
        #: ``SubscriptionManager.health``. All four have to be on one timeline:
        #: a writer on the wall clock and a reader on a virtual one makes every
        #: checkpoint look either brand new or a decade stale, depending on
        #: which way the test set its clock.
        self._clock: Clock = clock or SystemClock()

    def _key(self, subscriber: str, topic: str) -> str:
        return f"{self.PREFIX}:{topic}:{subscriber}"

    async def commit(self, subscriber: str, topic: str, position: Position) -> None:
        await self._store.set(
            self._key(subscriber, topic),
            {
                "position": str(position),
                "updated_at": self._clock.now().isoformat(),
            },
            _NO_TTL,
        )
        await self._enrol(subscriber, topic)

    async def load(self, subscriber: str, topic: str) -> Position | None:
        found = await self._store.get(self._key(subscriber, topic))
        if not found:
            return None
        return str(found["position"])

    async def commit_many(
        self, subscriber: str, positions: Mapping[str, Position]
    ) -> None:
        """One subscriber's position in several topics.

        Present because the dominant cost in a quiet system is per-subscriber,
        per-topic bookkeeping rather than events, and a subscriber watching
        twenty topics should not pay twenty writes to say nothing changed.
        """
        for topic, position in positions.items():
            await self.commit(subscriber, topic, position)

    async def active(self, topic: str) -> Mapping[str, Checkpoint]:
        names = await self._store.get(f"{self.ROSTER}:{topic}") or []
        found: dict[str, Checkpoint] = {}
        for name in names:
            raw = await self._store.get(self._key(name, topic))
            if raw is None:
                continue
            found[name] = Checkpoint(
                subscriber=name,
                topic=topic,
                position=str(raw["position"]),
                updated_at=datetime.fromisoformat(raw["updated_at"]),
            )
        return found

    async def forget(self, subscriber: str, topic: str) -> None:
        await self._store.delete(self._key(subscriber, topic))
        names = [n for n in (await self._store.get(f"{self.ROSTER}:{topic}") or [])
                 if n != subscriber]
        await self._store.set(f"{self.ROSTER}:{topic}", names, _NO_TTL)

    async def _enrol(self, subscriber: str, topic: str) -> None:
        key = f"{self.ROSTER}:{topic}"
        names = set(await self._store.get(key) or [])
        if subscriber not in names:
            names.add(subscriber)
            await self._store.set(key, sorted(names), _NO_TTL)

    def __repr__(self) -> str:
        return "<StoreBackedCheckpoints>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AppendLock:
    """Hold the topic's lock for a block.

    Two layers, and which one carries the weight depends on the store:

    **The store's ``LockProvider``** does the real work — it is what serialises
    two processes, and every LOOM store implements one.

    **A per-instance asyncio lock** serialises this instance's own coroutines,
    so a batch is not interleaved with itself. Deliberately *per instance*
    rather than class-level: a lock cached across instances outlives the event
    loop that created it, and an ``asyncio.Lock`` awaited from a second loop
    either raises or hangs — a bug that hides until two writers actually
    contend, which is exactly when it matters least to be debugging.

    A store offering no ``LockProvider`` therefore supports **one log instance
    per process**. That is stated rather than silently assumed; every store LOOM
    ships offers one.
    """

    def __init__(self, lock: Any, key: str, owner: str, ttl: float, guard: Any) -> None:
        self._lock = lock
        self._key = key
        self._owner = owner
        self._ttl = ttl
        self._guard = guard
        self._acquired = False

    async def __aenter__(self) -> None:
        import asyncio

        await self._guard.acquire()
        if self._lock is not None:
            # Deliberately the event loop's clock rather than the log's, and
            # for the reason the engine's lease heartbeat gives: this is a wait
            # on *another process*, which is on wall time whatever this one
            # believes. A virtual deadline here would let a test declare the
            # other writer wedged without a millisecond having passed.
            deadline = asyncio.get_running_loop().time() + self._ttl
            while not await self._lock.acquire(self._key, self._owner, self._ttl):
                if asyncio.get_running_loop().time() >= deadline:
                    self._guard.release()
                    raise TimeoutError(
                        f"could not take the append lock for {self._key} within "
                        f"{self._ttl}s; another writer may be wedged"
                    )
                await asyncio.sleep(0.02)
            self._acquired = True

    async def __aexit__(self, *_: Any) -> None:
        try:
            if self._acquired and self._lock is not None:
                await self._lock.release(self._key, self._owner)
        finally:
            self._guard.release()


def _encode(record: EventRecord, seq: int, appended_at: datetime) -> Any:
    return {
        "seq": seq,
        "event_id": record.event_id,
        "type": record.type,
        "payload": json.loads(json.dumps(dict(record.payload), default=str)),
        "key": record.key,
        "source": record.source,
        "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
        "chain_depth": record.chain_depth,
        "appended_at": appended_at.isoformat(),
    }


def _decode(raw: Any, seq: int) -> StoredEvent:
    occurred = raw.get("occurred_at")
    return StoredEvent(
        position=str(seq),
        record=EventRecord(
            event_id=raw["event_id"],
            type=raw["type"],
            payload=raw.get("payload") or {},
            key=raw.get("key", ""),
            source=raw.get("source", ""),
            occurred_at=datetime.fromisoformat(occurred) if occurred else None,
            chain_depth=int(raw.get("chain_depth", 0) or 0),
        ),
        appended_at=datetime.fromisoformat(raw["appended_at"]),
    )


def positions_of(events: Iterable[StoredEvent]) -> list[Position]:
    """The positions of *events*, for committing after a batch."""
    return [event.position for event in events]
