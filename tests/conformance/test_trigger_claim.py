"""``claim_due_triggers`` against every backend.

`due_triggers` reads; this one *takes*. Every store implements it with its own
primitive — a mutex, `BEGIN IMMEDIATE`, `UPDATE … RETURNING … SKIP LOCKED`,
`find_one_and_update` — and this suite is what says the four mean the same
thing. That is the bug class the conformance harness exists for: a claim that is
genuinely exclusive on Postgres and merely advisory on Mongo would leave one
deployment double-working and no test anywhere would notice.

Note what is *not* asserted here: that claiming prevents double *runs*. It does
not, and does not need to — the occurrence key does that, and it keeps doing it
when a lease expires at the wrong moment. Claiming stops duplicated work and
keeps two dispatchers from both advancing one record.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from conformance.backends import ALL_BACKENDS, open_store
from loom.core.models import TriggerRecord

NINE_AM = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


async def a_due_trigger(store, trigger_id: str = "trg_1", *, when=NINE_AM):
    await store.save_trigger(
        TriggerRecord(
            trigger_id=trigger_id,
            workflow="hourly_report",
            spec={"cron": "0 * * * *"},
            next_fire_at=when,
        )
    )


class TestClaimingIsExclusive:
    async def test_a_due_trigger_can_be_claimed(self, store) -> None:
        await a_due_trigger(store)

        won = await store.claim_due_triggers(NINE_AM, owner="node-a")

        assert [t.trigger_id for t in won] == ["trg_1"]
        assert won[0].claimed_by == "node-a"

    async def test_the_second_caller_gets_nothing(self, store) -> None:
        """The property. Sequential here; concurrent below."""
        await a_due_trigger(store)

        first = await store.claim_due_triggers(NINE_AM, owner="node-a")
        second = await store.claim_due_triggers(NINE_AM, owner="node-b")

        assert len(first) == 1
        assert second == []

    async def test_concurrent_claims_are_disjoint(self, store) -> None:
        """Four dispatchers, four triggers, no coordination.

        Every trigger must be claimed exactly once across the whole fleet — not
        merely "each caller got some". Overlap here is two processes doing the
        same work and both advancing one record.
        """
        for n in range(4):
            await a_due_trigger(store, f"trg_{n}")

        batches = await asyncio.gather(
            *(
                store.claim_due_triggers(NINE_AM, owner=f"node-{n}")
                for n in range(4)
            )
        )

        claimed = [t.trigger_id for batch in batches for t in batch]
        assert sorted(claimed) == ["trg_0", "trg_1", "trg_2", "trg_3"]
        assert len(claimed) == len(set(claimed)), f"overlapping claims: {claimed}"

    async def test_a_trigger_that_is_not_due_is_not_claimed(self, store) -> None:
        await a_due_trigger(store, when=NINE_AM + timedelta(hours=1))

        assert await store.claim_due_triggers(NINE_AM, owner="node-a") == []

    async def test_a_disabled_trigger_is_not_claimed(self, store) -> None:
        await store.save_trigger(
            TriggerRecord(
                trigger_id="trg_off",
                workflow="hourly_report",
                spec={"cron": "0 * * * *"},
                next_fire_at=NINE_AM,
                enabled=False,
            )
        )

        assert await store.claim_due_triggers(NINE_AM, owner="node-a") == []

    async def test_the_limit_is_honoured(self, store) -> None:
        for n in range(5):
            await a_due_trigger(store, f"trg_{n}")

        won = await store.claim_due_triggers(NINE_AM, owner="node-a", limit=2)

        assert len(won) == 2


class TestTheClaimIsALease:
    async def test_an_expired_claim_can_be_taken_by_someone_else(
        self, store
    ) -> None:
        """The property that keeps a crash cheap.

        A dispatcher that dies mid-tick holds a claim it will never release. If
        that were a lock, the trigger would never fire again — one crash costing
        every future occurrence instead of one late one.
        """
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a", lease_seconds=30)

        later = NINE_AM + timedelta(seconds=31)
        won = await store.claim_due_triggers(later, owner="node-b")

        assert [t.trigger_id for t in won] == ["trg_1"]
        assert won[0].claimed_by == "node-b"

    async def test_a_live_claim_is_not_stolen(self, store) -> None:
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a", lease_seconds=300)

        still_held = NINE_AM + timedelta(seconds=299)
        assert await store.claim_due_triggers(still_held, owner="node-b") == []


class TestClaimingIsNotFiring:
    async def test_claiming_does_not_advance_the_schedule(self, store) -> None:
        """`update_after_fire` stays the only thing that moves the schedule.

        If claiming advanced it, a dispatcher that claimed and then failed to
        submit would have skipped the occurrence — losing a run to an error
        that never reached the workflow.
        """
        await a_due_trigger(store)

        await store.claim_due_triggers(NINE_AM, owner="node-a")

        stored = await store.get_trigger("trg_1")
        assert stored.next_fire_at.replace(tzinfo=UTC) == NINE_AM
        assert stored.last_fire_at is None

    async def test_the_claim_survives_a_read(self, store) -> None:
        """Claim state is part of the record, so every store persists it."""
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a", lease_seconds=60)

        stored = await store.get_trigger("trg_1")

        assert stored.claimed_by == "node-a"
        assert stored.claimed_until is not None

    async def test_firing_after_a_claim_still_advances(self, store) -> None:
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a")

        await store.update_after_fire(
            "trg_1", NINE_AM, NINE_AM + timedelta(hours=1)
        )

        stored = await store.get_trigger("trg_1")
        assert stored.next_fire_at.replace(tzinfo=UTC) == NINE_AM + timedelta(
            hours=1
        )


class TestAdvancingReleasesTheClaim:
    """Claim, fire, advance-and-release is the whole cycle.

    Leaving the release to lease expiry makes the lease duration a silent lower
    bound on the schedule: a per-minute cron under a 60-second lease fires once
    and then sits idle until the claim lapses. Nothing errors; the schedule is
    simply slower than it says it is.
    """

    async def test_the_claim_is_cleared_when_the_trigger_advances(
        self, store
    ) -> None:
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a", lease_seconds=3600)

        await store.update_after_fire(
            "trg_1", NINE_AM, NINE_AM + timedelta(minutes=1)
        )

        stored = await store.get_trigger("trg_1")
        assert stored.claimed_by == ""
        assert stored.claimed_until is None

    async def test_the_next_occurrence_is_claimable_immediately(
        self, store
    ) -> None:
        """The consequence, in the unit that matters."""
        await a_due_trigger(store)
        await store.claim_due_triggers(NINE_AM, owner="node-a", lease_seconds=3600)
        await store.update_after_fire(
            "trg_1", NINE_AM, NINE_AM + timedelta(minutes=1)
        )

        next_minute = NINE_AM + timedelta(minutes=1)
        won = await store.claim_due_triggers(next_minute, owner="node-a")

        assert [t.trigger_id for t in won] == ["trg_1"], (
            "the next occurrence was blocked by the previous claim"
        )
