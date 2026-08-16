"""The event log: the contract, and the reference implementation's internals.

Two levels, deliberately.

``TestConformance`` runs the *shipped kit* — the same one a third party runs
against a Kafka adapter — against `StoreBackedEventLog` on every store backend.
If the kit and the reference implementation ever disagree, one of them is wrong,
and this is where that surfaces.

Everything else is white-box: crash ordering, the retention floor, and the
key layout. A black-box kit cannot force a store to fail on its second write,
and that is precisely the case the implementation's ordering exists to survive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom.core.exceptions import ConfigurationError
from loom.events import (
    Checkpoint,
    EventRecord,
    RetentionPolicy,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
)
from loom.testing.conformance import verify_checkpoints, verify_event_log

TOPIC = "app.test.event"


def record(event_id: str, **kw: Any) -> EventRecord:
    return EventRecord(event_id=event_id, type=kw.pop("type", "test"), **kw)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path):
    """Every backend the kit can reach without a server.

    Postgres and Mongo are covered by ``tests/conformance/`` when servers are
    configured; the point here is that the *same* implementation is exercised on
    more than one substrate, because a log that works only on a dict is a log
    that has not met a real store.
    """
    if request.param == "memory":
        from loom.stores.memory import MemoryStore

        return MemoryStore()

    from loom.stores.sqlite import SQLiteStore

    return SQLiteStore(str(tmp_path / "log.db"))


# ---------------------------------------------------------------------------
# The shipped contract
# ---------------------------------------------------------------------------


class TestConformance:
    """The reference implementation must pass the kit it ships with."""

    async def test_the_event_log_conforms(self, store) -> None:
        await verify_event_log(lambda: StoreBackedEventLog(store))

    async def test_the_checkpoints_conform(self, store) -> None:
        await verify_checkpoints(lambda: StoreBackedCheckpoints(store))


# ---------------------------------------------------------------------------
# Crash ordering — the reason records are written before the head moves
# ---------------------------------------------------------------------------


class FailingStore:
    """A store that fails on the Nth write. Stands in for a process being killed.

    Wrapping rather than subclassing so it works over any backend, and so the
    failure is at exactly the layer a crash would interrupt: one key write.
    """

    def __init__(self, inner: Any, *, fail_on_write: int) -> None:
        self._inner = inner
        self._budget = fail_on_write
        self.writes = 0

    async def get(self, key: str) -> Any:
        return await self._inner.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self.writes += 1
        if self.writes == self._budget:
            raise OSError("process killed mid-append")
        await self._inner.set(key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        return await self._inner.acquire(key, owner, ttl_seconds)

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool:
        return await self._inner.renew(key, owner, ttl_seconds)

    async def release(self, key: str, owner: str) -> None:
        await self._inner.release(key, owner)


class TestCrashMidAppend:
    """An append that did not return is an append that did not happen.

    The implementation writes every record and only then advances the head. A
    writer killed in between leaves records at sequences the head does not
    cover — invisible, because reads are bounded by the head, and overwritten by
    the next append. Advancing the head first would be the opposite trade:
    visible holes that stall every reader behind them, needing tombstones and a
    timeout to recover.
    """

    async def test_a_crash_before_the_head_moves_leaves_nothing_readable(
        self, store
    ) -> None:
        good = StoreBackedEventLog(store)
        await good.append(TOPIC, [record("before")])

        # Records are written, then the head. Failing on a later write lands
        # inside the batch, after at least one record has been stored.
        flaky = FailingStore(store, fail_on_write=3)
        with pytest.raises(OSError, match="killed"):
            await StoreBackedEventLog(flaky).append(
                TOPIC, [record("ghost-1"), record("ghost-2")]
            )

        visible = [e.event_id for e in await good.read(TOPIC, after=None, limit=50)]
        assert visible == ["before"], (
            f"a crashed append must be invisible; saw {visible}"
        )

    async def test_the_next_append_reuses_the_sequences_and_is_readable(
        self, store
    ) -> None:
        """Not merely invisible — the space is reclaimed, so no hole persists."""
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record("before")])

        flaky = FailingStore(store, fail_on_write=3)
        with pytest.raises(OSError):
            await StoreBackedEventLog(flaky).append(
                TOPIC, [record("ghost-1"), record("ghost-2")]
            )

        await log.append(TOPIC, [record("after-1"), record("after-2")])

        found = await log.read(TOPIC, after=None, limit=50)
        assert [e.event_id for e in found] == ["before", "after-1", "after-2"]
        assert [e.position for e in found] == ["1", "2", "3"], (
            "the crashed append's sequences must be reused, leaving no gap"
        )

    async def test_a_reader_is_never_stalled_by_a_crashed_writer(
        self, store
    ) -> None:
        """The failure the ordering exists to prevent."""
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record("e1")])
        after = (await log.read(TOPIC, after=None, limit=1))[0].position

        flaky = FailingStore(store, fail_on_write=2)
        with pytest.raises(OSError):
            await StoreBackedEventLog(flaky).append(TOPIC, [record("ghost")])

        await log.append(TOPIC, [record("e2")])

        assert [e.event_id for e in await log.read(TOPIC, after=after, limit=10)] == [
            "e2"
        ], "a reader resumed after a crashed append must see what came next"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    """Never past a live reader, and never unbounded because one is stuck."""

    async def _aged(self, store, log, count: int, *, days: float) -> None:
        """Backdate the appended records so age-based policy has something to do."""
        await log.append(TOPIC, [record(f"e{i}") for i in range(count)])
        old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        for seq in range(1, count + 1):
            raw = await store.get(f"eventlog:rec:{TOPIC}:{seq}")
            raw["appended_at"] = old
            await store.set(f"eventlog:rec:{TOPIC}:{seq}", raw, 0.0)

    async def test_a_count_ceiling_discards_the_oldest(self, store) -> None:
        """`max_records` was declared on the policy and silently ignored, which
        is worse than not offering it: an operator sets a cap, sees no error,
        and believes the topic is bounded."""
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record(f"e{i}") for i in range(5)])

        removed = await log.retain(TOPIC, RetentionPolicy(max_records=2))

        assert removed == 3
        assert [e.position for e in await log.read(TOPIC, after=None, limit=10)] == [
            "4",
            "5",
        ]

    async def test_a_count_ceiling_ignores_the_age_floor(self, store) -> None:
        """That is what makes it a ceiling rather than a suggestion — every
        record here is seconds old and three of them still go."""
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record(f"e{i}") for i in range(5)])

        removed = await log.retain(
            TOPIC, RetentionPolicy(max_age_seconds=7 * 24 * 3600, max_records=2)
        )

        assert removed == 3

    async def test_a_count_ceiling_still_stops_at_a_live_subscriber(
        self, store
    ) -> None:
        """An operator wanting to drop what a live reader has not seen
        quarantines that reader first — a decision with a name on it, rather
        than a number in a config."""
        marks = StoreBackedCheckpoints(store)
        log = StoreBackedEventLog(store).with_checkpoints(marks)
        await log.append(TOPIC, [record(f"e{i}") for i in range(5)])
        await marks.commit("slow", TOPIC, "1")

        removed = await log.retain(TOPIC, RetentionPolicy(max_records=2))

        assert removed == 1, "only what the slow reader has already seen"
        assert len(await log.read(TOPIC, after=None, limit=10)) == 4

    async def test_no_ceiling_means_unbounded(self, store) -> None:
        """The honest default: a size cap that silently drops unread records is
        the failure the whole policy exists to prevent."""
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record(f"e{i}") for i in range(5)])

        assert await log.retain(TOPIC, RetentionPolicy()) == 0

    async def test_records_younger_than_the_policy_survive(self, store) -> None:
        log = StoreBackedEventLog(store)
        await log.append(TOPIC, [record(f"e{i}") for i in range(3)])

        assert await log.retain(TOPIC, RetentionPolicy(max_age_seconds=3600)) == 0
        assert len(await log.read(TOPIC, after=None, limit=10)) == 3

    async def test_old_records_go_when_nobody_is_behind(self, store) -> None:
        log = StoreBackedEventLog(store)
        await self._aged(store, log, 3, days=30)

        removed = await log.retain(TOPIC, RetentionPolicy(max_age_seconds=3600))

        assert removed == 3
        assert await log.read(TOPIC, after=None, limit=10) == []

    async def test_retention_stops_at_the_slowest_live_subscriber(
        self, store
    ) -> None:
        """Discarding what a live reader has not seen is silent permanent loss.

        It surfaces much later as "that workflow just never ran", which is the
        hardest kind of bug to trace back to its cause.
        """
        marks = StoreBackedCheckpoints(store)
        log = StoreBackedEventLog(store).with_checkpoints(marks)
        await self._aged(store, log, 5, days=30)

        await marks.commit("slow", TOPIC, "2")

        removed = await log.retain(TOPIC, RetentionPolicy(max_age_seconds=3600))

        assert removed == 2, "retention passed a live subscriber"
        left = [e.event_id for e in await log.read(TOPIC, after="2", limit=10)]
        assert left == ["e2", "e3", "e4"], (
            "everything the slow subscriber had not read must still be there"
        )

    async def test_a_stale_subscriber_stops_pinning_the_log(self, store) -> None:
        """The other half: an abandoned subscriber must not hold the floor
        forever, or the log grows without bound because nobody retired it."""
        marks = StoreBackedCheckpoints(store)
        log = StoreBackedEventLog(store).with_checkpoints(marks)
        await self._aged(store, log, 5, days=30)

        await marks.commit("abandoned", TOPIC, "2")
        stale = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        await store.set(
            f"eventlog:ckpt:{TOPIC}:abandoned",
            {"position": "2", "updated_at": stale},
            0.0,
        )

        removed = await log.retain(
            TOPIC,
            RetentionPolicy(max_age_seconds=3600, subscriber_ttl_seconds=7 * 24 * 3600),
        )

        assert removed == 5, "a stale checkpoint must stop holding the retention floor"

    async def test_a_log_with_no_checkpoints_attached_still_retains(
        self, store
    ) -> None:
        """`with_checkpoints` is opt-in, so this path must be defined."""
        log = StoreBackedEventLog(store)
        await self._aged(store, log, 2, days=30)

        assert await log.retain(TOPIC, RetentionPolicy(max_age_seconds=3600)) == 2


class TestCheckpointStaleness:
    def test_a_fresh_checkpoint_is_not_stale(self) -> None:
        now = datetime.now(UTC)
        mark = Checkpoint("s", TOPIC, "1", updated_at=now)
        assert not mark.is_stale(now=now, ttl_seconds=60)

    def test_one_past_its_ttl_is(self) -> None:
        now = datetime.now(UTC)
        mark = Checkpoint("s", TOPIC, "1", updated_at=now - timedelta(seconds=120))
        assert mark.is_stale(now=now, ttl_seconds=60)


# ---------------------------------------------------------------------------
# Construction and layout
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_a_store_without_a_cache_is_refused_with_the_fix(self) -> None:
        """Naming the fix matters: the alternative is an AttributeError five
        frames into an append."""

        class NotAStore:
            pass

        with pytest.raises(ConfigurationError) as exc:
            StoreBackedEventLog(NotAStore())

        assert "CacheStore" in str(exc.value)
        assert "conformance" in str(exc.value)

    async def test_it_needs_no_store_methods_beyond_cache_and_lock(
        self, store
    ) -> None:
        """The claim the whole design rests on: no new backend code.

        Asserted by construction — a wrapper exposing *only* CacheStore and
        LockProvider must be enough. If this fails, the log has grown a
        dependency on something `stores/` would have to implement four times.
        """

        class CacheAndLockOnly:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def get(self, key: str) -> Any:
                return await self._inner.get(key)

            async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
                await self._inner.set(key, value, ttl_seconds)

            async def delete(self, key: str) -> None:
                await self._inner.delete(key)

            async def acquire(self, key: str, owner: str, ttl: float) -> bool:
                return await self._inner.acquire(key, owner, ttl)

            async def renew(self, key: str, owner: str, ttl: float) -> bool:
                return await self._inner.renew(key, owner, ttl)

            async def release(self, key: str, owner: str) -> None:
                await self._inner.release(key, owner)

        narrow = CacheAndLockOnly(store)
        await verify_event_log(lambda: StoreBackedEventLog(narrow), topic="narrow")
        await verify_checkpoints(
            lambda: StoreBackedCheckpoints(narrow), topic="narrow-ck"
        )

    async def test_the_protocols_are_satisfied_structurally(self, store) -> None:
        """No inheritance anywhere — an adapter must not have to import a base."""
        from loom.events import Checkpoints, EventLog

        assert isinstance(StoreBackedEventLog(store), EventLog)
        assert isinstance(StoreBackedCheckpoints(store), Checkpoints)


class TestRecordShape:
    async def test_every_field_survives_the_round_trip(self, store) -> None:
        log = StoreBackedEventLog(store)
        occurred = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        await log.append(
            TOPIC,
            [
                EventRecord(
                    event_id="e1",
                    type="slack.message",
                    payload={"text": "hi", "nested": {"a": 1}},
                    key="C123",
                    source="slack",
                    occurred_at=occurred,
                    chain_depth=2,
                )
            ],
        )

        got = (await log.read(TOPIC, after=None, limit=1))[0]

        assert got.record.type == "slack.message"
        assert got.record.payload == {"text": "hi", "nested": {"a": 1}}
        assert got.record.key == "C123"
        assert got.record.source == "slack"
        assert got.record.occurred_at == occurred
        assert got.record.chain_depth == 2

    async def test_occurred_at_and_appended_at_are_distinct(self, store) -> None:
        """A redelivery hours later has the original occurred_at and a fresh
        appended_at — conflating them makes a replayed event look current."""
        log = StoreBackedEventLog(store)
        occurred = datetime(2020, 1, 1, tzinfo=UTC)
        await log.append(TOPIC, [record("e1", occurred_at=occurred)])

        got = (await log.read(TOPIC, after=None, limit=1))[0]

        assert got.record.occurred_at == occurred
        assert got.appended_at.year >= 2026

    async def test_topics_are_isolated(self, store) -> None:
        log = StoreBackedEventLog(store)
        await log.append("a", [record("shared-id")])
        await log.append("b", [record("shared-id")])

        assert len(await log.read("a", after=None, limit=10)) == 1
        assert len(await log.read("b", after=None, limit=10)) == 1, (
            "the dedupe index must be per topic — two tenants' topics can "
            "legitimately carry the same provider event id"
        )


class TestLongLivedInstances:
    """A log outlives any one event loop.

    LOOM's ``get_default_*`` singletons are module-level and long-lived, and a
    test suite gives each test its own loop. An ``asyncio.Lock`` cached from an
    earlier loop either raises or hangs when awaited from a later one — and it
    only does so once two writers contend, so it hides until load.
    """

    def test_one_instance_works_across_separate_event_loops(self, store) -> None:
        import asyncio

        log = StoreBackedEventLog(store)

        asyncio.run(log.append(TOPIC, [record("first")]))
        asyncio.run(log.append(TOPIC, [record("second")]))
        found = asyncio.run(log.read(TOPIC, after=None, limit=10))

        assert [e.event_id for e in found] == ["first", "second"]

    def test_concurrent_writers_still_serialise_after_a_loop_change(
        self, store
    ) -> None:
        """The case a stale lock actually breaks: contention, in a later loop."""
        import asyncio

        log = StoreBackedEventLog(store)
        asyncio.run(log.append(TOPIC, [record("warm-up")]))

        async def hammer() -> None:
            await asyncio.gather(*(
                log.append(TOPIC, [record(f"w{w}-{i}") for i in range(3)])
                for w in range(4)
            ))

        asyncio.run(hammer())

        found = asyncio.run(log.read(TOPIC, after=None, limit=100))
        ids = [e.event_id for e in found]
        assert len(ids) == 13, f"lost records under contention: {len(ids)}"
        assert len(set(e.position for e in found)) == 13, "duplicate position"
