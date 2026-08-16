"""One idempotency key means one run — on every backend.

`submit()` checks `find_by_idempotency_key` and then creates. Two callers that
both pass the check race to create, so the *store* is what has to settle it:
the check narrows the window and the constraint closes it.

This is load-bearing rather than defensive. Scheduling submits every occurrence
under a key derived from the schedule (`trigger_id@scheduled_for`), so "a cron
fires once" is exactly as true as "one key means one run" is on the store the
host happens to run. A guarantee that holds on Postgres and not on the default
store is not a guarantee, it is a deployment note nobody reads.
"""

from __future__ import annotations

import asyncio

import pytest

from conformance.backends import ALL_BACKENDS, open_store
from loom.core.models import ExecutionRecord


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


def a_record(run_id: str, key: str = "trg_abc@2026-03-02T10:00:00+00:00"):
    """A run submitted for one scheduled occurrence."""
    return ExecutionRecord(
        run_id=run_id, workflow="hourly_report", idempotency_key=key
    )


class TestOneKeyIsOneRun:
    async def test_the_key_resolves_to_the_first_run(self, store) -> None:
        await store.create_execution(a_record("run-1"))

        found = await store.find_by_idempotency_key(
            "trg_abc@2026-03-02T10:00:00+00:00"
        )

        assert found is not None and found.run_id == "run-1"

    async def test_a_second_create_under_one_key_does_not_add_a_run(
        self, store
    ) -> None:
        """The property the whole file is about.

        Whether the store raises or absorbs the duplicate is its own business —
        the engine handles both. What no store may do is quietly end up holding
        two runs for one occurrence, because then the cron fired twice and
        every layer above reports success.
        """
        await store.create_execution(a_record("run-1"))

        with contextlib_suppress():
            await store.create_execution(a_record("run-2"))

        runs = await store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, (
            f"one occurrence left {len(runs)} runs in the store"
        )

    async def test_concurrent_creates_leave_one_run(self, store) -> None:
        """Two dispatchers, no sequencing — the shape production actually has."""
        await asyncio.gather(
            *(
                _create_ignoring_conflict(store, a_record(f"run-{n}"))
                for n in range(4)
            )
        )

        runs = await store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, (
            f"four concurrent submissions of one occurrence left {len(runs)} runs"
        )

    async def test_the_survivor_is_the_one_the_key_resolves_to(
        self, store
    ) -> None:
        """A caller that lost the race must be able to find the winner.

        Otherwise the loser has no run id to return, and a scheduled fire that
        was correctly deduplicated still surfaces as a failure.
        """
        await store.create_execution(a_record("run-1"))
        with contextlib_suppress():
            await store.create_execution(a_record("run-2"))

        found = await store.find_by_idempotency_key(
            "trg_abc@2026-03-02T10:00:00+00:00"
        )
        runs = await store.list_executions(workflow="hourly_report")

        assert found is not None
        assert found.run_id == runs[0].run_id, (
            "the key resolves to a run that is not the one that survived"
        )

    async def test_records_without_a_key_are_unaffected(self, store) -> None:
        """Most runs carry no key at all, and must not collide with each other.

        Mongo needed a partial index for exactly this: a plain unique index
        treats many missing values as many duplicates, so the store held one
        keyless run and rejected the rest.
        """
        await store.create_execution(
            ExecutionRecord(run_id="a", workflow="ad_hoc")
        )
        await store.create_execution(
            ExecutionRecord(run_id="b", workflow="ad_hoc")
        )

        assert len(await store.list_executions(workflow="ad_hoc")) == 2


def contextlib_suppress():
    """Suppress whatever a backend raises for a duplicate key.

    Named rather than inlined because the suite deliberately does *not* assert
    which exception: an integrity error and a silent absorb are both acceptable
    store behaviours. What is asserted is the state afterwards.
    """
    import contextlib

    return contextlib.suppress(Exception)


async def _create_ignoring_conflict(store, record) -> None:
    with contextlib_suppress():
        await store.create_execution(record)
