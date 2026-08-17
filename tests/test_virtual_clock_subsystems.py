"""The subsystems that used to read the wall clock behind the Runtime's back.

`Runtime(clock=...)` has always reached the engine, the context, and the trigger
dispatcher. It reached nothing in `loom/events/`, nothing in `AdmissionController`,
and only half of the queue consumer — which is not a policy, it is drift, and
the shape of it is always the same: one component stamps or measures against
`datetime.now(UTC)` while everything around it is on a timeline the test moved.

Two things follow from that, and both are silent:

* an age or an idle interval computed across the two clocks is nonsense — a
  checkpoint written on a virtual clock and read on the wall clock is either
  brand new or a decade stale, depending only on which way the test set its
  clock; and
* a retention window, a subscriber TTL, or a poll interval measured on the wall
  clock cannot be crossed by a test at all, so nothing tests the far side of it.

These tests cross those windows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from loom import Context, workflow
from loom.connectors.credentials import RefreshPolicy
from loom.connectors.refresh import CredentialRefreshService
from loom.events.dispatcher import EventDispatcher
from loom.events.log import StoreBackedCheckpoints, StoreBackedEventLog
from loom.events.manager import SubscriptionManager
from loom.events.models import EventRecord, RetentionPolicy
from loom.events.subscription import Subscription
from loom.runtime.clock import ManualClock, SystemClock
from loom.runtime.engine import Runtime
from loom.stores.memory import MemoryStore
from loom.testing import advance
from loom.triggers.queue import InMemoryQueue, QueueConsumer
from loom.triggers.specs import OnAppEvent

NINE_AM = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
TOPIC = "app.test.thing"


def a_record(n: int) -> EventRecord:
    return EventRecord(event_id=f"e{n}", type="app.test.thing", payload={"n": n})


@pytest.fixture
def wired():
    """A Runtime, a log, checkpoints, and a dispatcher, all on one clock."""
    clock = ManualClock(NINE_AM)
    store = MemoryStore()
    log = StoreBackedEventLog(store, clock=clock)
    marks = StoreBackedCheckpoints(store, clock=clock)
    runtime = Runtime(store=store, clock=clock, events=log)
    dispatcher = EventDispatcher(runtime, log=log, checkpoints=marks)
    log.with_checkpoints(marks)
    return clock, store, log, marks, runtime, dispatcher


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------


class TestTheLogIsOnTheRuntimesTimeline:
    async def test_appended_at_comes_from_the_clock(self, wired) -> None:
        """`appended_at` is what retention ages records against, so a record
        stamped from the wall clock inside a virtual-time run is a record no
        age policy can ever reach."""
        clock, _, log, _, _, _ = wired
        await log.append(TOPIC, [a_record(1)])
        clock.advance(hours=3)
        await log.append(TOPIC, [a_record(2)])

        both = await log.read(TOPIC, after=None, limit=10)

        assert both[0].appended_at == NINE_AM
        assert both[1].appended_at == NINE_AM.replace(hour=12)

    async def test_retention_by_age_is_reachable_in_virtual_time(self, wired) -> None:
        """A seven-day policy used to need seven days to test, so it was
        tested by nobody. The far edge of the window is the half that matters:
        a log that never discards grows without bound, and it does that
        quietly."""
        clock, _, log, _, _, _ = wired
        await log.append(TOPIC, [a_record(1), a_record(2)])

        policy = RetentionPolicy(max_age_seconds=7 * 24 * 3600.0)
        assert await log.retain(TOPIC, policy) == 0, "nothing is old enough yet"

        clock.advance(days=8)

        assert await log.retain(TOPIC, policy) == 2
        assert await log.read(TOPIC, after=None, limit=10) == []

    async def test_a_live_subscriber_still_holds_the_floor(self, wired) -> None:
        """Age alone never discards past a reader that has not caught up.
        Advancing the clock must not turn a retention policy into data loss."""
        clock, _, log, marks, _, _ = wired
        await log.append(TOPIC, [a_record(1), a_record(2), a_record(3)])
        await marks.commit("reader", TOPIC, "1")

        clock.advance(days=8)
        removed = await log.retain(
            TOPIC,
            RetentionPolicy(
                max_age_seconds=3600.0, subscriber_ttl_seconds=30 * 24 * 3600.0
            ),
        )

        assert removed == 1, (
            "retention passed a live subscriber's checkpoint — the events it "
            "had not read are gone with nothing to say so"
        )

    async def test_a_stale_subscriber_stops_holding_the_floor(self, wired) -> None:
        """And the other side of the same window. A subscriber abandoned past
        its TTL must not pin a topic forever — which is only assertable now
        that the writer of `updated_at` and the reader of it share a clock."""
        clock, _, log, marks, _, _ = wired
        await log.append(TOPIC, [a_record(1), a_record(2), a_record(3)])
        await marks.commit("reader", TOPIC, "1")

        clock.advance(days=30)
        removed = await log.retain(
            TOPIC,
            RetentionPolicy(max_age_seconds=3600.0, subscriber_ttl_seconds=7 * 24 * 3600.0),
        )

        assert removed == 3, (
            "an abandoned subscriber is still pinning retention past its TTL"
        )


class TestCheckpointsAndTheirReader:
    async def test_updated_at_is_stamped_from_the_clock(self, wired) -> None:
        _, _, _, marks, _, _ = wired
        await marks.commit("reader", TOPIC, "1")

        assert (await marks.active(TOPIC))["reader"].updated_at == NINE_AM

    async def test_the_writer_and_the_health_reader_agree(self, wired) -> None:
        """`StoreBackedCheckpoints.commit` writes `updated_at` and
        `SubscriptionManager.health` subtracts it from `now()`. Those were on
        two different clocks, and no production call site passed the manager
        one — so an idle age reported by `loom events status` was the gap
        between a virtual write and a wall-clock read, which is not a
        measurement of anything.
        """
        clock, store, log, marks, _, _ = wired
        manager = SubscriptionManager(store, log=log, checkpoints=marks, clock=clock)
        await manager.add(Subscription("reader", TOPIC, "w"))
        await marks.commit("reader", TOPIC, "1")

        clock.advance(minutes=30)
        row = (await manager.health(TOPIC))[0]

        assert row.idle_seconds == pytest.approx(1800.0)

    async def test_the_subscriber_ttl_is_crossable(self, wired) -> None:
        """A week of silence, reported as a week of silence. Nothing before
        this could reach the far side of `DEFAULT_SUBSCRIBER_TTL`."""
        clock, store, log, marks, _, _ = wired
        manager = SubscriptionManager(
            store, log=log, checkpoints=marks, clock=clock, subscriber_ttl=3600.0
        )
        await manager.add(Subscription("reader", TOPIC, "w"))
        await marks.commit("reader", TOPIC, "1")

        assert (await manager.health(TOPIC))[0].healthy

        clock.advance(hours=2)
        row = (await manager.health(TOPIC))[0]

        assert not row.healthy
        assert "subscriber TTL" in row.reason

    async def test_the_manager_defaults_to_a_clock_rather_than_to_none(self) -> None:
        """It used to be `clock or None`, and every production call site passed
        nothing — so the default *was* the behaviour."""
        assert isinstance(SubscriptionManager(MemoryStore())._clock, SystemClock)


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


@workflow(name="thing_worker", triggers=[OnAppEvent(TOPIC)])
async def thing_worker(ctx: Context, payload: dict) -> str:
    return f"saw {payload.get('n')}"


class TestTheDispatcherTakesTheRuntimesClock:
    async def test_it_defaults_from_the_runtime(self) -> None:
        clock = ManualClock(NINE_AM)
        store = MemoryStore()
        runtime = Runtime(store=store, clock=clock, events=StoreBackedEventLog(store))

        assert EventDispatcher(runtime)._clock is clock, (
            "the dispatcher is on wall time inside a Runtime on virtual time — "
            "the one component a time-travel test cannot reach"
        )

    async def test_an_explicit_clock_wins(self) -> None:
        store = MemoryStore()
        runtime = Runtime(store=store, events=StoreBackedEventLog(store))
        mine = ManualClock(NINE_AM)

        assert EventDispatcher(runtime, clock=mine)._clock is mine

    async def test_tick_reports_the_runs_it_started(self, wired) -> None:
        _, _, log, _, _, dispatcher = wired
        await dispatcher.register(thing_worker)
        await log.append(TOPIC, [a_record(1)])

        started = await dispatcher.tick()

        assert len(started) == 1
        assert isinstance(started[0], str)

    async def test_advance_can_drive_it(self, wired) -> None:
        """`advance(rt, dispatcher=EventDispatcher(...))` used to raise
        `AttributeError`: the helper a test reaches for to move time covered
        cron and timers and, silently, not the event backbone."""
        clock, _, log, _, runtime, dispatcher = wired
        await dispatcher.register(thing_worker)
        await log.append(TOPIC, [a_record(1)])

        fired = await advance(runtime, minutes=1, dispatcher=dispatcher)

        assert len(fired) == 1
        assert clock.now() == NINE_AM.replace(minute=1)

    async def test_wait_for_is_measured_on_the_clock(self, wired) -> None:
        """The log's polling `wait_for` used to time itself off the event
        loop, so an idle dispatcher inside a virtual-time test still paid real
        seconds for every pass."""
        clock, _, log, _, _, _ = wired

        assert await log.wait_for(TOPIC, after=None, timeout=30.0) is False
        assert clock.now() > NINE_AM, "the timeout elapsed on nobody's clock"
        assert clock.slept, "the poll never went through the clock"


# ---------------------------------------------------------------------------
# The other two loops
# ---------------------------------------------------------------------------


@workflow(name="queue_worker")
async def queue_worker(ctx: Context, payload: dict) -> str:
    return "ok"


class TestQueueConsumer:
    async def test_it_polls_on_the_runtimes_clock(self) -> None:
        """It diverged from `TriggerDispatcher.start` by one line — a bare
        `asyncio.sleep` — which is enough to make it the only ingress a
        virtual-clock test cannot pace."""
        clock = ManualClock(NINE_AM)
        runtime = Runtime(store=MemoryStore(), clock=clock)
        consumer = QueueConsumer(runtime, InMemoryQueue(), queue_worker)

        assert consumer._clock is clock

    async def test_it_falls_back_to_the_wall_clock(self) -> None:
        runtime = Runtime(store=MemoryStore())
        consumer = QueueConsumer(runtime, InMemoryQueue(), queue_worker)

        assert isinstance(consumer._clock, SystemClock)


class _Store:
    """The smallest credential store the refresh service will accept."""

    refresh_policy = RefreshPolicy()
    clock = SystemClock()

    async def names(self) -> list[str]:
        return []

    async def peek_all(self) -> dict[str, Any]:
        return {}


class TestCredentialRefreshService:
    async def test_the_runtimes_clock_outranks_the_stores(self) -> None:
        """`WatchRenewer` already prefers the Runtime's; this read the store's,
        so a host that put its Runtime on a `ManualClock` got a sweeper still
        sleeping on wall time."""
        clock = ManualClock(NINE_AM)
        runtime = Runtime(store=MemoryStore(), clock=clock)

        service = CredentialRefreshService(_Store(), runtime=runtime)

        assert service._clock is clock

    async def test_an_explicit_clock_still_wins(self) -> None:
        mine = ManualClock(NINE_AM)
        runtime = Runtime(store=MemoryStore(), clock=ManualClock(NINE_AM))

        service = CredentialRefreshService(_Store(), runtime=runtime, clock=mine)

        assert service._clock is mine

    async def test_with_no_runtime_it_still_finds_the_stores_clock(self) -> None:
        """The `loom login` case: a credential store and no Runtime at all."""
        assert isinstance(CredentialRefreshService(_Store())._clock, SystemClock)


# ---------------------------------------------------------------------------
# Park mode
# ---------------------------------------------------------------------------


class TestParkMode:
    """`ManualClock(park=True)` — the mode a background loop needs.

    The default clock advances on `sleep()` and returns, which is right for a
    timer inside work the test is awaiting and wrong for a `while True` that
    owns its own interval: that loop does not wait at all, it runs at event-loop
    speed and drags virtual time with it — a day per turn for a renewer on a
    daily interval.
    """

    async def test_a_sleep_waits_to_be_released(self) -> None:
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(60))
        await asyncio.sleep(0)

        assert clock.parked == 1
        assert not task.done(), "the sleep returned without anyone moving time"

        clock.advance(seconds=60)
        await task

        assert clock.parked == 0

    async def test_the_clock_does_not_move_itself(self) -> None:
        """The whole difference from the default mode."""
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(3600))
        await asyncio.sleep(0)

        assert clock.now() == NINE_AM

        clock.advance(hours=1)
        await task

    async def test_a_short_advance_leaves_it_parked(self) -> None:
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(60))
        await asyncio.sleep(0)

        clock.advance(seconds=59)
        await asyncio.sleep(0)
        assert not task.done()

        clock.advance(seconds=1)
        await task

    async def test_set_releases_too(self) -> None:
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(3600))
        await asyncio.sleep(0)

        clock.set(NINE_AM.replace(hour=23))
        await task

    async def test_the_duration_is_recorded_while_still_parked(self) -> None:
        """A test asserting that a loop asked for its interval should not have
        to let the loop finish to find out."""
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(86400))
        await asyncio.sleep(0)

        assert clock.slept == [86400]

        clock.advance(days=1)
        await task

    async def test_a_forgotten_advance_fails_loudly_rather_than_hanging(self) -> None:
        """The safety net. A test that starts a loop and never moves the clock
        would otherwise hang with no output at all, which is the one outcome
        worse than a wrong answer — so the guard is timed off the event loop,
        because a virtual clock nobody is advancing cannot time its own
        deadlock."""
        clock = ManualClock(NINE_AM, park=True, park_timeout=0.05)

        with pytest.raises(TimeoutError, match="parked sleep"):
            await clock.sleep(60)

    async def test_cancelling_a_parked_sleeper_lets_go(self) -> None:
        """A supervised loop is stopped by cancelling it, so a parked sleeper
        that stayed in the waiter list after cancellation would be woken by a
        later `advance()` and resolve a future nobody holds."""
        clock = ManualClock(NINE_AM, park=True)
        task = asyncio.create_task(clock.sleep(60))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert clock.parked == 0

    async def test_the_default_mode_is_untouched(self) -> None:
        """Park is opt-in, and this is the reason it has to be: every inline
        `ctx.sleep` in the suite depends on advance-and-return."""
        clock = ManualClock(NINE_AM)
        await clock.sleep(240)

        assert clock.parks is False
        assert clock.now() == NINE_AM.replace(hour=9, minute=4)
        assert clock.slept == [240]


class TestABackgroundLoopUnderParkMode:
    """The failure park mode exists for, demonstrated on a loop.

    Against the default clock this loop is a spin: `sleep` returns without
    waiting, so it runs as fast as the event loop allows and advances virtual
    time by its whole interval on every turn. A renewer on a 24-hour interval
    burns through a decade of virtual time in a few milliseconds of a test.
    """

    async def loop_for(self, clock: ManualClock, ticks: list[int]) -> None:
        while True:
            await clock.sleep(24 * 3600)
            ticks.append(1)

    async def test_a_daily_loop_steps_once_per_day_advanced(self) -> None:
        clock = ManualClock(NINE_AM, park=True)
        ticks: list[int] = []
        task = asyncio.create_task(self.loop_for(clock, ticks))
        await asyncio.sleep(0)

        assert ticks == [], "the loop ran its body without any time passing"

        for _ in range(3):
            clock.advance(days=1)
            await asyncio.sleep(0)

        task.cancel()
        assert len(ticks) == 3

    async def test_the_same_loop_free_runs_on_the_default_clock(self) -> None:
        """Named rather than hidden: this is what park mode is opting out of,
        and it is why the events, queue, and refresh loops all needed the
        clock *and* needed a way to stop it running away."""
        clock = ManualClock(NINE_AM)
        ticks: list[int] = []
        task = asyncio.create_task(self.loop_for(clock, ticks))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()

        assert len(ticks) > 5
        assert clock.now() - NINE_AM > (
            datetime(2026, 1, 6, tzinfo=UTC) - datetime(2026, 1, 1, tzinfo=UTC)
        ), "twenty event-loop turns should not be able to advance five days"
