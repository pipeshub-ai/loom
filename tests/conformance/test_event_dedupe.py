"""``claim_event_delivery`` against every backend.

At-least-once is what every event bus worth using guarantees, so a redelivered
message must not resume a run a second time. ``submit()`` has had an idempotency
key since the beginning; events did not, which left the trigger path protected
and the event path open.

The claim lives in the store rather than the engine because it has to be
**atomic**: a read-then-write lets two consumers both observe a key as
unclaimed, which is precisely the race being prevented. Each backend uses its
own primitive — a mutex, ``INSERT OR IGNORE``, ``ON CONFLICT DO NOTHING``, a
unique ``_id`` — and this suite is what says all four actually mean the same
thing.
"""

from __future__ import annotations

import asyncio

import pytest

from conformance.backends import ALL_BACKENDS, open_store


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


class TestClaiming:
    async def test_the_first_claim_wins_and_the_second_loses(self, store) -> None:
        assert await store.claim_event_delivery("msg-1") is True
        assert await store.claim_event_delivery("msg-1") is False

    async def test_different_keys_do_not_interfere(self, store) -> None:
        assert await store.claim_event_delivery("msg-1") is True
        assert await store.claim_event_delivery("msg-2") is True

    async def test_repeated_claims_stay_false(self, store) -> None:
        """A consumer retrying forever must keep getting the same answer."""
        await store.claim_event_delivery("msg-1")
        assert [await store.claim_event_delivery("msg-1") for _ in range(5)] == [
            False
        ] * 5

    async def test_exactly_one_concurrent_claimer_wins(self, store) -> None:
        """The property the whole method exists for.

        Two consumers on one partition, or one consumer whose ack was lost, are
        the normal case. A read-then-write would let several through here.
        """
        outcomes = await asyncio.gather(
            *(store.claim_event_delivery("contended") for _ in range(16))
        )
        assert sum(outcomes) == 1, f"{sum(outcomes)} claimers won, expected 1"

    async def test_an_expired_claim_can_be_reclaimed(self, store) -> None:
        """The memory is bounded, so it must be reclaimable once it lapses —
        otherwise the table grows forever or the semantics lie."""
        assert await store.claim_event_delivery("short", ttl_seconds=0.05) is True
        await asyncio.sleep(0.12)
        assert await store.claim_event_delivery("short", ttl_seconds=60) is True

    async def test_an_unexpired_claim_is_not_reclaimed(self, store) -> None:
        assert await store.claim_event_delivery("long", ttl_seconds=300) is True
        await asyncio.sleep(0.05)
        assert await store.claim_event_delivery("long", ttl_seconds=300) is False

    async def test_claiming_does_not_disturb_events(self, store) -> None:
        """Dedupe and delivery are separate concerns sharing a store."""
        from loom.core.models import Event

        await store.enqueue_event(Event(name="ping", run_id="run-1", payload=1))
        await store.claim_event_delivery("unrelated")
        taken = await store.take_event("run-1", "ping")
        assert taken is not None and taken.payload == 1
