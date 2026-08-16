"""An abandoned run is findable again — on every backend.

`reclaim_orphans` is the only thing that covers a run whose worker went away:
it is RUNNING rather than waiting, so no timer scans for it, and the sole
handle anyone has on it is an expired `lease_expires_at`. The engine settles
that field on its way out (`Runtime._settle_lease`), and `test_shutdown.py`
pins the behaviour against MemoryStore.

What that leaves unasserted is the round trip. A lease is a *timestamp on a
record*, and the four stores disagree about timestamps in exactly the ways
timestamps go wrong: SQLite keeps ISO text, Mongo keeps BSON datetimes that
come back naive, Postgres keeps `timestamptz`. A lease that survives a dict and
not a document is not a recovery mechanism, it is a recovery mechanism on one
deployment — which is the bug class this whole directory exists for.

So: write the record the abandon path writes, read it back the way
`reclaim_orphans` reads it, and require the same answer everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conformance.backends import ALL_BACKENDS, open_store
from loom.core.models import ExecutionRecord, ExecutionStatus

NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


def abandoned(run_id: str, *, expires_at: datetime = NOW) -> ExecutionRecord:
    """What `_settle_lease` leaves behind when a drive is interrupted.

    RUNNING, owner kept as a breadcrumb, lease expired at the moment we knew we
    were not coming back.
    """
    return ExecutionRecord(
        run_id=run_id,
        workflow="held",
        status=ExecutionStatus.RUNNING,
        lease_owner="node-a",
        lease_expires_at=expires_at,
    )


def as_utc(moment: datetime) -> datetime:
    """Mongo returns naive UTC; the engine's own `_as_utc` does the same."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class TestAnExpiredLeaseSurvivesTheStore:
    async def test_the_record_comes_back_with_its_lease(self, store) -> None:
        await store.create_execution(abandoned("run-1"))

        found = await store.get_execution("run-1")

        assert found is not None
        assert found.lease_owner == "node-a"
        assert found.lease_expires_at is not None
        assert as_utc(found.lease_expires_at) == NOW

    async def test_it_is_listed_among_running_runs(self, store) -> None:
        """How `reclaim_orphans` finds candidates in the first place."""
        await store.create_execution(abandoned("run-1"))

        listed = await store.list_executions(status=ExecutionStatus.RUNNING)

        assert [record.run_id for record in listed] == ["run-1"]

    async def test_the_expiry_still_compares_after_the_round_trip(self, store) -> None:
        """The actual predicate: expired is in the past, live is not.

        Comparing rather than just reading, because a store that returns a
        naive datetime raises `TypeError` here instead of quietly matching
        nothing — and a lease that never compares as expired is a run that is
        never reclaimed.
        """
        await store.create_execution(abandoned("expired", expires_at=NOW))
        await store.create_execution(
            abandoned("live", expires_at=NOW + timedelta(minutes=5))
        )

        listed = await store.list_executions(status=ExecutionStatus.RUNNING)
        stale = [
            record.run_id
            for record in listed
            if record.lease_expires_at is not None
            and as_utc(record.lease_expires_at) <= NOW
        ]

        assert stale == ["expired"]


class TestAQueuedRunIsCoveredToo:
    """A drive cancelled before its first write leaves a PENDING record.

    ``reclaim_orphans`` scans that status as well, so the same round trip has
    to hold for it — and the *absence* of a lease has to survive too, or a run
    merely waiting its turn would be reclaimed out from under the task about to
    run it.
    """

    async def test_an_abandoned_pending_run_is_listed_with_its_lease(
        self, store
    ) -> None:
        record = abandoned("run-1")
        record.status = ExecutionStatus.PENDING
        await store.create_execution(record)

        listed = await store.list_executions(status=ExecutionStatus.PENDING)

        assert [r.run_id for r in listed] == ["run-1"]
        assert listed[0].lease_expires_at is not None
        assert as_utc(listed[0].lease_expires_at) == NOW

    async def test_a_queued_run_comes_back_with_no_lease_at_all(self, store) -> None:
        await store.create_execution(
            ExecutionRecord(
                run_id="queued", workflow="held", status=ExecutionStatus.PENDING
            )
        )

        listed = await store.list_executions(status=ExecutionStatus.PENDING)

        assert [r.run_id for r in listed] == ["queued"]
        # An epoch or an empty string here would make every queued run an orphan.
        assert listed[0].lease_expires_at is None


class TestSettlingClearsIt:
    async def test_a_cleared_lease_round_trips_as_absent(self, store) -> None:
        """The other branch of `_settle_lease`, on a run that finished.

        It has to read back as None rather than as epoch or an empty string, or
        a completed run would look like an orphan to the scan above.
        """
        record = abandoned("run-1")
        await store.create_execution(record)

        record.status = ExecutionStatus.COMPLETED
        record.lease_owner = None
        record.lease_expires_at = None
        await store.update_execution(record)

        found = await store.get_execution("run-1")
        assert found is not None
        assert found.lease_owner is None
        assert found.lease_expires_at is None
