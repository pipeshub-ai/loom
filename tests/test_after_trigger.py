"""A delay stated up front, and the thing that makes it happen.

The generation this was written for: ``can you tell me a joke after 2
minutes``. It came back as ``ctx.sleep(timedelta(minutes=2))`` at the top of a
body that had done nothing yet — so the run parked immediately, and since no
CLI command has ever ticked a scheduler, nothing woke it. A workflow that
reports ``suspended`` and never resumes reads exactly like a broken workflow.

``After`` is the gap ``Schedule`` and ``Interval`` leave between them, and the
half that matters is not the spec but ``tick_schedules`` — a declared trigger
nothing drives is a statement no dispatcher has heard.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from loom import Context, Runtime, workflow
from loom.facade import LocalFacade
from loom.runtime.dispatcher import _next_fire_from_record, _trigger_id
from loom.stores import MemoryStore
from loom.triggers import After, Interval


@workflow(name="joke_after", triggers=[After(seconds=2)])
async def joke_after(ctx: Context, input_data=None) -> str:
    return "a joke"


def _facade() -> LocalFacade:
    rt = Runtime(store=MemoryStore())
    rt.register(joke_after)
    return LocalFacade(runtime=rt)


async def _settled(facade: LocalFacade, run_id: str, tries: int = 60) -> object:
    """The run's output, once it stops moving.

    ``tick_schedules`` returns runs it *started*; the drive runs on the loop
    behind it. That is why the command follows the run rather than reading it
    once, and why a test has to as well.
    """
    for _ in range(tries):
        run = await facade.get(run_id)
        if run["status"] not in ("pending", "running"):
            return run["output"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never settled")


class TestTheSpec:
    def test_the_delay_adds_up(self) -> None:
        assert After(minutes=2).delay == 120
        assert After(hours=1, minutes=30).delay == 5400
        assert After(days=1).delay == 86400

    def test_next_fire_is_the_delay_past_the_moment_it_is_given(self) -> None:
        now = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)

        assert After(minutes=2).next_fire(now) == now + timedelta(minutes=2)

    def test_a_zero_delay_is_refused_and_names_the_alternative(self) -> None:
        with pytest.raises(ValueError, match="Manual"):
            After()
        with pytest.raises(ValueError, match="greater than zero"):
            After(seconds=-1)

    def test_it_does_not_publish_seconds(self) -> None:
        """The trap. ``seconds`` in a stored spec *is* an interval.

        ``_next_fire_from_record`` reads ``cron`` then ``seconds`` out of the
        persisted spec and answers ``None`` for anything else — which is what
        retires a one-shot. Publishing the delay under ``seconds`` would make
        every stored ``After`` indistinguishable from an ``Interval``, and it
        would repeat for ever: the single behaviour this spec rules out.
        """
        described = After(minutes=2).describe()

        assert "seconds" not in described
        assert described["after_seconds"] == 120
        assert described["once"] is True

    def test_the_record_cannot_reproduce_a_next_fire(self) -> None:
        from loom.core.models import TriggerKind, TriggerRecord

        record = TriggerRecord(
            trigger_id="t",
            workflow="w",
            kind=TriggerKind.SCHEDULE,
            spec=After(minutes=2).describe(),
            next_fire_at=datetime.now(UTC),
        )

        assert _next_fire_from_record(record, datetime.now(UTC)) is None

    def test_two_delays_are_two_triggers(self) -> None:
        """``after_seconds`` decides when it fires, so it decides identity."""
        assert _trigger_id("w", After(minutes=2)) != _trigger_id("w", After(minutes=3))
        assert _trigger_id("w", After(minutes=2)) == _trigger_id("w", After(minutes=2))

    def test_it_is_not_confused_with_an_interval_of_the_same_length(self) -> None:
        assert _trigger_id("w", After(seconds=300)) != _trigger_id("w", Interval(every=300))


class TestFiringIt:
    async def test_it_fires_once_after_the_delay(self) -> None:
        facade = _facade()
        await facade.wire_triggers("joke_after")

        assert await facade.tick_schedules() == [], "not before it is due"

        await asyncio.sleep(2.2)
        started = await facade.tick_schedules()

        assert len(started) == 1
        # Started, not settled: the dispatcher submits and the drive runs on
        # the loop behind it, which is why the command follows the run rather
        # than reading it once.
        assert await _settled(facade, started[0]["run_id"]) == "a joke"

    async def test_it_never_fires_again(self) -> None:
        facade = _facade()
        await facade.wire_triggers("joke_after")
        await asyncio.sleep(2.2)
        assert await facade.tick_schedules()

        await asyncio.sleep(2.2)

        assert await facade.tick_schedules() == [], "one shot means one"

    async def test_re_wiring_keeps_one_record_and_its_fire_time(self) -> None:
        """Registration runs on every boot; the delay is from the first one.

        Recomputing it here is what would make a process restarting more often
        than its delay never fire at all — the same trap ``Schedule`` documents.
        """
        facade = _facade()
        first = await facade.wire_triggers("joke_after")
        again = await facade.wire_triggers("joke_after")

        assert len(first) == len(again) == 1
        assert first[0]["next_fire_at"] == again[0]["next_fire_at"]

    async def test_ticking_also_advances_the_engine_timers(self) -> None:
        """``ctx.sleep`` was the other half of the same silence.

        Nothing in the CLI has ever ticked, so a run parked on a timer stayed
        parked for as long as the process lived — which is what made the
        original ``ctx.sleep(minutes=2)`` joke never arrive. The dispatcher's
        tick drives the engine's timers on its way through, so the one loop
        covers both.
        """
        from loom.runtime.clock import ManualClock

        @workflow(name="parks_on_a_timer")
        async def parks(ctx: Context, _=None) -> str:
            await ctx.sleep(timedelta(minutes=30))
            return "awake"

        clock = ManualClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
        rt = Runtime(store=MemoryStore(), clock=clock)
        rt.register(parks)
        facade = LocalFacade(runtime=rt)
        started = await facade.start("parks_on_a_timer", None, wait=True)
        assert started["status"] == "suspended", "parked, with nothing driving it"

        clock.advance(timedelta(minutes=31))
        for _ in range(40):
            await facade.tick_schedules()
            run = await facade.get(started["run_id"])
            if run["status"] == "completed":
                break
            await asyncio.sleep(0.05)

        assert run["output"] == "awake"


class TestTheCommandRoutesADelayToTheScheduler:
    """A cron's first run is a rehearsal; a one-shot's is the whole event."""

    def test_a_declared_delay_is_recognised(self) -> None:
        from loom.cli.commands import _one_shot_delay

        assert _one_shot_delay([{"kind": "After", "fields": {"minutes": 2}}]) == 120
        assert _one_shot_delay([{"kind": "After", "fields": {"seconds": 30}}]) == 30

    def test_other_triggers_still_run_now(self) -> None:
        from loom.cli.commands import _one_shot_delay

        assert _one_shot_delay([]) is None
        assert _one_shot_delay([{"kind": "Schedule", "fields": {"cron": "0 9 * * *"}}]) is None
        assert _one_shot_delay([{"kind": "Interval", "fields": {"every": 300}}]) is None
