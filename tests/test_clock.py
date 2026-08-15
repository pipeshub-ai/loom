"""Time under test: sleeps, schedules, and cron triggers, without waiting.

A durable workflow is mostly a thing that waits, and the waiting is the part
most worth testing — a reminder that fires an hour early is a bug that a test
running in milliseconds will never see unless it can move the clock.

The cases below are the ones that actually go wrong:

* a workflow that sleeps and must resume *once*, not twice and not never,
* a cron trigger that must fire at 9am and not at 8:59,
* ``ctx.now()``, which is journaled — so it must follow the virtual clock on the
  first run and ignore it entirely on a replay,
* and the shape of the failure when someone advances a clock that cannot move.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom import Context, Runtime, step, workflow
from loom.core.exceptions import ConfigurationError
from loom.core.models import ExecutionStatus
from loom.runtime.clock import Clock, ManualClock, SystemClock
from loom.stores import MemoryStore
from loom.testing import advance, advance_to, settled

NINE_AM = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@step(name="clock_note")
async def clock_note(text: str) -> str:
    return text


@workflow(name="clock_reminder")
async def clock_reminder(ctx: Context, _: Any = None) -> str:
    """Wait four minutes, then act."""
    await ctx.sleep(240)
    return await ctx.step(clock_note, "reminded")


@workflow(name="clock_stamped")
async def clock_stamped(ctx: Context, _: Any = None) -> str:
    """Record when it ran."""
    return ctx.now().isoformat()


def _runtime(start: datetime | None = None, **kwargs: Any) -> Runtime:
    rt = Runtime(store=MemoryStore(), clock=ManualClock(start or NINE_AM), **kwargs)
    rt.register_all([clock_reminder, clock_stamped])
    return rt


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


def test_both_clocks_satisfy_the_port() -> None:
    """Design rule 1: a port with one adapter is a guess."""
    assert isinstance(SystemClock(), Clock)
    assert isinstance(ManualClock(), Clock)


def test_a_bare_runtime_is_on_the_wall_clock() -> None:
    """The default must stay the cheap, real one."""
    assert isinstance(Runtime().clock, SystemClock)


def test_advancing_accepts_the_three_spellings() -> None:
    clock = ManualClock(NINE_AM)

    assert clock.advance(timedelta(hours=1)) == NINE_AM + timedelta(hours=1)
    assert clock.advance(60) == NINE_AM + timedelta(hours=1, minutes=1)
    assert clock.advance(minutes=5, seconds=30) == NINE_AM + timedelta(
        hours=1, minutes=6, seconds=30
    )


def test_time_can_be_set_backwards() -> None:
    """Clock skew and late-arriving events are real; the clock does not judge."""
    clock = ManualClock(NINE_AM)
    assert clock.set(NINE_AM - timedelta(days=1)).day == 1


def test_a_naive_datetime_is_read_as_utc() -> None:
    """Rather than blowing up later on a comparison, which is where it surfaces."""
    assert ManualClock(datetime(2026, 3, 2, 9, 0)).now().tzinfo is UTC


# ---------------------------------------------------------------------------
# Sleeping
# ---------------------------------------------------------------------------


async def test_a_sleep_parks_and_advancing_resumes_it() -> None:
    """The whole point: a four-minute wait, tested in milliseconds."""
    rt = _runtime()

    parked = await rt.run(clock_reminder)
    assert parked.status is ExecutionStatus.SUSPENDED

    resumed = await advance(rt, minutes=5)

    assert resumed == [parked.run_id]
    final = await rt.get(parked.run_id)
    assert final.status is ExecutionStatus.COMPLETED
    assert final.output == "reminded"


async def test_advancing_short_of_the_wake_time_changes_nothing() -> None:
    """The assertion that makes the one above mean something.

    A test that only ever advances past the deadline cannot tell a working timer
    from one that fires the moment anybody looks at it.
    """
    rt = _runtime()
    parked = await rt.run(clock_reminder)

    assert await advance(rt, minutes=3) == []
    assert (await rt.get(parked.run_id)).status is ExecutionStatus.SUSPENDED

    assert await advance(rt, minutes=2) == [parked.run_id]
    assert (await rt.get(parked.run_id)).status is ExecutionStatus.COMPLETED


async def test_advancing_again_does_not_resume_a_finished_run() -> None:
    rt = _runtime()
    await rt.run(clock_reminder)
    await advance(rt, minutes=5)

    assert await advance(rt, hours=2) == []


async def test_a_short_sleep_is_held_inline_and_still_moves_the_clock() -> None:
    """Below the inline threshold the runtime waits rather than parking.

    That wait has to go through the clock as well, or a ManualClock is only
    virtual for the long sleeps — and the short ones quietly cost real seconds.
    """
    rt = _runtime(inline_timer_threshold=60.0)

    @workflow(name="clock_brief")
    async def brief(ctx: Context, _: Any = None) -> str:
        await ctx.sleep(30)
        return ctx.now().isoformat()

    rt.register(brief)
    result = await rt.run(brief)

    assert result.status is ExecutionStatus.COMPLETED
    assert datetime.fromisoformat(result.output) == NINE_AM + timedelta(seconds=30)
    assert rt.clock.slept == [30.0]


async def test_a_retry_backoff_does_not_cost_real_time() -> None:
    """The other place the engine waits, and the one nobody remembers."""
    attempts = 0

    @step(name="clock_flaky")
    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("not yet")
        return "ok"

    @workflow(name="clock_retrier")
    async def retrier(ctx: Context, _: Any = None) -> str:
        return await ctx.step(flaky, retry=3)

    rt = _runtime()
    rt.register(retrier)

    result = await rt.run(retrier)

    assert result.output == "ok"
    assert len(rt.clock.slept) == 2
    assert rt.clock.now() > NINE_AM


# ---------------------------------------------------------------------------
# ctx.now()
# ---------------------------------------------------------------------------


async def test_ctx_now_reads_the_virtual_clock() -> None:
    rt = _runtime()
    result = await rt.run(clock_stamped)
    assert datetime.fromisoformat(result.output) == NINE_AM


async def test_ctx_now_is_still_frozen_into_the_journal() -> None:
    """Determinism outranks the clock.

    A replay must reproduce what the run saw, so moving the clock between the
    run and the replay changes nothing. If this ever failed, replay would stop
    being a rehearsal of what happened and become a fresh execution.
    """
    rt = _runtime()
    original = await rt.run(clock_stamped)

    rt.clock.advance(days=365)
    replayed = await rt.replay(original.run_id)

    assert replayed.output == original.output


async def test_a_resumed_run_sees_the_time_it_woke_at() -> None:
    """Not the time it started — the wait really happened."""
    rt = _runtime()

    @workflow(name="clock_two_stamps")
    async def two_stamps(ctx: Context, _: Any = None) -> list[str]:
        before = ctx.now().isoformat()
        await ctx.sleep(3600)
        return [before, ctx.now().isoformat()]

    rt.register(two_stamps)
    parked = await rt.run(two_stamps)
    await advance(rt, hours=2)

    before, after = (await rt.get(parked.run_id)).output
    assert datetime.fromisoformat(before) == NINE_AM
    assert datetime.fromisoformat(after) == NINE_AM + timedelta(hours=2)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def test_a_cron_trigger_fires_at_its_hour_and_not_before() -> None:
    """The trigger-point case: 9am means 9am."""
    from loom.runtime.dispatcher import TriggerDispatcher
    from loom.triggers import Schedule

    @workflow(name="clock_daily", triggers=[Schedule(cron="0 9 * * *")])
    async def daily(ctx: Context, _: Any = None) -> str:
        """Every morning at nine."""
        return await ctx.step(clock_note, "morning")

    rt = _runtime(datetime(2026, 3, 2, 8, 0, tzinfo=UTC))
    rt.register(daily)
    dispatcher = TriggerDispatcher(rt)
    await dispatcher.register(daily)

    assert await advance(rt, minutes=59, dispatcher=dispatcher) == []

    fired = await advance(rt, minutes=1, dispatcher=dispatcher)
    assert len(fired) == 1

    ran = await rt.get(fired[0])
    assert ran.output == "morning"


async def test_a_cron_trigger_fires_once_per_occurrence() -> None:
    """Ticking twice inside one minute must not fire twice."""
    from loom.runtime.dispatcher import TriggerDispatcher
    from loom.triggers import Schedule

    @workflow(name="clock_hourly", triggers=[Schedule(cron="0 * * * *")])
    async def hourly(ctx: Context, _: Any = None) -> str:
        """On the hour."""
        return await ctx.step(clock_note, "tick")

    rt = _runtime(datetime(2026, 3, 2, 8, 30, tzinfo=UTC))
    rt.register(hourly)
    dispatcher = TriggerDispatcher(rt)
    await dispatcher.register(hourly)

    first = await advance(rt, minutes=31, dispatcher=dispatcher)
    again = await advance(rt, seconds=5, dispatcher=dispatcher)
    next_hour = await advance(rt, hours=1, dispatcher=dispatcher)

    assert len(first) == 1
    assert again == []
    assert len(next_hour) == 1


async def test_advance_to_reaches_a_named_moment() -> None:
    """Because computing the delta to "the first of next month" by hand is
    where the off-by-one lives."""
    rt = _runtime(datetime(2026, 3, 2, 8, 0, tzinfo=UTC))
    parked = await rt.run(clock_reminder)

    fired = await advance_to(rt, datetime(2026, 3, 2, 8, 5, tzinfo=UTC))

    assert fired == [parked.run_id]


# ---------------------------------------------------------------------------
# Sharp edges
# ---------------------------------------------------------------------------


async def test_advancing_a_wall_clock_says_what_to_do_about_it() -> None:
    """The likely mistake, so the message has to carry the fix."""
    with pytest.raises(ConfigurationError) as caught:
        await advance(Runtime(), minutes=1)

    assert "ManualClock" in str(caught.value)
    assert "SystemClock" in str(caught.value)


async def test_settling_reports_a_run_that_never_finishes() -> None:
    """Silence would be indistinguishable from a workflow that did nothing."""
    import asyncio

    rt = _runtime()
    rt._background.add(asyncio.ensure_future(asyncio.sleep(30)))
    try:
        with pytest.raises(TimeoutError, match="still in flight"):
            await settled(rt, timeout=0.1)
    finally:
        await rt.shutdown()


async def test_a_run_binds_the_clock_it_started_with() -> None:
    """Swapping the clock mid-run would move time under a run already in it."""
    rt = _runtime()
    parked = await rt.run(clock_reminder)

    rt.clock = SystemClock()

    assert (await rt.get(parked.run_id)).status is ExecutionStatus.SUSPENDED


# ---------------------------------------------------------------------------
# Triggers against the store that actually persists them
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduled():
    """A runtime and dispatcher over one MemoryStore, at 08:00.

    The dispatcher is given no ``trigger_store``, so it resolves the Runtime's
    own — ``MemoryStore`` implements ``TriggerStore``, which means these tests
    exercise the real persistence path rather than a substitute the production
    code never sees.
    """
    from loom.runtime.dispatcher import TriggerDispatcher

    store = MemoryStore()
    rt = Runtime(store=store, clock=ManualClock(datetime(2026, 3, 2, 8, 0, tzinfo=UTC)))
    return rt, TriggerDispatcher(rt), store


def _at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def test_the_dispatcher_uses_the_runtimes_own_store(scheduled) -> None:
    """Not a private dict — a restart has to find the schedule again."""
    from loom.triggers import Schedule

    _, dispatcher, store = scheduled

    @workflow(name="clock_persisted", triggers=[Schedule(cron="0 9 * * *")])
    async def persisted(ctx: Context, _: Any = None) -> str:
        """Nine sharp."""
        return await ctx.step(clock_note, "ran")

    assert await dispatcher.register(persisted) == 1

    saved = await store.list_triggers(workflow="clock_persisted")
    assert len(saved) == 1
    assert saved[0].next_fire_at == _at(9)


async def test_a_second_dispatcher_inherits_the_schedule(scheduled) -> None:
    """A restart mid-day must not re-fire this morning's run.

    The schedule lives in the store, so a fresh dispatcher over the same store
    picks up where the last one left off rather than starting the cron over.
    """
    from loom.runtime.dispatcher import TriggerDispatcher
    from loom.triggers import Schedule

    rt, dispatcher, _ = scheduled

    @workflow(name="clock_restarted", triggers=[Schedule(cron="0 9 * * *")])
    async def restarted(ctx: Context, _: Any = None) -> str:
        """Nine sharp."""
        return await ctx.step(clock_note, "ran")

    await dispatcher.register(restarted)
    assert len(await advance(rt, hours=1, dispatcher=dispatcher)) == 1

    successor = TriggerDispatcher(rt)
    assert await advance(rt, hours=2, dispatcher=successor) == []

    tomorrow = await advance_to(rt, _at(9, day=3), dispatcher=successor)
    assert len(tomorrow) == 1


async def test_an_interval_trigger_fires_every_period(scheduled) -> None:
    from loom.triggers import Interval

    rt, dispatcher, _ = scheduled

    @workflow(name="clock_every_15", triggers=[Interval(every=900)])
    async def every_15(ctx: Context, _: Any = None) -> str:
        """Quarter-hourly."""
        return await ctx.step(clock_note, "poll")

    await dispatcher.register(every_15)

    assert await advance(rt, minutes=14, dispatcher=dispatcher) == []
    assert len(await advance(rt, minutes=1, dispatcher=dispatcher)) == 1
    assert len(await advance(rt, minutes=15, dispatcher=dispatcher)) == 1
    assert len(await advance(rt, minutes=15, dispatcher=dispatcher)) == 1

    assert len(await rt.list_runs(workflow="clock_every_15")) == 3


async def test_a_long_jump_fires_once_not_once_per_missed_period(scheduled) -> None:
    """A worker down for a day comes back to a backlog of one.

    Firing once per missed occurrence would hammer whatever the workflow talks
    to, at exactly the moment it has just come back up. Catch-up is a policy a
    host can add; stampeding is not a default anyone wants.
    """
    from loom.triggers import Interval

    rt, dispatcher, _ = scheduled

    @workflow(name="clock_catchup", triggers=[Interval(every=60)])
    async def catchup(ctx: Context, _: Any = None) -> str:
        """Every minute."""
        return await ctx.step(clock_note, "poll")

    await dispatcher.register(catchup)

    fired = await advance(rt, hours=24, dispatcher=dispatcher)

    assert len(fired) == 1
    assert len(await rt.list_runs(workflow="clock_catchup")) == 1


async def test_the_run_a_trigger_starts_records_the_schedule(scheduled) -> None:
    """A scheduled run must be tellable from a hand-started one."""
    from loom.core.models import TriggerKind
    from loom.triggers import Schedule

    rt, dispatcher, _ = scheduled

    @workflow(name="clock_tagged", triggers=[Schedule(cron="0 9 * * *")])
    async def tagged(ctx: Context, _: Any = None) -> str:
        """Nine sharp."""
        return await ctx.step(clock_note, "ran")

    await dispatcher.register(tagged)
    fired = await advance(rt, hours=1, dispatcher=dispatcher)

    record = await rt.get(fired[0])
    assert record.trigger is TriggerKind.SCHEDULE
    assert record.status is ExecutionStatus.COMPLETED


async def test_a_disabled_trigger_stays_quiet(scheduled) -> None:
    from loom.triggers import Schedule

    rt, dispatcher, store = scheduled

    @workflow(name="clock_disabled", triggers=[Schedule(cron="0 9 * * *")])
    async def disabled(ctx: Context, _: Any = None) -> str:
        """Nine sharp."""
        return await ctx.step(clock_note, "ran")

    await dispatcher.register(disabled)
    saved = (await store.list_triggers(workflow="clock_disabled"))[0]
    saved.enabled = False
    await store.save_trigger(saved)

    assert await advance(rt, hours=2, dispatcher=dispatcher) == []


async def test_a_scheduled_workflow_that_sleeps_still_resumes(scheduled) -> None:
    """The two schedulers in one move: the trigger starts it, the timer finishes it."""
    from loom.triggers import Schedule

    rt, dispatcher, _ = scheduled

    @workflow(name="clock_slow_daily", triggers=[Schedule(cron="0 9 * * *")])
    async def slow_daily(ctx: Context, _: Any = None) -> str:
        """Start at nine, finish after a wait."""
        await ctx.sleep(1800)
        return await ctx.step(clock_note, "finished")

    await dispatcher.register(slow_daily)

    started = await advance(rt, hours=1, dispatcher=dispatcher)
    assert (await rt.get(started[0])).status is ExecutionStatus.SUSPENDED

    await advance(rt, minutes=31, dispatcher=dispatcher)
    assert (await rt.get(started[0])).output == "finished"
