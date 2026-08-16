"""Cron across daylight-saving boundaries.

A daily cron in a zone that observes DST meets two days a year that are not
24 hours long. On one, its local time does not exist; on the other, it happens
twice. Both are silent: nothing errors, the job simply runs twice or not at
all, six months apart, on a day nobody is looking.

`CronSchedule` computes in the target zone and converts to UTC, which is the
right shape for getting this correct — but nothing asserted the outcome, so
this file states what the two days must produce. The properties are written
per-day rather than per-instant because "how many times did it run today" is
the question an operator actually has.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from loom.triggers.cron import CronSchedule

LONDON = ZoneInfo("Europe/London")

#: EU transitions: clocks go forward on the last Sunday of March and back on
#: the last Sunday of October.
SPRING_FORWARD = datetime(2026, 3, 29, tzinfo=UTC)
AUTUMN_BACK = datetime(2026, 10, 25, tzinfo=UTC)


def fires_between(expression: str, start: datetime, end: datetime, tz: str):
    """Every occurrence in ``[start, end)``, as the dispatcher would walk them."""
    schedule = CronSchedule.parse(expression, timezone=tz)
    moments = []
    cursor = start
    while True:
        cursor = schedule.next_after(cursor)
        if cursor >= end:
            return moments
        moments.append(cursor)


def _fires_in_local_day(day: datetime, expression: str = "0 * * * *") -> int:
    """How many times *expression* fires during that London calendar day.

    Bounded by local midnight either side, so the window is the day a person
    would point at — 23 real hours in March, 25 in October — rather than a
    fixed 24 that silently straddles a transition.
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=LONDON)
    start = midnight.astimezone(UTC)
    tomorrow = midnight + timedelta(days=1)
    end = datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=LONDON
    ).astimezone(UTC)
    return len(
        fires_between(expression, start - timedelta(seconds=1), end, "Europe/London")
    )


class TestSpringForward:
    """The day an hour does not exist."""

    def test_a_daily_cron_in_the_lost_hour_does_not_run_twice(self) -> None:
        """01:30 London does not happen on this date.

        Skipping it is defensible and firing it once is defensible. Firing it
        *twice* is not, and neither is an exception.
        """
        day = fires_between(
            "30 1 * * *",
            SPRING_FORWARD - timedelta(hours=6),
            SPRING_FORWARD + timedelta(hours=24),
            "Europe/London",
        )

        assert len(day) <= 1, f"the lost hour produced {len(day)} runs: {day}"

    def test_a_daily_cron_outside_the_lost_hour_runs_exactly_once(self) -> None:
        day = fires_between(
            "30 9 * * *",
            SPRING_FORWARD,
            SPRING_FORWARD + timedelta(hours=24),
            "Europe/London",
        )

        assert len(day) == 1, f"09:30 ran {len(day)} times on the short day"

    def test_the_next_day_is_unaffected(self) -> None:
        """The transition must not leave the schedule shifted afterwards."""
        after = fires_between(
            "30 9 * * *",
            SPRING_FORWARD + timedelta(days=1),
            SPRING_FORWARD + timedelta(days=2),
            "Europe/London",
        )

        assert len(after) == 1
        assert after[0].astimezone(LONDON).hour == 9
        assert after[0].astimezone(LONDON).minute == 30


class TestAutumnBack:
    """The day an hour happens twice."""

    def test_a_daily_cron_in_the_repeated_hour_runs_once(self) -> None:
        """01:30 London happens twice on this date — once BST, once GMT.

        Firing on both is the classic duplicate: a nightly reconciliation runs
        against yesterday twice, and the second pass sees the first pass's
        writes.
        """
        day = fires_between(
            "30 1 * * *",
            AUTUMN_BACK - timedelta(hours=6),
            AUTUMN_BACK + timedelta(hours=24),
            "Europe/London",
        )

        assert len(day) == 1, f"the repeated hour produced {len(day)} runs: {day}"

    def test_a_daily_cron_outside_the_repeated_hour_runs_once(self) -> None:
        day = fires_between(
            "30 9 * * *",
            AUTUMN_BACK,
            AUTUMN_BACK + timedelta(hours=24),
            "Europe/London",
        )

        assert len(day) == 1


class TestTheWalkIsAlwaysForward:
    """Whatever a transition does, the sequence must stay strictly increasing.

    The dispatcher advances by asking for the next moment after the last one.
    A schedule that ever returns a moment that is not later would either spin
    the dispatcher or park a trigger permanently in the past — and a DST
    boundary is exactly where an off-by-one in a local/UTC conversion shows up.
    """

    def test_a_year_of_a_daily_cron_never_goes_backwards(self) -> None:
        moments = fires_between(
            "30 1 * * *",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "Europe/London",
        )

        assert moments == sorted(moments)
        assert len(set(moments)) == len(moments), "a moment repeated"

    def test_a_daily_cron_produces_about_a_year_of_days(self) -> None:
        """365 days, minus at most the one the lost hour swallows."""
        moments = fires_between(
            "30 1 * * *",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "Europe/London",
        )

        assert 364 <= len(moments) <= 365, f"{len(moments)} occurrences in 2026"

    def test_an_hourly_cron_fires_once_per_local_hour_label(self) -> None:
        """Counted over the *local* day, which is what "hourly" means to a user.

        The short day has 23 hour labels and the long day has 25 real hours but
        still only 24 labels — 01:00 occurs twice and fires once. Firing on
        both passes is the classic autumn duplicate: a nightly reconciliation
        runs twice, and the second pass sees the first pass's writes. Firing
        once is the safer reading of an ambiguous wall-clock time, and it is
        what this implementation does.

        Anchoring the count to the local day rather than to a UTC window is the
        part that matters: a walk anchored to UTC would report a flat 24 on
        every day of the year and this test would never notice a transition.
        """
        assert _fires_in_local_day(SPRING_FORWARD) == 23
        assert _fires_in_local_day(AUTUMN_BACK) == 24
        assert _fires_in_local_day(datetime(2026, 6, 1, tzinfo=UTC)) == 24


class TestUTCIsUnaffected:
    """The default zone has no transitions, and must stay boring."""

    def test_a_daily_utc_cron_runs_once_a_day_across_a_transition(self) -> None:
        moments = fires_between(
            "30 1 * * *",
            SPRING_FORWARD - timedelta(days=1),
            SPRING_FORWARD + timedelta(days=2),
            "UTC",
        )

        assert len(moments) == 3
