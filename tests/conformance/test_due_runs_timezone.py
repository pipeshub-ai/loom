"""A wake time is an instant, not a string — on every backend.

`due_runs(now)` answers "which parked runs should wake?". Memory and Postgres
answer it by comparing instants. SQLite and Mongo keep `wake_at` as TEXT and
answer it by comparing strings, which is the same question only while every
string carries the same offset.

It does not. `2026-08-17T15:00:00+05:30` is 09:30 UTC — earlier than
`2026-08-17T12:00:00+00:00` as an instant, later as a string. So a run parked on
a tz-aware non-UTC wake time was never returned by `due_runs` on two of the four
backends, and slept forever: no error, no log line, and indistinguishable from a
run that is patiently waiting for a time that has not arrived.

The reason this survived the conformance suite is worth keeping in view. Every
other test here builds its timestamps from `datetime.now(UTC)`, where the string
and the instant orderings agree — so the suite exercised the comparison
thoroughly and never once disagreed with it. The bug needed a *non-UTC* input to
appear, and nothing supplied one. `ctx.sleep_until` normalises a naive datetime
and passes an aware one through untouched, so any caller doing
`sleep_until(local_9am)` reached it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from conformance.backends import ALL_BACKENDS, open_store
from loom.core.models import ExecutionRecord, ExecutionStatus

IST = timezone(timedelta(hours=5, minutes=30))
CHICAGO = timezone(timedelta(hours=-5))

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


def parked(run_id: str, wake_at: datetime) -> ExecutionRecord:
    return ExecutionRecord(
        run_id=run_id,
        workflow="reminder",
        status=ExecutionStatus.SUSPENDED,
        wake_at=wake_at,
    )


class TestWakeTimeIsAnInstant:
    async def test_a_past_wake_time_east_of_utc_is_due(self, store) -> None:
        """15:00+05:30 is 09:30 UTC — two and a half hours before `NOW`.

        As a string it sorts *after* `NOW`, which is the whole bug.
        """
        await store.create_execution(parked("run-ist", datetime(2026, 8, 17, 15, 0, tzinfo=IST)))

        assert "run-ist" in await store.due_runs(NOW)

    async def test_a_past_wake_time_west_of_utc_is_due(self, store) -> None:
        """The other direction, which string comparison happens to get right.

        Kept so a fix that normalises only one sign is caught.
        """
        await store.create_execution(
            parked("run-chi", datetime(2026, 8, 17, 6, 0, tzinfo=CHICAGO))
        )

        assert "run-chi" in await store.due_runs(NOW)

    async def test_a_future_wake_time_east_of_utc_is_not_due(self, store) -> None:
        """The negative half.

        23:00+05:30 is 17:30 UTC — five hours *after* `NOW`. A fix that made
        everything due would pass the two tests above and be worse than the bug.
        """
        await store.create_execution(
            parked("run-later", datetime(2026, 8, 17, 23, 0, tzinfo=IST))
        )

        assert "run-later" not in await store.due_runs(NOW)

    async def test_a_naive_wake_time_is_read_as_utc(self, store) -> None:
        """Matching `Context.sleep_until`, which stamps UTC on a naive value."""
        await store.create_execution(parked("run-naive", datetime(2026, 8, 17, 9, 30)))

        assert "run-naive" in await store.due_runs(NOW)

    async def test_a_non_utc_now_asks_the_same_question(self, store) -> None:
        """The offset can also arrive on the *query* side.

        `NOW` expressed as 17:30+05:30 is the same instant, so it must return
        the same set — otherwise a scheduler running with a local clock reads a
        different due list from one running in UTC.
        """
        await store.create_execution(parked("run-a", datetime(2026, 8, 17, 9, 30, tzinfo=UTC)))
        await store.create_execution(parked("run-b", datetime(2026, 8, 17, 23, 0, tzinfo=IST)))

        due = await store.due_runs(NOW.astimezone(IST))

        assert "run-a" in due
        assert "run-b" not in due

    async def test_ordering_is_by_instant_not_by_offset(self, store) -> None:
        """`due_runs` orders by wake time, and callers take the head of it.

        Two runs whose string order is the reverse of their instant order.
        """
        await store.create_execution(
            parked("run-first", datetime(2026, 8, 17, 13, 0, tzinfo=IST))  # 07:30Z
        )
        await store.create_execution(
            parked("run-second", datetime(2026, 8, 17, 9, 0, tzinfo=UTC))  # 09:00Z
        )

        due = await store.due_runs(NOW)

        assert due.index("run-first") < due.index("run-second")
