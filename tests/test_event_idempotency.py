"""At-least-once senders, and the run that must not advance twice.

``tests/conformance/test_event_dedupe.py`` proves the store-level claim is
atomic on every backend. This proves the Runtime uses it, that the default is
unchanged, and that the answer a consumer needs to ack correctly comes back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore

CALLS: list[str] = []


@step
async def record(marker: str) -> str:
    CALLS.append(marker)
    return marker


@workflow(name="waits")
async def waits(ctx: Context, _: Any = None) -> str:
    payload = await ctx.wait_for_event("go")
    return await ctx.step(record, str(payload))


@pytest.fixture(autouse=True)
def _clear() -> None:
    CALLS.clear()


@pytest.fixture
def runtime() -> Runtime:
    made = Runtime(store=MemoryStore())
    made.register(waits)
    return made


class TestRedelivery:
    async def test_a_repeated_key_does_not_advance_the_run_twice(
        self, runtime: Runtime
    ) -> None:
        """The defect this exists for.

        Kafka and Redis Streams are at-least-once. Without a key, the second
        delivery of one message re-enters the body and the step runs again.
        """
        parked = await runtime.run(waits)
        assert parked.status.value == "suspended"

        first = await runtime.send_event(parked.run_id, "go", "a", dedupe_key="m-1")
        again = await runtime.send_event(parked.run_id, "go", "a", dedupe_key="m-1")

        assert first.delivered is True
        assert first.run_ids == [parked.run_id]
        assert again.delivered is False
        assert again.reason == "duplicate"
        assert again.run_ids == [], "a duplicate must not report resuming anything"

    async def test_the_consumer_can_tell_the_two_apart(self, runtime: Runtime) -> None:
        """A duplicate that raised would be retried forever; one that looked
        fresh would hide the redelivery. Neither is ackable."""
        parked = await runtime.run(waits)
        outcomes = [
            (await runtime.send_event(parked.run_id, "go", 1, dedupe_key="m-1")).delivered
            for _ in range(4)
        ]
        assert outcomes == [True, False, False, False]

    async def test_concurrent_redelivery_delivers_once(
        self, runtime: Runtime
    ) -> None:
        """Two consumers on one partition is the normal case, not the rare one."""
        parked = await runtime.run(waits)
        results = await asyncio.gather(
            *(
                runtime.send_event(parked.run_id, "go", 1, dedupe_key="m-1")
                for _ in range(10)
            )
        )
        assert sum(r.delivered for r in results) == 1

    async def test_different_keys_both_deliver(self, runtime: Runtime) -> None:
        parked = await runtime.run(waits)
        a = await runtime.send_event(parked.run_id, "go", 1, dedupe_key="m-1")
        b = await runtime.send_event(parked.run_id, "other", 2, dedupe_key="m-2")
        assert a.delivered and b.delivered


class TestTheDefaultIsUnchanged:
    async def test_without_a_key_nothing_is_deduplicated(
        self, runtime: Runtime
    ) -> None:
        """Existing callers must behave exactly as before — including the ones
        that deliberately send the same event twice."""
        parked = await runtime.run(waits)
        first = await runtime.send_event(parked.run_id, "go", 1)
        second = await runtime.send_event(parked.run_id, "go", 1)
        assert first.delivered and second.delivered

    async def test_approve_still_works(self, runtime: Runtime) -> None:
        """``approve`` routes through ``send_event``; a changed return type
        must not break it."""

        @workflow(name="needs_approval")
        async def needs_approval(ctx: Context, _: Any = None) -> str:
            return "yes" if await ctx.wait_for_approval("refund") else "no"

        runtime.register(needs_approval)
        parked = await runtime.run(needs_approval)
        await runtime.approve(parked.run_id, "refund", approved=True)
        assert (await runtime.resume(parked.run_id)).output == "yes"

    async def test_an_event_with_no_waiting_run_is_still_buffered(
        self, runtime: Runtime
    ) -> None:
        """Empty ``run_ids`` is normal, not a failure: an event can arrive
        before anything waits for it."""
        delivery = await runtime.send_event(None, "early", 1, dedupe_key="m-1")
        assert delivery.delivered is True
        assert delivery.run_ids == []


class TestTheSurfaces:
    async def test_the_facade_returns_the_delivery(self) -> None:
        from loom.facade import LocalFacade

        made = Runtime(store=MemoryStore())
        made.register(waits)
        facade = LocalFacade(made)
        parked = await made.run(waits)

        first = await facade.send_event(parked.run_id, "go", 1, dedupe_key="m-1")
        again = await facade.send_event(parked.run_id, "go", 1, dedupe_key="m-1")
        assert first["delivered"] is True
        assert again["delivered"] is False and again["reason"] == "duplicate"

    async def test_a_duplicate_does_not_resume_through_the_facade(self) -> None:
        """The facade resumes after delivering. A duplicate must skip that too,
        or the dedupe protects the queue and not the run."""
        from loom.facade import LocalFacade

        made = Runtime(store=MemoryStore())
        made.register(waits)
        facade = LocalFacade(made)
        parked = await made.run(waits)

        await facade.send_event(parked.run_id, "go", "x", dedupe_key="m-1")
        before = list(CALLS)
        await facade.send_event(parked.run_id, "go", "x", dedupe_key="m-1")
        assert before == CALLS, "the duplicate re-entered the workflow body"

    def test_the_remote_facade_refuses_rather_than_dropping_the_key(self) -> None:
        """Silently ignoring it would resume the run twice while the caller
        saw success — worse than not offering the parameter."""
        import inspect

        from loom.facade import RemoteFacade

        source = inspect.getsource(RemoteFacade.send_event)
        assert "dedupe_key" in source
        assert "raise" in source, "the remote adapter accepts the key and drops it"
