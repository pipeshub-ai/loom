"""A store built by URL is indexed by the time it is written to.

`loom.stores.from_url` is synchronous and `ensure_indexes()` is not, so for as
long as indexing was the caller's job, `LOOM_STORE=mongodb://…` plus
`Runtime.from_env()` — the documented deployment path — produced a store whose
`executions` collection carried only `_id_`.

The missing index that matters is the **unique** one on `idempotency_key`. Its
absence removes the exactly-once guarantee behind cron dedupe, event dedupe and
queue ingress, and removes it *silently*: two runs are created for one
occurrence and both writes report success. Postgres has the same construction
gap and fails loudly instead, its pool being `None`; loud and broken is
recoverable, silent and degraded is not, which is why Mongo builds on first use
rather than matching Postgres.

These tests deliberately go through `from_url` and never call `ensure_indexes`,
because calling it is precisely the step the deployment path omits.
"""

from __future__ import annotations

import uuid

import pytest

from conformance.backends import BY_NAME
from loom.core.exceptions import ConfigurationError
from loom.core.models import ExecutionRecord
from loom.stores import from_url

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mongo_url():
    backend = BY_NAME["mongo"]
    reason = backend.why_not()
    if reason:
        pytest.skip(f"mongo: {reason}")
    name = f"loom_lazy_{uuid.uuid4().hex[:10]}"
    yield f"{backend.url}/{name}", name
    from motor.motor_asyncio import AsyncIOMotorClient

    await AsyncIOMotorClient(backend.url).drop_database(name)


class TestFromUrlIsIndexed:
    async def test_a_write_through_from_url_builds_the_indexes(self, mongo_url) -> None:
        url, _ = mongo_url
        store = from_url(url)

        await store.create_execution(
            ExecutionRecord(run_id="r1", workflow="w", idempotency_key="k1")
        )

        names = await store._db.executions.index_information()
        assert "idempotency_key_1" in names
        journal = await store._db.journal.index_information()
        assert "run_id_1_path_1" in journal

    async def test_one_key_still_means_one_run(self, mongo_url) -> None:
        """The property the index exists for, asserted through the URL path.

        Without the index this second create succeeded and left two runs for one
        occurrence — a cron that fired twice, reported as success by every layer
        above.
        """
        from pymongo.errors import DuplicateKeyError

        url, _ = mongo_url
        store = from_url(url)
        key = "trg_abc@2026-03-02T10:00:00+00:00"
        await store.create_execution(
            ExecutionRecord(run_id="run-1", workflow="w", idempotency_key=key)
        )

        with pytest.raises(DuplicateKeyError):
            await store.create_execution(
                ExecutionRecord(run_id="run-2", workflow="w", idempotency_key=key)
            )

        count = await store._db.executions.count_documents({"idempotency_key": key})
        assert count == 1

    async def test_runs_without_a_key_do_not_collide(self, mongo_url) -> None:
        """The partial filter, re-pinned on the lazy path.

        `sparse` would have indexed a document carrying `idempotency_key: null`,
        so a second keyless run collided with the first and the store could hold
        exactly one. Building the index lazily must not lose that distinction.
        """
        url, _ = mongo_url
        store = from_url(url)

        await store.create_execution(ExecutionRecord(run_id="a", workflow="w"))
        await store.create_execution(ExecutionRecord(run_id="b", workflow="w"))

        assert await store.get_execution("a") is not None
        assert await store.get_execution("b") is not None

    async def test_indexes_are_built_once_not_per_call(self, mongo_url) -> None:
        url, _ = mongo_url
        store = from_url(url)
        await store.create_execution(ExecutionRecord(run_id="r1", workflow="w"))
        assert store._indexed

        calls = 0
        original = store._build_indexes

        async def counted() -> None:
            nonlocal calls
            calls += 1
            await original()

        store._build_indexes = counted
        for i in range(5):
            await store.create_execution(ExecutionRecord(run_id=f"n{i}", workflow="w"))

        assert calls == 0


class TestPreExistingDuplicatesAreExplained:
    async def test_duplicates_from_an_unindexed_period_raise_a_named_error(
        self, mongo_url
    ) -> None:
        """Upgrading onto a database that ran without the index.

        Mongo refuses to build a unique index over data that already violates
        it. Raw, that arrives as a `DuplicateKeyError` from whichever call was
        first, which reads as "this write is a duplicate" rather than "dedupe
        was never enforced here". Skipping the index instead would restore
        exactly the silence being fixed, so it raises — and names the key.
        """
        url, name = mongo_url
        from motor.motor_asyncio import AsyncIOMotorClient

        # Seed the state an unindexed deployment would have reached.
        raw = AsyncIOMotorClient(url.rsplit("/", 1)[0])[name]
        await raw.executions.insert_many(
            [
                {"_id": "run-1", "workflow": "w", "idempotency_key": "dup"},
                {"_id": "run-2", "workflow": "w", "idempotency_key": "dup"},
            ]
        )

        store = from_url(url)
        with pytest.raises(ConfigurationError) as caught:
            await store.create_execution(ExecutionRecord(run_id="r3", workflow="w"))

        message = str(caught.value)
        assert "idempotency_key" in message
        assert name in message
        # Actionable, not merely descriptive: the operator needs to find them.
        assert "aggregate" in message
