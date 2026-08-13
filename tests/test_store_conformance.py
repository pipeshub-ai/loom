"""One conformance suite, run against every in-memory-capable store.

``MemoryStore`` and ``SQLiteStore(":memory:")`` are both fully in-process, so
the same behavioural contract can be asserted against both with no
infrastructure. Divergence between stores is the bug class this catches — a
feature wired against dict semantics that quietly behaves differently once
rows go through SQL.

Mongo and Postgres implement the same protocol but need a server, so they are
covered by the protocol-shape test at the bottom rather than executed here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from workflow_builder import Context, ExecutionStatus, Runtime, step, workflow
from workflow_builder.core.models import Event, ExecutionRecord
from workflow_builder.runtime.journal import EntryKind, EntryStatus, JournalEntry
from workflow_builder.state.memory import MemoryStore
from workflow_builder.state.sqlite import SQLiteStore


@pytest.fixture(params=["memory", "sqlite"])
async def store(request):
    """Each test runs once per in-memory store implementation."""
    if request.param == "memory":
        yield MemoryStore()
        return

    made = SQLiteStore(":memory:")
    try:
        yield made
    finally:
        close = getattr(made, "close", None)
        if close is not None:
            await close()


def _record(run_id: str = "run-1", **overrides) -> ExecutionRecord:
    base = {
        "run_id": run_id,
        "workflow": "conformance",
        "status": ExecutionStatus.PENDING,
        "created_at": datetime.now(UTC),
    }
    return ExecutionRecord(**{**base, **overrides})


def _entry(path: str = "0000", name: str = "s1", **overrides) -> JournalEntry:
    base = {
        "path": path,
        "kind": EntryKind.STEP,
        "name": name,
        "status": EntryStatus.COMPLETED,
        "output": {"ok": True},
    }
    return JournalEntry(**{**base, **overrides})


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------


class TestExecutions:
    async def test_create_and_get(self, store) -> None:
        await store.create_execution(_record())
        found = await store.get_execution("run-1")

        assert found is not None
        assert found.workflow == "conformance"

    async def test_get_unknown_returns_none(self, store) -> None:
        assert await store.get_execution("nope") is None

    async def test_update_round_trips(self, store) -> None:
        await store.create_execution(_record())
        record = await store.get_execution("run-1")
        record.status = ExecutionStatus.COMPLETED
        record.output = {"value": 7}
        await store.update_execution(record)

        reloaded = await store.get_execution("run-1")
        assert reloaded.status is ExecutionStatus.COMPLETED
        assert reloaded.output == {"value": 7}

    async def test_code_hash_persists(self, store) -> None:
        """Run provenance must survive the round trip, or it is not provenance."""
        await store.create_execution(_record(code_hash="abc123"))
        assert (await store.get_execution("run-1")).code_hash == "abc123"

    async def test_lease_fields_persist(self, store) -> None:
        expiry = datetime.now(UTC) + timedelta(seconds=30)
        await store.create_execution(
            _record(lease_owner="node-a", lease_expires_at=expiry)
        )

        found = await store.get_execution("run-1")
        assert found.lease_owner == "node-a"
        assert found.lease_expires_at is not None

    async def test_list_filters_by_workflow_and_status(self, store) -> None:
        await store.create_execution(_record("a", workflow="alpha"))
        await store.create_execution(
            _record("b", workflow="beta", status=ExecutionStatus.COMPLETED)
        )

        assert {r.run_id for r in await store.list_executions(workflow="alpha")} == {"a"}
        assert {
            r.run_id
            for r in await store.list_executions(status=ExecutionStatus.COMPLETED)
        } == {"b"}

    async def test_list_respects_limit(self, store) -> None:
        for i in range(5):
            await store.create_execution(_record(f"run-{i}"))
        assert len(await store.list_executions(limit=3)) == 3

    async def test_idempotency_lookup(self, store) -> None:
        await store.create_execution(_record(idempotency_key="k1"))

        found = await store.find_by_idempotency_key("k1")
        assert found is not None and found.run_id == "run-1"
        assert await store.find_by_idempotency_key("absent") is None

    async def test_delete_removes_record_and_journal(self, store) -> None:
        await store.create_execution(_record(idempotency_key="k1"))
        await store.save_journal("run-1", [_entry()])

        await store.delete_execution("run-1")

        assert await store.get_execution("run-1") is None
        assert await store.load_journal("run-1") == []
        assert await store.find_by_idempotency_key("k1") is None

    async def test_delete_unknown_is_a_no_op(self, store) -> None:
        await store.delete_execution("never-existed")


# ---------------------------------------------------------------------------
# Journals
# ---------------------------------------------------------------------------


class TestJournals:
    async def test_save_and_load_preserves_order(self, store) -> None:
        await store.create_execution(_record())
        await store.save_journal(
            "run-1",
            [_entry("0002", "third"), _entry("0000", "first"), _entry("0001", "second")],
        )

        loaded = await store.load_journal("run-1")
        assert [e.name for e in loaded] == ["first", "second", "third"]

    async def test_save_is_an_upsert_by_path(self, store) -> None:
        await store.create_execution(_record())
        await store.save_journal("run-1", [_entry("0000", "s1")])
        await store.save_journal(
            "run-1", [_entry("0000", "s1", status=EntryStatus.FAILED)]
        )

        loaded = await store.load_journal("run-1")
        assert len(loaded) == 1
        assert loaded[0].status is EntryStatus.FAILED

    async def test_save_empty_is_a_no_op(self, store) -> None:
        await store.create_execution(_record())
        await store.save_journal("run-1", [])
        assert await store.load_journal("run-1") == []

    async def test_truncate_drops_from_the_path_onward(self, store) -> None:
        await store.create_execution(_record())
        await store.save_journal(
            "run-1", [_entry("0000", "a"), _entry("0001", "b"), _entry("0002", "c")]
        )

        await store.truncate_journal("run-1", "0001")

        assert [e.name for e in await store.load_journal("run-1")] == ["a"]

    async def test_truncate_from_empty_path_drops_everything(self, store) -> None:
        """Retention compaction relies on this to clear a journal wholesale."""
        await store.create_execution(_record())
        await store.save_journal("run-1", [_entry("0000"), _entry("0001")])

        await store.truncate_journal("run-1", "")

        assert await store.load_journal("run-1") == []

    async def test_journals_are_isolated_per_run(self, store) -> None:
        await store.create_execution(_record("a"))
        await store.create_execution(_record("b"))
        await store.save_journal("a", [_entry("0000", "only-a")])

        assert await store.load_journal("b") == []


# ---------------------------------------------------------------------------
# Events and timers
# ---------------------------------------------------------------------------


class TestEventsAndTimers:
    async def test_event_round_trip(self, store) -> None:
        await store.enqueue_event(Event(name="approve", payload={"ok": True}, run_id="r"))

        taken = await store.take_event("r", "approve")
        assert taken is not None and taken.payload == {"ok": True}

    async def test_taking_an_event_consumes_it(self, store) -> None:
        await store.enqueue_event(Event(name="approve", run_id="r"))
        await store.take_event("r", "approve")

        assert await store.take_event("r", "approve") is None

    async def test_events_are_delivered_in_order(self, store) -> None:
        for i in range(3):
            await store.enqueue_event(Event(name="tick", payload={"i": i}, run_id="r"))

        seen = [(await store.take_event("r", "tick")).payload["i"] for _ in range(3)]
        assert seen == [0, 1, 2]

    async def test_runs_awaiting_event(self, store) -> None:
        await store.create_execution(
            _record("waiting", status=ExecutionStatus.SUSPENDED, awaiting_event="go")
        )
        await store.create_execution(_record("busy", status=ExecutionStatus.RUNNING))

        assert await store.runs_awaiting_event("go") == ["waiting"]

    async def test_due_runs_returns_only_expired_timers(self, store) -> None:
        now = datetime.now(UTC)
        await store.create_execution(
            _record(
                "ready",
                status=ExecutionStatus.SUSPENDED,
                wake_at=now - timedelta(seconds=10),
            )
        )
        await store.create_execution(
            _record(
                "later",
                status=ExecutionStatus.SUSPENDED,
                wake_at=now + timedelta(hours=1),
            )
        )

        assert await store.due_runs(now) == ["ready"]

    async def test_due_runs_ignores_running_records(self, store) -> None:
        """A RUNNING record is not a timer, however old — that is orphan recovery."""
        now = datetime.now(UTC)
        await store.create_execution(
            _record(
                "active",
                status=ExecutionStatus.RUNNING,
                wake_at=now - timedelta(hours=1),
            )
        )

        assert await store.due_runs(now) == []


# ---------------------------------------------------------------------------
# Cache and locks
# ---------------------------------------------------------------------------


class TestCacheAndLocks:
    async def test_cache_round_trip(self, store) -> None:
        await store.set("k", {"v": 1}, 60)
        assert await store.get("k") == {"v": 1}

    async def test_cache_miss_returns_none(self, store) -> None:
        assert await store.get("absent") is None

    async def test_cache_delete(self, store) -> None:
        await store.set("k", 1, 60)
        await store.delete("k")
        assert await store.get("k") is None

    async def test_expired_cache_entry_is_a_miss(self, store) -> None:
        import asyncio

        await store.set("k", 1, 0.01)
        await asyncio.sleep(0.05)

        assert await store.get("k") is None

    @pytest.mark.parametrize("ttl", [0, -1])
    async def test_non_positive_ttl_means_never_expires(self, store, ttl) -> None:
        """Reading zero as "already expired" would make set() a silent no-op."""
        await store.set("forever", {"v": 1}, ttl)
        assert await store.get("forever") == {"v": 1}

    async def test_lock_is_exclusive(self, store) -> None:
        assert await store.acquire("lock", "alice", 30)
        assert not await store.acquire("lock", "bob", 30)

    async def test_lock_release_frees_it(self, store) -> None:
        await store.acquire("lock", "alice", 30)
        await store.release("lock", "alice")
        assert await store.acquire("lock", "bob", 30)

    async def test_only_the_owner_renews(self, store) -> None:
        await store.acquire("lock", "alice", 30)
        assert await store.renew("lock", "alice", 30)
        assert not await store.renew("lock", "bob", 30)


# ---------------------------------------------------------------------------
# End to end through the engine
# ---------------------------------------------------------------------------


@step
async def double(n: int) -> int:
    """Double a number."""
    return n * 2


@workflow(name="conformance_flow")
async def conformance_flow(ctx: Context, n: int) -> int:
    """Double twice, with a durable step each time."""
    once = await ctx.step(double, n)
    return await ctx.step(double, once)


@workflow(name="conformance_parked")
async def conformance_parked(ctx: Context, _input: str) -> str:
    """Park on an approval so suspension is exercised against the store."""
    return "yes" if await ctx.wait_for_approval("go") else "no"


class TestEngineAgainstEachStore:
    async def test_run_completes_and_journals(self, store) -> None:
        rt = Runtime(store=store)
        result = await rt.run(conformance_flow, 5)

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == 20
        assert [e.name for e in await rt.history(result.run_id)] == ["double", "double"]

    async def test_run_records_its_code_hash(self, store) -> None:
        rt = Runtime(store=store)
        result = await rt.run(conformance_flow, 1)

        record = await rt.get(result.run_id)
        assert record.code_hash == conformance_flow.code_hash
        assert record.code_hash

    async def test_replay_reuses_the_journal(self, store) -> None:
        calls: list[int] = []

        @step
        async def counted(n: int) -> int:
            """Record that it actually ran."""
            calls.append(n)
            return n + 1

        @workflow(name="conformance_counted")
        async def counted_flow(ctx: Context, n: int) -> int:
            return await ctx.step(counted, n)

        rt = Runtime(store=store)
        first = await rt.run(counted_flow, 1)
        assert calls == [1]

        replayed = await rt.replay(first.run_id)
        assert replayed.status is ExecutionStatus.COMPLETED
        # Served from the journal — the body did not run a second time.
        assert calls == [1]

    async def test_suspend_and_resume(self, store) -> None:
        rt = Runtime(store=store)
        parked = await rt.run(conformance_parked, "go")
        assert parked.status is ExecutionStatus.SUSPENDED

        await rt.approve(parked.run_id, "go")
        resumed = await rt.resume(parked.run_id)

        assert resumed.status is ExecutionStatus.COMPLETED
        assert resumed.output == "yes"

    async def test_idempotency_key_returns_the_same_run(self, store) -> None:
        rt = Runtime(store=store)
        first = await rt.run(conformance_flow, 3, idempotency_key="once")
        second = await rt.run(conformance_flow, 3, idempotency_key="once")

        assert first.run_id == second.run_id

    async def test_retry_resumes_from_the_failed_step(self, store) -> None:
        attempts: list[int] = []

        # retry=1 so the failure reaches the workflow instead of being absorbed
        # by the step's own retry budget, which defaults to 3 attempts.
        @step(retry=1)
        async def flaky(n: int) -> int:
            """Fail the first time it is called, succeed after."""
            attempts.append(n)
            if attempts.count(n) == 1:
                raise RuntimeError("transient")
            return n

        @step
        async def before(n: int) -> int:
            """Runs once and should not be repeated by the retry."""
            attempts.append(-1)
            return n

        @workflow(name="conformance_retry")
        async def retry_flow(ctx: Context, n: int) -> int:
            first = await ctx.step(before, n)
            return await ctx.step(flaky, first)

        rt = Runtime(store=store)
        failed = await rt.run(retry_flow, 4)
        assert failed.status is ExecutionStatus.FAILED

        recovered = await rt.retry(failed.run_id)

        assert recovered.status is ExecutionStatus.COMPLETED
        # `before` ran exactly once across both attempts.
        assert attempts.count(-1) == 1


# ---------------------------------------------------------------------------
# Protocol shape for stores that need a server
# ---------------------------------------------------------------------------


class TestProtocolCoverage:
    @pytest.mark.parametrize("module,name", [
        ("workflow_builder.state.mongo", "MongoStore"),
        ("workflow_builder.state.postgres", "PostgresStore"),
    ])
    def test_remote_stores_implement_the_full_surface(self, module, name) -> None:
        """These need a server to run, so at least assert nothing is missing."""
        import importlib

        cls = getattr(importlib.import_module(module), name)
        required = [
            "create_execution", "get_execution", "update_execution",
            "delete_execution", "list_executions", "find_by_idempotency_key",
            "save_journal", "load_journal", "truncate_journal",
            "enqueue_event", "take_event", "runs_awaiting_event", "due_runs",
        ]
        missing = [method for method in required if not hasattr(cls, method)]
        assert missing == [], f"{name} is missing {missing}"
