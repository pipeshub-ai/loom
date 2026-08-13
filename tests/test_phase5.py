"""Tests for Phase 5 — Production Hardening.

Covers: blob service, flow control / admission, saga compensation,
continue_as_new, version gates, structural replay, RBAC, retention,
leader election, OTel tracer.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workflow_builder import ExecutionStatus, Runtime, step, workflow
from workflow_builder.core.exceptions import ContinueAsNew, ControlSignal
from workflow_builder.core.serde import decode
from workflow_builder.runtime.flowcontrol import (
    AdmissionController,
    AdmissionDecision,
    BatchPolicy,
    ConcurrencyPolicy,
    FlowControlPolicy,
    RateLimitPolicy,
    SingletonPolicy,
)
from workflow_builder.runtime.leader import InMemoryLockProvider, LeaderElector
from workflow_builder.runtime.structural_replay import (
    ReplayStatus,
    StepIdentity,
    plan_structural_replay,
)
from workflow_builder.security.rbac import (
    AuthorizationError,
    Permission,
    Role,
    authorize,
    require,
)
from workflow_builder.state.memory import MemoryStore
from workflow_builder.storage.blob import (
    BlobNotFoundError,
    BlobService,
    LocalBlobBackend,
)
from workflow_builder.storage.retention import (
    CompactionResult,
    RetentionManager,
    RetentionPolicy,
)

# ---------------------------------------------------------------------------
# Blob Service
# ---------------------------------------------------------------------------


class TestLocalBlobBackend:
    @pytest.fixture
    def backend(self, tmp_path: Path) -> LocalBlobBackend:
        return LocalBlobBackend(tmp_path / "blobs")

    async def test_put_and_get(self, backend: LocalBlobBackend) -> None:
        data = b"hello world"
        await backend.put("abc123", data, "text/plain")
        result = await backend.get("abc123")
        assert result == data

    async def test_get_missing_raises(self, backend: LocalBlobBackend) -> None:
        with pytest.raises(BlobNotFoundError):
            await backend.get("nonexistent")

    async def test_exists(self, backend: LocalBlobBackend) -> None:
        assert not await backend.exists("abc123")
        await backend.put("abc123", b"data", "application/json")
        assert await backend.exists("abc123")

    async def test_delete(self, backend: LocalBlobBackend) -> None:
        await backend.put("abc123", b"data", "application/json")
        assert await backend.exists("abc123")
        await backend.delete("abc123")
        assert not await backend.exists("abc123")

    async def test_delete_missing_noop(self, backend: LocalBlobBackend) -> None:
        await backend.delete("nonexistent")  # should not raise

    async def test_fanout_subdirs(self, backend: LocalBlobBackend) -> None:
        await backend.put("abcdef", b"data", "text/plain")
        expected = backend._base_dir / "ab" / "abcdef"
        assert expected.exists()


class TestBlobService:
    @pytest.fixture
    def service(self, tmp_path: Path) -> BlobService:
        return BlobService(LocalBlobBackend(tmp_path / "blobs"))

    def test_should_offload(self, service: BlobService) -> None:
        small = b"x" * (256 * 1024)
        big = b"x" * (256 * 1024 + 1)
        assert not service.should_offload(small)
        assert service.should_offload(big)

    async def test_store_and_load(self, service: BlobService) -> None:
        data = b"some payload data"
        ref = await service.store(data)
        expected_hash = hashlib.sha256(data).hexdigest()
        assert ref == f"blob:{expected_hash}"

        loaded = await service.load(ref)
        assert loaded == data

    async def test_store_deduplicates(self, service: BlobService) -> None:
        data = b"duplicate data"
        ref1 = await service.store(data)
        ref2 = await service.store(data)
        assert ref1 == ref2

    def test_is_blob_ref(self) -> None:
        assert BlobService.is_blob_ref("blob:abc123")
        assert not BlobService.is_blob_ref("inline:data")
        assert not BlobService.is_blob_ref("abc123")

    async def test_delete(self, service: BlobService) -> None:
        data = b"to be deleted"
        ref = await service.store(data)
        await service.delete(ref)
        with pytest.raises(BlobNotFoundError):
            await service.load(ref)


# ---------------------------------------------------------------------------
# Flow Control
# ---------------------------------------------------------------------------


class TestAdmissionController:
    async def test_empty_policy_admits(self) -> None:
        ctrl = AdmissionController()
        result = await ctrl.evaluate("flow1", FlowControlPolicy())
        assert result.decision == AdmissionDecision.ADMIT

    async def test_concurrency_limit(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=2))

        ctrl.record_start("flow1")
        ctrl.record_start("flow1")
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.DELAY

    async def test_concurrency_allows_under_limit(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=2))

        ctrl.record_start("flow1")
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.ADMIT

    async def test_concurrency_after_end(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=1))

        ctrl.record_start("flow1")
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.DELAY

        ctrl.record_end("flow1")
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.ADMIT

    async def test_singleton_skip(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(
            singleton=SingletonPolicy(mode="skip")
        )

        ctrl.record_start("flow1")
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.SKIP

    async def test_singleton_allows_when_idle(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(
            singleton=SingletonPolicy(mode="skip")
        )
        result = await ctrl.evaluate("flow1", policy)
        assert result.decision == AdmissionDecision.ADMIT

    async def test_rate_limit(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(
            rate_limit=RateLimitPolicy(requests=2, period_seconds=60.0)
        )

        r1 = await ctrl.evaluate("flow1", policy)
        assert r1.decision == AdmissionDecision.ADMIT
        r2 = await ctrl.evaluate("flow1", policy)
        assert r2.decision == AdmissionDecision.ADMIT
        r3 = await ctrl.evaluate("flow1", policy)
        assert r3.decision == AdmissionDecision.DELAY

    async def test_in_flight_count(self) -> None:
        ctrl = AdmissionController()
        assert ctrl.in_flight_count("flow1") == 0
        ctrl.record_start("flow1")
        assert ctrl.in_flight_count("flow1") == 1
        ctrl.record_start("flow1")
        assert ctrl.in_flight_count("flow1") == 2
        ctrl.record_end("flow1")
        assert ctrl.in_flight_count("flow1") == 1

    async def test_partitioned_concurrency(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(
            concurrency=ConcurrencyPolicy(limit=1, key="tenant")
        )
        ctrl.record_start("flow1", partition_key="tenant")

        result = await ctrl.evaluate("flow1", policy, partition_key="tenant")
        assert result.decision == AdmissionDecision.DELAY

    async def test_batch_accumulation(self) -> None:
        ctrl = AdmissionController()
        policy = FlowControlPolicy(
            batch=BatchPolicy(max_size=3, window_seconds=60.0)
        )

        r1 = await ctrl.evaluate("flow1", policy)
        assert r1.decision == AdmissionDecision.BATCH
        r2 = await ctrl.evaluate("flow1", policy)
        assert r2.decision == AdmissionDecision.BATCH
        r3 = await ctrl.evaluate("flow1", policy)
        assert r3.decision == AdmissionDecision.ADMIT


# ---------------------------------------------------------------------------
# Structural Replay
# ---------------------------------------------------------------------------


class TestStructuralReplay:
    def _id(
        self, step_id: str, klass: str = "effect",
        contract: str = "c1", closure: str = "cl1",
    ) -> StepIdentity:
        return StepIdentity(
            step_id=step_id, step_class=klass,
            contract_hash=contract, closure_hash=closure,
        )

    def test_unchanged_step_is_green(self) -> None:
        old = {"s1": self._id("s1")}
        new = {"s1": self._id("s1")}
        plan = plan_structural_replay(old, new)
        assert len(plan.green) == 1
        assert plan.steps[0].status == ReplayStatus.REUSE

    def test_removed_step_is_orphan(self) -> None:
        old = {"s1": self._id("s1")}
        plan = plan_structural_replay(old, {})
        assert plan.steps[0].status == ReplayStatus.ORPHAN

    def test_new_step(self) -> None:
        new = {"s1": self._id("s1")}
        plan = plan_structural_replay({}, new)
        assert plan.steps[0].status == ReplayStatus.NEW

    def test_pure_body_change_is_recompute(self) -> None:
        old = {"s1": self._id("s1", klass="pure", closure="cl1")}
        new = {"s1": self._id("s1", klass="pure", closure="cl2")}
        plan = plan_structural_replay(old, new)
        assert plan.steps[0].status == ReplayStatus.RECOMPUTE

    def test_effect_body_change_is_ask(self) -> None:
        old = {"s1": self._id("s1", klass="effect", closure="cl1")}
        new = {"s1": self._id("s1", klass="effect", closure="cl2")}
        plan = plan_structural_replay(old, new)
        assert plan.steps[0].status == ReplayStatus.ASK

    def test_contract_change_is_invalidate(self) -> None:
        old = {"s1": self._id("s1", contract="c1")}
        new = {"s1": self._id("s1", contract="c2")}
        plan = plan_structural_replay(old, new)
        assert plan.steps[0].status == ReplayStatus.INVALIDATE

    def test_safe_to_auto_replay_true(self) -> None:
        old = {"s1": self._id("s1")}
        new = {"s1": self._id("s1")}
        plan = plan_structural_replay(old, new)
        assert plan.safe_to_auto_replay

    def test_safe_to_auto_replay_false_with_red(self) -> None:
        old = {"s1": self._id("s1", contract="c1")}
        new = {"s1": self._id("s1", contract="c2")}
        plan = plan_structural_replay(old, new)
        assert not plan.safe_to_auto_replay

    def test_safe_to_auto_replay_false_with_ask(self) -> None:
        old = {"s1": self._id("s1", klass="effect", closure="cl1")}
        new = {"s1": self._id("s1", klass="effect", closure="cl2")}
        plan = plan_structural_replay(old, new)
        assert not plan.safe_to_auto_replay

    def test_safe_to_auto_replay_true_with_recompute(self) -> None:
        old = {"s1": self._id("s1", klass="pure", closure="cl1")}
        new = {"s1": self._id("s1", klass="pure", closure="cl2")}
        plan = plan_structural_replay(old, new)
        assert plan.safe_to_auto_replay

    def test_summary(self) -> None:
        old = {
            "s1": self._id("s1"),
            "s2": self._id("s2", klass="pure", closure="cl1"),
            "s3": self._id("s3", contract="c1"),
        }
        new = {
            "s1": self._id("s1"),
            "s2": self._id("s2", klass="pure", closure="cl2"),
            "s3": self._id("s3", contract="c2"),
        }
        plan = plan_structural_replay(old, new)
        assert plan.summary == "1 green, 1 amber, 1 red"

    def test_mixed_scenario(self) -> None:
        old = {
            "unchanged": self._id("unchanged"),
            "removed": self._id("removed"),
            "changed_pure": self._id("changed_pure", klass="pure", closure="old"),
            "changed_effect": self._id("changed_effect", klass="effect", closure="old"),
            "broken_contract": self._id("broken_contract", contract="old"),
        }
        new = {
            "unchanged": self._id("unchanged"),
            "changed_pure": self._id("changed_pure", klass="pure", closure="new"),
            "changed_effect": self._id("changed_effect", klass="effect", closure="new"),
            "broken_contract": self._id("broken_contract", contract="new"),
            "brand_new": self._id("brand_new"),
        }
        plan = plan_structural_replay(old, new)
        statuses = {s.step_id: s.status for s in plan.steps}
        assert statuses["unchanged"] == ReplayStatus.REUSE
        assert statuses["removed"] == ReplayStatus.ORPHAN
        assert statuses["changed_pure"] == ReplayStatus.RECOMPUTE
        assert statuses["changed_effect"] == ReplayStatus.ASK
        assert statuses["broken_contract"] == ReplayStatus.INVALIDATE
        assert statuses["brand_new"] == ReplayStatus.NEW


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestRBAC:
    def test_admin_has_all_permissions(self) -> None:
        for perm in Permission:
            assert authorize(Role.ADMIN, perm)

    def test_viewer_can_view(self) -> None:
        assert authorize(Role.VIEWER, Permission.RUN_VIEW)

    def test_viewer_cannot_author(self) -> None:
        assert not authorize(Role.VIEWER, Permission.FLOW_AUTHOR)

    def test_developer_permissions(self) -> None:
        assert authorize(Role.DEVELOPER, Permission.FLOW_AUTHOR)
        assert authorize(Role.DEVELOPER, Permission.FLOW_DEPLOY)
        assert authorize(Role.DEVELOPER, Permission.FLOW_RUN)
        assert authorize(Role.DEVELOPER, Permission.RUN_VIEW)
        assert not authorize(Role.DEVELOPER, Permission.GRANT_APPROVE)

    def test_operator_permissions(self) -> None:
        assert authorize(Role.OPERATOR, Permission.FLOW_RUN)
        assert authorize(Role.OPERATOR, Permission.RUN_VIEW)
        assert not authorize(Role.OPERATOR, Permission.FLOW_AUTHOR)

    def test_require_passes(self) -> None:
        require(Role.ADMIN, Permission.FLOW_AUTHOR)  # should not raise

    def test_require_raises(self) -> None:
        with pytest.raises(AuthorizationError, match="lacks permission"):
            require(Role.VIEWER, Permission.FLOW_AUTHOR)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    def test_default_policy_values(self) -> None:
        policy = RetentionPolicy()
        assert policy.journal_hot_days == 7
        assert policy.run_record_days == 365

    def test_compaction_result_total(self) -> None:
        result = CompactionResult(
            journals_archived=10,
            payloads_deleted=5,
            runs_archived=3,
            kv_expired=2,
        )
        assert result.total == 20

    def test_cutoff_date(self) -> None:
        policy = RetentionPolicy(journal_hot_days=7)
        mgr = RetentionManager(policy)
        cutoff = mgr.cutoff_date("journal_hot")
        expected = datetime.now(UTC) - timedelta(days=7)
        assert abs((cutoff - expected).total_seconds()) < 1.0

    def test_cutoff_invalid_category(self) -> None:
        mgr = RetentionManager(RetentionPolicy())
        with pytest.raises(ValueError, match="unknown category"):
            mgr.cutoff_date("invalid_category")

    def test_should_archive_run(self) -> None:
        mgr = RetentionManager(RetentionPolicy(run_record_days=30))
        old = datetime.now(UTC) - timedelta(days=31)
        recent = datetime.now(UTC) - timedelta(days=1)
        assert mgr.should_archive_run(old)
        assert not mgr.should_archive_run(recent)

    def test_should_archive_journal(self) -> None:
        mgr = RetentionManager(RetentionPolicy(journal_warm_days=90))
        old = datetime.now(UTC) - timedelta(days=91)
        recent = datetime.now(UTC) - timedelta(days=1)
        assert mgr.should_archive_journal(old)
        assert not mgr.should_archive_journal(recent)

    async def test_compact_is_a_no_op_on_an_empty_store(self) -> None:
        mgr = RetentionManager(RetentionPolicy())
        result = await mgr.compact(MemoryStore())
        assert result.total == 0


# ---------------------------------------------------------------------------
# Leader Election
# ---------------------------------------------------------------------------


class TestInMemoryLockProvider:
    async def test_acquire_success(self) -> None:
        lp = InMemoryLockProvider()
        assert await lp.acquire("lock1", "node-a", 30.0)

    async def test_acquire_blocked_by_other(self) -> None:
        lp = InMemoryLockProvider()
        assert await lp.acquire("lock1", "node-a", 30.0)
        assert not await lp.acquire("lock1", "node-b", 30.0)

    async def test_acquire_same_owner(self) -> None:
        lp = InMemoryLockProvider()
        assert await lp.acquire("lock1", "node-a", 30.0)
        assert await lp.acquire("lock1", "node-a", 30.0)

    async def test_renew_success(self) -> None:
        lp = InMemoryLockProvider()
        await lp.acquire("lock1", "node-a", 30.0)
        assert await lp.renew("lock1", "node-a", 30.0)

    async def test_renew_wrong_owner(self) -> None:
        lp = InMemoryLockProvider()
        await lp.acquire("lock1", "node-a", 30.0)
        assert not await lp.renew("lock1", "node-b", 30.0)

    async def test_renew_missing_key(self) -> None:
        lp = InMemoryLockProvider()
        assert not await lp.renew("lock1", "node-a", 30.0)

    async def test_release(self) -> None:
        lp = InMemoryLockProvider()
        await lp.acquire("lock1", "node-a", 30.0)
        await lp.release("lock1", "node-a")
        assert await lp.acquire("lock1", "node-b", 30.0)

    async def test_release_wrong_owner_noop(self) -> None:
        lp = InMemoryLockProvider()
        await lp.acquire("lock1", "node-a", 30.0)
        await lp.release("lock1", "node-b")  # no-op
        assert not await lp.acquire("lock1", "node-b", 30.0)


class TestLeaderElector:
    async def test_acquire_and_check(self) -> None:
        lp = InMemoryLockProvider()
        elector = LeaderElector(lp, "node-1")
        assert elector.node_id == "node-1"
        assert await elector.acquire_leadership("scheduler")
        assert await elector.is_leader("scheduler")

    async def test_two_nodes_election(self) -> None:
        lp = InMemoryLockProvider()
        e1 = LeaderElector(lp, "node-1")
        e2 = LeaderElector(lp, "node-2")

        assert await e1.acquire_leadership("scheduler")
        assert not await e2.acquire_leadership("scheduler")
        assert await e1.is_leader("scheduler")
        assert not await e2.is_leader("scheduler")

    async def test_release_and_takeover(self) -> None:
        lp = InMemoryLockProvider()
        e1 = LeaderElector(lp, "node-1")
        e2 = LeaderElector(lp, "node-2")

        await e1.acquire_leadership("scheduler")
        await e1.release("scheduler")
        assert await e2.acquire_leadership("scheduler")


# ---------------------------------------------------------------------------
# OTel Tracer
# ---------------------------------------------------------------------------


class TestOTelTracer:
    def test_import_without_otel_sdk(self) -> None:
        from workflow_builder.observability import otel
        assert hasattr(otel, "OTelTracer")

    def test_attribute_mapping(self) -> None:
        from workflow_builder.observability.otel import LOOM_ATTRS
        assert LOOM_ATTRS["run_id"] == "loom.run_id"
        assert LOOM_ATTRS["model"] == "gen_ai.request.model"

    def test_map_attributes(self) -> None:
        from workflow_builder.observability.otel import _map_attributes
        mapped = _map_attributes({"run_id": "abc", "custom": "val"})
        assert mapped == {"loom.run_id": "abc", "custom": "val"}

    def test_map_attributes_none(self) -> None:
        from workflow_builder.observability.otel import _map_attributes
        assert _map_attributes(None) == {}


# ---------------------------------------------------------------------------
# Context Extensions (compensate, patched, continue_as_new)
# ---------------------------------------------------------------------------


class TestContinueAsNewSignal:
    def test_is_control_signal(self) -> None:
        exc = ContinueAsNew(seed={"count": 42})
        assert isinstance(exc, ControlSignal)
        assert exc.seed == {"count": 42}


class TestContextExtensions:
    def test_context_has_compensate(self) -> None:
        from workflow_builder.runtime.context import Context
        assert hasattr(Context, "compensate")
        assert asyncio.iscoroutinefunction(Context.compensate)

    def test_context_has_run_compensations(self) -> None:
        from workflow_builder.runtime.context import Context
        assert hasattr(Context, "run_compensations")
        assert asyncio.iscoroutinefunction(Context.run_compensations)

    def test_context_has_patched(self) -> None:
        from workflow_builder.runtime.context import Context
        assert hasattr(Context, "patched")

    def test_context_has_continue_as_new(self) -> None:
        from workflow_builder.runtime.context import Context
        assert hasattr(Context, "continue_as_new")
        assert asyncio.iscoroutinefunction(Context.continue_as_new)


# ---------------------------------------------------------------------------
# Integration: end-to-end saga compensation via MemoryStore
# ---------------------------------------------------------------------------


class TestSagaIntegration:
    async def test_compensation_stack_runs_on_failure(self) -> None:
        @step
        async def charge_card(ctx, amount: int) -> str:
            return f"charged-{amount}"

        @step
        async def reserve_inventory(ctx, item: str) -> str:
            return f"reserved-{item}"

        @step
        async def send_confirmation(ctx, order_id: str) -> str:
            raise RuntimeError("email service down")

        unwound: list[str] = []

        async def reverse_charge(amount: int) -> None:
            unwound.append(f"reverse_charge:{amount}")

        async def release_inventory(item: str) -> None:
            unwound.append(f"release_inventory:{item}")

        @workflow
        async def order_flow(ctx, order_id: str) -> str:
            await ctx.step(charge_card, 100)
            await ctx.compensate(reverse_charge, 100)

            await ctx.step(reserve_inventory, "widget")
            await ctx.compensate(release_inventory, "widget")

            await ctx.step(send_confirmation, order_id)
            return "done"

        store = MemoryStore()
        rt = Runtime(store=store)
        result = await rt.run(order_flow, "order-1")

        assert result.status.value == "failed"

        journal = await store.load_journal(result.run_id)
        comp_entries = [
            e for e in journal
            if e.name.startswith("compensate:")
        ]
        assert len(comp_entries) == 2

        # The handlers actually ran, most-recently-registered first.
        assert unwound == ["release_inventory:widget", "reverse_charge:100"]

    async def test_failing_compensation_is_recorded_not_raised(self) -> None:
        @step
        async def book(ctx, what: str) -> str:
            return f"booked-{what}"

        @step
        async def explode(ctx) -> str:
            raise RuntimeError("downstream is down")

        async def cancel_booking(what: str) -> None:
            raise RuntimeError("cancellation endpoint is also down")

        @workflow
        async def trip_flow(ctx, _input: str) -> str:
            await ctx.step(book, "flight")
            await ctx.compensate(cancel_booking, "flight")
            await ctx.step(explode)
            return "done"

        rt = Runtime(store=MemoryStore())
        result = await rt.run(trip_flow, "go")

        # A compensation that fails must not mask the original failure.
        assert result.status.value == "failed"
        assert "downstream is down" in result.error.message

        record = await rt.get(result.run_id)
        assert record.metadata["compensation_failures"] == ["cancel_booking"]


class TestContinueAsNewIntegration:
    async def test_no_rotation_returns_normally(self) -> None:
        @workflow
        async def rotating_flow(ctx, counter: int) -> int:
            if counter < 3:
                await ctx.continue_as_new(counter + 1)
            return counter

        rt = Runtime(store=MemoryStore())
        result = await rt.run(rotating_flow, 3)
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == 3

    async def test_rotation_starts_a_successor_run(self) -> None:
        @workflow
        async def rotating_flow(ctx, counter: int) -> int:
            if counter < 3:
                await ctx.continue_as_new(counter + 1)
            return counter

        rt = Runtime(store=MemoryStore())
        try:
            first = await rt.run(rotating_flow, 1)

            # The rotating run completes rather than failing — the signal is
            # control flow, not an error.
            assert first.status is ExecutionStatus.COMPLETED

            chain = [first.run_id]
            record = await rt.get(first.run_id)
            while (successor := record.metadata.get("continued_as")) is not None:
                chain.append(successor)
                await rt.wait(successor, timeout=5)
                record = await rt.get(successor)

            # 1 -> 2 -> 3, so three runs, and only the last produces a value.
            assert len(chain) == 3
            assert record.status is ExecutionStatus.COMPLETED
            assert decode(record.output) == 3

            # Every run in the chain shares one root, so the whole forever-flow
            # stays queryable as a single logical execution.
            roots = {
                (await rt.get(run_id)).root_run_id or run_id for run_id in chain
            }
            assert roots == {chain[0]}

            # Journals do not accumulate across the chain — that is the point.
            # Each rotating run holds only its own continue_as_new marker, and
            # the run that finally returns holds nothing at all.
            assert [len(await rt.history(run_id)) for run_id in chain] == [1, 1, 0]
        finally:
            await rt.shutdown()


class TestVersionGate:
    async def test_patched_returns_false_by_default(self) -> None:
        @workflow
        async def gated_flow(ctx, data: str) -> str:
            if ctx.patched("new_format"):
                return f"v2:{data}"
            return f"v1:{data}"

        store = MemoryStore()
        rt = Runtime(store=store)
        result = await rt.run(gated_flow, "hello")
        assert result.output == "v1:hello"
