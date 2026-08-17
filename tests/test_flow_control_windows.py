"""Admission windows have to *close* as well as open.

Every flow-control policy but concurrency and singleton is a window over time:
a debounce coalesces triggers inside one, a rate limit counts admissions within
one, a throttle enforces a minimum gap, a batch accumulates until one elapses.
The existing tests all prove the same half — that entering the window has the
declared effect. None of them could prove the other half, because
`AdmissionController` read `time.monotonic()`, and there is no way to move that
from a test short of sleeping for real.

That gap is the dangerous one. A debounce that admits the first trigger and
then never releases is *indistinguishable from a working debounce* until
somebody notices the second report never went out — and it looks like a broken
trigger, not a broken admission window, so it is debugged in the wrong file.

`AdmissionController(clock=...)` is what makes the far edge reachable. Each
test here advances past a window and asserts the decision changes back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loom.runtime.clock import ManualClock, SystemClock
from loom.runtime.flowcontrol import (
    AdmissionController,
    AdmissionDecision,
    BatchPolicy,
    DebouncePolicy,
    FlowControlPolicy,
    RateLimitPolicy,
    ThrottlePolicy,
)

NINE_AM = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def a_controller() -> tuple[AdmissionController, ManualClock]:
    clock = ManualClock(NINE_AM)
    return AdmissionController(clock=clock), clock


class TestDebounce:
    """The window a report is coalesced into, and the moment it reopens."""

    async def test_a_second_trigger_inside_the_window_is_debounced(self) -> None:
        control, _ = a_controller()
        policy = FlowControlPolicy(debounce=DebouncePolicy(period_seconds=30))

        first = await control.evaluate("report", policy)
        second = await control.evaluate("report", policy)

        assert first.decision is AdmissionDecision.ADMIT
        assert second.decision is AdmissionDecision.DEBOUNCE

    async def test_the_window_expires_and_the_next_trigger_is_admitted(self) -> None:
        """The half nothing proved.

        A debounce that never releases and a debounce that works look the same
        from inside the window; only crossing the far edge tells them apart.
        """
        control, clock = a_controller()
        policy = FlowControlPolicy(debounce=DebouncePolicy(period_seconds=30))

        await control.evaluate("report", policy)
        assert (
            await control.evaluate("report", policy)
        ).decision is AdmissionDecision.DEBOUNCE

        clock.advance(seconds=31)

        assert (await control.evaluate("report", policy)).decision is (
            AdmissionDecision.ADMIT
        ), "the debounce window never reopened — triggers are lost forever"

    async def test_the_window_is_measured_from_the_last_trigger_not_the_last_admit(
        self,
    ) -> None:
        """A stream of triggers inside the window keeps pushing it out.

        That is what "coalesce" means: the run fires once the noise *stops*,
        not once the first window since the last run has elapsed. Measuring
        from the admission instead would fire mid-burst.
        """
        control, clock = a_controller()
        policy = FlowControlPolicy(debounce=DebouncePolicy(period_seconds=30))

        await control.evaluate("report", policy)
        for _ in range(4):
            clock.advance(seconds=20)
            assert (
                await control.evaluate("report", policy)
            ).decision is AdmissionDecision.DEBOUNCE

        clock.advance(seconds=31)
        assert (
            await control.evaluate("report", policy)
        ).decision is AdmissionDecision.ADMIT

    async def test_the_reported_delay_shrinks_as_the_window_closes(self) -> None:
        """`delay_seconds` is what a caller reschedules on, so it has to count
        down. A constant one would make a retry land at the same instant every
        time, debounce again, and loop."""
        control, clock = a_controller()
        policy = FlowControlPolicy(debounce=DebouncePolicy(period_seconds=60))

        await control.evaluate("report", policy)
        clock.advance(seconds=10)
        early = await control.evaluate("report", policy)

        assert early.delay_seconds == pytest.approx(50.0)

    async def test_debounce_keys_hold_separate_windows(self) -> None:
        control, clock = a_controller()
        policy = FlowControlPolicy(
            debounce=DebouncePolicy(period_seconds=30, key="tenant")
        )

        await control.evaluate("report", policy, partition_key="a")
        clock.advance(seconds=31)

        assert (
            await control.evaluate("report", policy, partition_key="a")
        ).decision is AdmissionDecision.ADMIT


class TestRateLimit:
    """A fixed count per period, and what happens when the period rolls over."""

    async def test_the_budget_is_spent_within_the_period(self) -> None:
        control, _ = a_controller()
        policy = FlowControlPolicy(
            rate_limit=RateLimitPolicy(requests=2, period_seconds=60)
        )

        assert (await control.evaluate("sync", policy)).decision is (
            AdmissionDecision.ADMIT
        )
        assert (await control.evaluate("sync", policy)).decision is (
            AdmissionDecision.ADMIT
        )
        assert (await control.evaluate("sync", policy)).decision is (
            AdmissionDecision.DELAY
        )

    async def test_the_period_rolls_over_and_the_budget_returns(self) -> None:
        """The other half nothing proved. A rate limit whose log never prunes
        admits `requests` runs and then refuses forever, which reads as a
        workflow that stopped working rather than as a limiter."""
        control, clock = a_controller()
        policy = FlowControlPolicy(
            rate_limit=RateLimitPolicy(requests=2, period_seconds=60)
        )

        await control.evaluate("sync", policy)
        await control.evaluate("sync", policy)
        assert (
            await control.evaluate("sync", policy)
        ).decision is AdmissionDecision.DELAY

        clock.advance(seconds=61)

        assert (await control.evaluate("sync", policy)).decision is (
            AdmissionDecision.ADMIT
        ), "the rate-limit period never rolled over — the flow is refused forever"

    async def test_it_slides_rather_than_resetting_wholesale(self) -> None:
        """Entries age out one at a time, so spending the budget at 0s and 30s
        does not free both of them at 60s. A window that reset wholesale would
        let a burst of `requests` recur twice in one period."""
        control, clock = a_controller()
        policy = FlowControlPolicy(
            rate_limit=RateLimitPolicy(requests=2, period_seconds=60)
        )

        await control.evaluate("sync", policy)
        clock.advance(seconds=30)
        await control.evaluate("sync", policy)

        clock.advance(seconds=31)  # 61s: the first has aged out, the second has not
        assert (
            await control.evaluate("sync", policy)
        ).decision is AdmissionDecision.ADMIT
        assert (
            await control.evaluate("sync", policy)
        ).decision is AdmissionDecision.DELAY

    async def test_the_reported_delay_reaches_the_moment_the_oldest_expires(
        self,
    ) -> None:
        control, clock = a_controller()
        policy = FlowControlPolicy(
            rate_limit=RateLimitPolicy(requests=1, period_seconds=60)
        )

        await control.evaluate("sync", policy)
        clock.advance(seconds=15)
        refused = await control.evaluate("sync", policy)

        assert refused.delay_seconds == pytest.approx(45.0)


class TestThrottle:
    """A minimum gap between admissions, and the gap actually elapsing."""

    async def test_a_second_admission_too_soon_is_delayed(self) -> None:
        control, _ = a_controller()
        policy = FlowControlPolicy(throttle=ThrottlePolicy(max_per_second=2))

        await control.evaluate("call", policy)
        assert (
            await control.evaluate("call", policy)
        ).decision is AdmissionDecision.DELAY

    async def test_waiting_out_the_gap_admits(self) -> None:
        control, clock = a_controller()
        policy = FlowControlPolicy(throttle=ThrottlePolicy(max_per_second=2))

        await control.evaluate("call", policy)
        clock.advance(0.6)

        assert (await control.evaluate("call", policy)).decision is (
            AdmissionDecision.ADMIT
        ), "the throttle gap never elapsed — one admission and then silence"


class TestBatch:
    """Accumulate until full *or* until the window closes — both flush."""

    async def test_the_window_expiring_flushes_a_partial_batch(self) -> None:
        """The reason a batch has a window at all. Without the time half, a
        batch that never reaches `max_size` sits unflushed forever, and the
        last few items of a quiet day are simply never processed."""
        control, clock = a_controller()
        policy = FlowControlPolicy(
            batch=BatchPolicy(max_size=10, window_seconds=30)
        )

        assert (
            await control.evaluate("ingest", policy)
        ).decision is AdmissionDecision.BATCH

        clock.advance(seconds=31)

        assert (await control.evaluate("ingest", policy)).decision is (
            AdmissionDecision.ADMIT
        ), "the batch window never closed — a partial batch is never flushed"

    async def test_a_full_batch_flushes_before_the_window_closes(self) -> None:
        control, _ = a_controller()
        policy = FlowControlPolicy(batch=BatchPolicy(max_size=3, window_seconds=300))

        assert (
            await control.evaluate("ingest", policy)
        ).decision is AdmissionDecision.BATCH
        assert (
            await control.evaluate("ingest", policy)
        ).decision is AdmissionDecision.BATCH
        assert (
            await control.evaluate("ingest", policy)
        ).decision is AdmissionDecision.ADMIT

    async def test_a_flush_starts_the_next_window_fresh(self) -> None:
        control, clock = a_controller()
        policy = FlowControlPolicy(batch=BatchPolicy(max_size=2, window_seconds=30))

        await control.evaluate("ingest", policy)
        await control.evaluate("ingest", policy)  # flushes

        clock.advance(seconds=5)
        assert (await control.evaluate("ingest", policy)).decision is (
            AdmissionDecision.BATCH
        ), "the second batch inherited the first one's start time"


class TestTheDefault:
    """Nothing changes for a host that passes no clock."""

    async def test_a_bare_controller_is_on_the_wall_clock(self) -> None:
        control = AdmissionController()
        assert isinstance(control._clock, SystemClock)

    async def test_an_explicit_moment_overrides_the_clock(self) -> None:
        """`now=` exists for a caller holding a moment already. Two policies
        evaluated for one trigger have to agree about when they were
        evaluated."""
        control, clock = a_controller()
        policy = FlowControlPolicy(debounce=DebouncePolicy(period_seconds=30))

        base = clock.now().timestamp()
        await control.evaluate("report", policy, now=base)

        assert (
            await control.evaluate("report", policy, now=base + 31)
        ).decision is AdmissionDecision.ADMIT
