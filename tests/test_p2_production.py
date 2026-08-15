"""Coverage for the P2 production layer.

Retention, grants, RBAC, flow control, leader election, queue ingress, and the
HTTP surface. As with the P0/P1 tests, each capability is driven through the
public API — a policy object that computes the right answer in isolation while
the engine never consults it is exactly the failure mode this suite exists to
catch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.agents.result import AgentResult
from loom.agents.tool_registry import Toolset
from loom.blobs.retention import RetentionManager, RetentionPolicy
from loom.core.exceptions import (
    AdmissionRejected,
    ConfigurationError,
    GrantDenied,
)
from loom.runtime.flowcontrol import (
    AdmissionController,
    ConcurrencyPolicy,
    FlowControlPolicy,
    RateLimitPolicy,
    SingletonPolicy,
)
from loom.runtime.leader import InMemoryLockProvider, LeaderElector
from loom.security.grants import GrantSet
from loom.security.rbac import AuthorizationError, Role
from loom.stores.memory import MemoryStore
from loom.triggers.queue import InMemoryQueue, QueueConsumer
from loom.triggers.specs import OnEvent

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@step
async def noop(value: str) -> str:
    """Return the value unchanged."""
    return value


@workflow(name="p2_simple")
async def simple_wf(ctx: Context, value: str) -> str:
    """A workflow with one durable step."""
    return await ctx.step(noop, value or "x")


async def _seed_run(
    store: MemoryStore,
    *,
    status: ExecutionStatus,
    finished_days_ago: float,
    run_id: str,
) -> None:
    """Write a terminal-looking run straight to the store, aged as requested."""
    from loom.core.models import ExecutionRecord
    from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

    when = datetime.now(UTC) - timedelta(days=finished_days_ago)
    record = ExecutionRecord(
        run_id=run_id,
        workflow="p2_simple",
        status=status,
        created_at=when,
        finished_at=when if status.is_terminal else None,
    )
    await store.create_execution(record)
    await store.save_journal(
        run_id,
        [
            JournalEntry(
                path="0000",
                kind=EntryKind.STEP,
                name="noop",
                status=EntryStatus.COMPLETED,
                output="x",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    async def test_old_journals_are_dropped_but_records_kept(self) -> None:
        store = MemoryStore()
        await _seed_run(
            store, status=ExecutionStatus.COMPLETED, finished_days_ago=120, run_id="old"
        )

        result = await RetentionManager(RetentionPolicy()).compact(store)

        assert result.journals_archived == 1
        assert result.runs_archived == 0
        # The journal is gone; the run is still findable.
        assert await store.load_journal("old") == []
        assert await store.get_execution("old") is not None

    async def test_very_old_runs_are_deleted_outright(self) -> None:
        store = MemoryStore()
        await _seed_run(
            store, status=ExecutionStatus.FAILED, finished_days_ago=400, run_id="ancient"
        )

        result = await RetentionManager(RetentionPolicy()).compact(store)

        assert result.runs_archived == 1
        # Not double-counted as a journal archival.
        assert result.journals_archived == 0
        assert await store.get_execution("ancient") is None
        assert await store.load_journal("ancient") == []

    async def test_recent_runs_are_untouched(self) -> None:
        store = MemoryStore()
        await _seed_run(
            store, status=ExecutionStatus.COMPLETED, finished_days_ago=1, run_id="fresh"
        )

        result = await RetentionManager(RetentionPolicy()).compact(store)

        assert result.total == 0
        assert len(await store.load_journal("fresh")) == 1

    async def test_suspended_runs_are_never_compacted(self) -> None:
        """A run parked on a long timer is old but very much alive."""
        store = MemoryStore()
        await _seed_run(
            store,
            status=ExecutionStatus.SUSPENDED,
            finished_days_ago=400,
            run_id="parked",
        )

        result = await RetentionManager(RetentionPolicy()).compact(store)

        assert result.total == 0
        assert await store.get_execution("parked") is not None
        assert len(await store.load_journal("parked")) == 1

    async def test_dry_run_counts_without_deleting(self) -> None:
        store = MemoryStore()
        await _seed_run(
            store, status=ExecutionStatus.COMPLETED, finished_days_ago=400, run_id="old"
        )

        result = await RetentionManager(RetentionPolicy()).compact(store, dry_run=True)

        assert result.runs_archived == 1
        assert await store.get_execution("old") is not None

    async def test_deleting_frees_the_idempotency_key(self) -> None:
        """Otherwise the key resolves forever to a run that no longer exists."""
        from loom.core.models import ExecutionRecord

        store = MemoryStore()
        await store.create_execution(
            ExecutionRecord(run_id="r1", workflow="w", idempotency_key="key-1")
        )
        await store.delete_execution("r1")

        assert await store.find_by_idempotency_key("key-1") is None


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


@step
async def tickets_search(query: str) -> list:
    """Search tickets."""
    return []


@step
async def tickets_delete(key: str) -> bool:
    """Delete a ticket permanently."""
    return True


class TestGrantMatching:
    def test_bare_toolset_grants_everything_in_it(self) -> None:
        grants = GrantSet(toolsets=["jira"])
        assert grants.allows_operation("jira", "issues.search", "read")
        assert grants.allows_operation("jira", "issues.delete", "destructive")

    def test_effect_scoped_grant(self) -> None:
        grants = GrantSet(toolsets=["jira:read"])
        assert grants.allows_operation("jira", "issues.search", "read")
        assert not grants.allows_operation("jira", "issues.create", "write")

    def test_group_scoped_grant(self) -> None:
        grants = GrantSet(toolsets=["jira.issues"])
        assert grants.allows_operation("jira", "issues.search", "read")
        assert not grants.allows_operation("jira", "boards.list", "read")

    def test_group_and_effect_scoped_grant(self) -> None:
        grants = GrantSet(toolsets=["jira.issues:write"])
        assert grants.allows_operation("jira", "issues.create", "write")
        assert not grants.allows_operation("jira", "issues.search", "read")

    def test_empty_grant_set_allows_nothing(self) -> None:
        """A declared-but-empty grant set is a deny-all, not a wildcard."""
        assert not GrantSet().allows_operation("jira", "issues.search", "read")

    def test_other_toolsets_are_denied(self) -> None:
        grants = GrantSet(toolsets=["jira"])
        assert not grants.allows_operation("slack", "chat.post", "write")


class TestGrantEnforcement:
    def test_resolve_tools_withholds_ungranted_operations(self) -> None:
        rt = Runtime(store=MemoryStore())
        rt.toolsets.register(
            Toolset.from_steps("tickets", [tickets_search, tickets_delete])
        )

        granted = rt.toolsets.resolve_tools(grants=GrantSet(toolsets=["tickets:read"]))
        assert [t.name for t in granted] == ["tickets_search"]

    def test_naming_a_denied_toolset_raises(self) -> None:
        rt = Runtime(store=MemoryStore())
        rt.toolsets.register(Toolset.from_steps("tickets", [tickets_search]))

        with pytest.raises(GrantDenied, match="tickets"):
            rt.toolsets.resolve_tools(["tickets"], grants=GrantSet(toolsets=["slack"]))

    def test_no_grants_means_no_restriction(self) -> None:
        rt = Runtime(store=MemoryStore())
        rt.toolsets.register(
            Toolset.from_steps("tickets", [tickets_search, tickets_delete])
        )
        assert len(rt.toolsets.resolve_tools(grants=None)) == 2

    async def test_workflow_grants_narrow_what_ctx_agent_sees(self) -> None:
        seen: list[list[str]] = []

        class SpyBackend:
            supports_history = False

            async def run(self, prompt, *, tools=None, history=None, agent_id="",
                          max_turns=None):
                seen.append([t.name for t in (tools or [])])
                return AgentResult(output="ok", agent=agent_id)

        @workflow(name="granted_wf", grants=GrantSet(toolsets=["tickets:read"]))
        async def granted_wf(ctx: Context, _input: str) -> str:
            return (await ctx.agent("do something")).output

        rt = Runtime(store=MemoryStore(), agent_backend=SpyBackend())
        rt.toolsets.register(
            Toolset.from_steps("tickets", [tickets_search, tickets_delete])
        )

        result = await rt.run(granted_wf, "go")
        assert result.status is ExecutionStatus.COMPLETED
        # The destructive tool was never put in front of the model.
        assert seen == [["tickets_search"]]

    async def test_workflow_without_grants_sees_everything(self) -> None:
        seen: list[list[str]] = []

        class SpyBackend:
            supports_history = False

            async def run(self, prompt, *, tools=None, history=None, agent_id="",
                          max_turns=None):
                seen.append([t.name for t in (tools or [])])
                return AgentResult(output="ok", agent=agent_id)

        @workflow(name="ungranted_wf")
        async def ungranted_wf(ctx: Context, _input: str) -> str:
            return (await ctx.agent("do something")).output

        rt = Runtime(store=MemoryStore(), agent_backend=SpyBackend())
        rt.toolsets.register(
            Toolset.from_steps("tickets", [tickets_search, tickets_delete])
        )

        await rt.run(ungranted_wf, "go")
        assert sorted(seen[0]) == ["tickets_delete", "tickets_search"]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestRuntimeRBAC:
    async def test_no_role_enforces_nothing(self) -> None:
        rt = Runtime(store=MemoryStore())
        result = await rt.run(simple_wf, "go")
        assert result.status is ExecutionStatus.COMPLETED

    async def test_viewer_cannot_start_a_run(self) -> None:
        rt = Runtime(store=MemoryStore(), role=Role.VIEWER)
        with pytest.raises(AuthorizationError, match="flow:run"):
            await rt.run(simple_wf, "go")

    async def test_viewer_can_read(self) -> None:
        author = Runtime(store=(store := MemoryStore()))
        result = await author.run(simple_wf, "go")

        viewer = Runtime(store=store, role=Role.VIEWER)
        viewer.register(simple_wf)
        assert (await viewer.get(result.run_id)) is not None
        assert len(await viewer.history(result.run_id)) >= 1

    async def test_operator_can_run_and_cancel_but_not_replay(self) -> None:
        rt = Runtime(store=MemoryStore(), role=Role.OPERATOR)
        result = await rt.run(simple_wf, "go")
        await rt.cancel(result.run_id)

        with pytest.raises(AuthorizationError, match="run:replay"):
            await rt.replay(result.run_id)

    async def test_viewer_cannot_cancel(self) -> None:
        author = Runtime(store=(store := MemoryStore()))
        result = await author.run(simple_wf, "go")

        viewer = Runtime(store=store, role=Role.VIEWER)
        with pytest.raises(AuthorizationError, match="flow:cancel"):
            await viewer.cancel(result.run_id)

    async def test_admin_may_do_everything(self) -> None:
        rt = Runtime(store=MemoryStore(), role=Role.ADMIN)
        result = await rt.run(simple_wf, "go")
        replayed = await rt.replay(result.run_id)
        assert replayed.status is ExecutionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Flow control
# ---------------------------------------------------------------------------


@workflow(
    name="p2_singleton",
    flow_control=FlowControlPolicy(singleton=SingletonPolicy(mode="skip")),
)
async def singleton_wf(ctx: Context, _input: object = None) -> str:
    """Parks, so it stays in flight while a second submit is attempted.

    Declares ``object`` because both callers here supply different shapes — a
    string from ``rt.run`` and a queue message dict from the consumer. An
    annotation the callers do not honour is not a contract, and the ingress
    gate now says so.
    """
    await ctx.wait_for_event("release")
    return "done"


@workflow(
    name="p2_limited",
    flow_control=FlowControlPolicy(rate_limit=RateLimitPolicy(requests=2, period_seconds=60)),
)
async def limited_wf(ctx: Context, value: str) -> str:
    """Rate limited to two starts per minute."""
    return await ctx.step(noop, value or "x")


class TestFlowControl:
    async def test_policy_without_a_controller_does_nothing(self) -> None:
        """Opt-in: the policy is declared, but no controller is configured."""
        rt = Runtime(store=MemoryStore())
        for _ in range(5):
            assert (await rt.run(limited_wf, "go")).status is ExecutionStatus.COMPLETED

    async def test_rate_limit_rejects_beyond_the_window(self) -> None:
        rt = Runtime(store=MemoryStore(), admission=AdmissionController())

        assert (await rt.run(limited_wf, "a")).status is ExecutionStatus.COMPLETED
        assert (await rt.run(limited_wf, "b")).status is ExecutionStatus.COMPLETED

        with pytest.raises(AdmissionRejected) as caught:
            await rt.run(limited_wf, "c")
        assert caught.value.decision == "delay"
        assert caught.value.retryable
        assert caught.value.delay_seconds > 0

    async def test_rejected_runs_leave_no_record_behind(self) -> None:
        store = MemoryStore()
        rt = Runtime(store=store, admission=AdmissionController())
        await rt.run(limited_wf, "a")
        await rt.run(limited_wf, "b")
        with pytest.raises(AdmissionRejected):
            await rt.run(limited_wf, "c")

        # Two starts, two records — the rejection did not create a third.
        assert len(await store.list_executions(workflow="p2_limited")) == 2

    async def test_singleton_skips_while_one_is_in_flight(self) -> None:
        rt = Runtime(store=MemoryStore(), admission=AdmissionController())

        first = await rt.run(singleton_wf, "go")
        assert first.status is ExecutionStatus.SUSPENDED

        with pytest.raises(AdmissionRejected) as caught:
            await rt.run(singleton_wf, "go")
        assert caught.value.decision == "skip"
        assert not caught.value.retryable

    async def test_the_slot_is_released_when_the_run_finishes(self) -> None:
        rt = Runtime(store=MemoryStore(), admission=AdmissionController())

        first = await rt.run(singleton_wf, "go")
        await rt.send_event(first.run_id, "release", None)
        await rt.resume(first.run_id)
        assert (await rt.get(first.run_id)).status is ExecutionStatus.COMPLETED

        # With the first run terminal, a second is admitted.
        second = await rt.run(singleton_wf, "go")
        assert second.status is ExecutionStatus.SUSPENDED
        assert second.run_id != first.run_id

    async def test_concurrency_policy_counts_live_runs(self) -> None:
        @workflow(
            name="p2_concurrent",
            flow_control=FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=2)),
        )
        async def concurrent_wf(ctx: Context, _input: str) -> str:
            await ctx.wait_for_event("release")
            return "done"

        rt = Runtime(store=MemoryStore(), admission=AdmissionController())
        await rt.run(concurrent_wf, "a")
        await rt.run(concurrent_wf, "b")

        with pytest.raises(AdmissionRejected, match="concurrency limit"):
            await rt.run(concurrent_wf, "c")


# ---------------------------------------------------------------------------
# Leader election
# ---------------------------------------------------------------------------


class TestLeaderElection:
    async def test_only_one_node_holds_the_lease(self) -> None:
        locks = InMemoryLockProvider()
        alice = LeaderElector(locks, "alice")
        bob = LeaderElector(locks, "bob")

        assert await alice.acquire_leadership("scheduler", 30)
        assert not await bob.acquire_leadership("scheduler", 30)

        await alice.release("scheduler")
        assert await bob.acquire_leadership("scheduler", 30)

    async def test_scheduler_only_ticks_for_the_leader(self) -> None:
        import asyncio

        locks = InMemoryLockProvider()
        # Someone else already holds the lease.
        assert await LeaderElector(locks, "incumbent").acquire_leadership("scheduler", 30)

        ticks: list[int] = []
        rt = Runtime(store=MemoryStore())

        async def counting_tick(*args: object, **kwargs: object) -> list[str]:
            ticks.append(1)
            return []

        rt.tick = counting_tick  # type: ignore[method-assign]
        await rt.start_scheduler(
            interval=0.01, elector=LeaderElector(locks, "follower")
        )
        await asyncio.sleep(0.05)
        await rt.shutdown()

        assert ticks == []

    async def test_scheduler_ticks_when_it_wins_the_lease(self) -> None:
        import asyncio

        ticks: list[int] = []
        rt = Runtime(store=MemoryStore())

        async def counting_tick(*args: object, **kwargs: object) -> list[str]:
            ticks.append(1)
            return []

        rt.tick = counting_tick  # type: ignore[method-assign]
        await rt.start_scheduler(
            interval=0.01, elector=LeaderElector(InMemoryLockProvider(), "solo")
        )
        await asyncio.sleep(0.05)
        await rt.shutdown()

        assert ticks


# ---------------------------------------------------------------------------
# Queue ingress
# ---------------------------------------------------------------------------


@workflow(name="p2_queue", triggers=[OnEvent(topic="orders", idempotency_field="order_id")])
async def queue_wf(ctx: Context, payload: dict) -> str:
    """Consume an order message."""
    return await ctx.step(noop, str(payload.get("order_id", "?")))


class TestQueueConsumer:
    async def test_messages_become_runs_and_are_acked(self) -> None:
        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"})
        queue.publish({"order_id": "A2"})

        rt = Runtime(store=MemoryStore())
        consumer = QueueConsumer(rt, queue, queue_wf, batch_size=10)

        report = await consumer.poll_once()
        await rt.shutdown()

        assert len(report.submitted) == 2
        assert queue.depth == 0
        assert queue.in_flight == 0  # both acked

    async def test_declared_batch_size_is_honoured(self) -> None:
        """OnEvent(batch_size=...) governs the poll, whichever constructor is used."""
        queue = InMemoryQueue()
        for i in range(3):
            queue.publish({"order_id": f"A{i}"})

        rt = Runtime(store=MemoryStore())
        # queue_wf's OnEvent leaves batch_size at its default of 1.
        report = await QueueConsumer(rt, queue, queue_wf).poll_once()
        await rt.shutdown()

        assert len(report.submitted) == 1
        assert queue.depth == 2

    async def test_explicit_batch_size_beats_the_declaration(self) -> None:
        queue = InMemoryQueue()
        for i in range(3):
            queue.publish({"order_id": f"B{i}"})

        rt = Runtime(store=MemoryStore())
        report = await QueueConsumer(rt, queue, queue_wf, batch_size=3).poll_once()
        await rt.shutdown()

        assert len(report.submitted) == 3

    async def test_redelivery_does_not_start_a_second_run(self) -> None:
        """At-least-once delivery, exactly-once execution."""
        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"}, message_id="m1")

        rt = Runtime(store=MemoryStore())
        consumer = QueueConsumer(rt, queue, queue_wf)
        first = await consumer.poll_once()

        # The broker redelivers the same message under a fresh id.
        queue.publish({"order_id": "A1"}, message_id="m1-redelivered")
        second = await consumer.poll_once()
        await rt.shutdown()

        # Same run id both times, because the idempotency_field deduplicates.
        assert first.submitted == second.submitted

    async def test_message_id_dedupes_when_no_idempotency_field(self) -> None:
        @workflow(name="p2_queue_plain", triggers=[OnEvent(topic="plain")])
        async def plain_wf(ctx: Context, payload: dict) -> str:
            return await ctx.step(noop, "ok")

        queue = InMemoryQueue()
        queue.publish({"x": 1}, message_id="m1")

        rt = Runtime(store=MemoryStore())
        consumer = QueueConsumer.for_workflow(rt, queue, plain_wf)
        first = await consumer.poll_once()

        queue.publish({"x": 1}, message_id="m1")
        second = await consumer.poll_once()
        await rt.shutdown()

        assert first.submitted == second.submitted

    async def test_a_failed_submit_is_not_acked(self) -> None:
        """The message is still owed if the run was never recorded."""
        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"})

        rt = Runtime(store=MemoryStore())

        async def exploding_submit(*args: object, **kwargs: object) -> str:
            raise RuntimeError("store is down")

        rt.submit = exploding_submit  # type: ignore[method-assign]
        report = await QueueConsumer(rt, queue, queue_wf).poll_once()

        assert report.submitted == []
        assert report.requeued
        assert queue.depth == 1  # back on the queue, not lost
        assert queue.in_flight == 0

    async def test_dead_letter_after_max_attempts(self) -> None:
        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"})

        rt = Runtime(store=MemoryStore())

        async def exploding_submit(*args: object, **kwargs: object) -> str:
            raise RuntimeError("permanently broken")

        rt.submit = exploding_submit  # type: ignore[method-assign]
        consumer = QueueConsumer(rt, queue, queue_wf, max_attempts=2)

        first = await consumer.poll_once()
        assert first.requeued
        second = await consumer.poll_once()

        assert second.dead_lettered
        assert queue.depth == 0
        assert [m.payload for m in queue.dead_letters] == [{"order_id": "A1"}]

    async def test_admission_skip_dead_letters_instead_of_looping(self) -> None:
        """A skipped message will never be wanted; requeueing it spins forever."""
        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"})
        queue.publish({"order_id": "A2"})

        rt = Runtime(store=MemoryStore(), admission=AdmissionController())
        rt.register(singleton_wf)
        consumer = QueueConsumer(rt, queue, singleton_wf)

        report = await consumer.poll_once()
        await rt.shutdown()

        assert len(report.submitted) == 1
        assert len(report.dead_lettered) == 1
        assert queue.depth == 0

    def test_for_workflow_reads_the_trigger_declaration(self) -> None:
        rt = Runtime(store=MemoryStore())
        consumer = QueueConsumer.for_workflow(rt, InMemoryQueue(), queue_wf)

        from loom.triggers.queue import QueueMessage

        key = consumer.idempotency_key(QueueMessage(id="m9", payload={"order_id": "Z"}))
        assert key.endswith(":Z")  # the declared field won, not the message id

    def test_for_workflow_rejects_a_workflow_with_no_event_trigger(self) -> None:
        rt = Runtime(store=MemoryStore())
        with pytest.raises(ConfigurationError, match="no OnEvent trigger"):
            QueueConsumer.for_workflow(rt, InMemoryQueue(), simple_wf)

    async def test_start_and_stop_drain_the_queue(self) -> None:
        import asyncio

        queue = InMemoryQueue()
        queue.publish({"order_id": "A1"})

        rt = Runtime(store=MemoryStore())
        consumer = QueueConsumer(rt, queue, queue_wf)
        await consumer.start(interval=0.01)
        await asyncio.sleep(0.05)
        await consumer.stop()
        await rt.shutdown()

        assert queue.depth == 0
        assert len(await rt.list_runs(workflow="p2_queue")) == 1


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

pytest.importorskip("fastapi", reason="server tests need the api extra")


@workflow(name="p2_http")
async def http_wf(ctx: Context, value: str) -> str:
    """A workflow reachable over HTTP."""
    return await ctx.step(noop, value or "x")


@workflow(name="p2_http_approval")
async def http_approval_wf(ctx: Context, _input: str) -> str:
    """Parks on a human decision, to be resolved over HTTP."""
    return "approved" if await ctx.wait_for_approval("release") else "rejected"


@pytest.fixture
def api():
    """An in-process client bound to an app over a fresh Runtime."""
    import httpx

    from loom.server import LoomClient
    from loom.server.app import create_app

    rt = Runtime(store=MemoryStore())
    rt.register_all([http_wf, http_approval_wf])
    app = create_app(rt)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
    return LoomClient(http=http), rt


class TestHttpSurface:
    async def test_health(self, api) -> None:
        client, _ = api
        assert await client._request("GET", "/health") == {"status": "ok"}

    async def test_list_workflows_exposes_schemas(self, api) -> None:
        client, _ = api
        names = {w["name"] for w in await client.workflows()}
        assert {"p2_http", "p2_http_approval"} <= names

    async def test_start_and_read_a_run(self, api) -> None:
        client, _ = api
        run = await client.start("p2_http", "hello", wait=True)

        assert run["status"] == "completed"
        assert run["output"] == "hello"
        assert (await client.get(run["run_id"]))["run_id"] == run["run_id"]

    async def test_the_http_view_matches_the_embedded_one(self, api) -> None:
        """One execution history, not two that can drift."""
        client, rt = api
        run = await client.start("p2_http", "hello", wait=True)

        record = await rt.get(run["run_id"])
        assert record is not None
        assert record.status.value == run["status"]

    async def test_journal_is_readable(self, api) -> None:
        client, _ = api
        run = await client.start("p2_http", "hello", wait=True)

        journal = await client.journal(run["run_id"])
        assert [e["name"] for e in journal] == ["noop"]
        assert journal[0]["status"] == "completed"

    async def test_idempotency_key_returns_the_same_run(self, api) -> None:
        client, _ = api
        first = await client.start("p2_http", "a", idempotency_key="k1", wait=True)
        second = await client.start("p2_http", "a", idempotency_key="k1", wait=True)

        assert first["run_id"] == second["run_id"]

    async def test_unknown_workflow_is_404(self, api) -> None:
        from loom.server import LoomClientError

        client, _ = api
        with pytest.raises(LoomClientError) as caught:
            await client.start("does_not_exist")
        assert caught.value.status_code == 404
        assert not caught.value.retryable

    async def test_unknown_run_is_404(self, api) -> None:
        from loom.server import LoomClientError

        client, _ = api
        with pytest.raises(LoomClientError) as caught:
            await client.get("run_nope")
        assert caught.value.status_code == 404

    async def test_approval_can_be_delivered_over_http(self, api) -> None:
        client, _ = api
        run = await client.start("p2_http_approval", None, wait=True)
        assert run["status"] == "suspended"

        await client.approve(run["run_id"], "release")
        final = await client.wait(run["run_id"], timeout=2.0, poll_interval=0.01)

        assert final["status"] == "completed"
        assert final["output"] == "approved"

    async def test_cancel_over_http(self, api) -> None:
        client, _ = api
        run = await client.start("p2_http_approval", None, wait=True)

        cancelled = await client.cancel(run["run_id"])
        assert cancelled["status"] == "cancelled"

    async def test_replay_over_http(self, api) -> None:
        client, _ = api
        run = await client.start("p2_http", "hello", wait=True)

        replayed = await client.replay(run["run_id"])
        assert replayed["status"] == "completed"
        assert replayed["run_id"] != run["run_id"]

    async def test_list_runs_filters(self, api) -> None:
        client, _ = api
        await client.start("p2_http", "a", wait=True)
        await client.start("p2_http", "b", wait=True)

        assert len(await client.list_runs(workflow="p2_http")) == 2
        assert await client.list_runs(workflow="nothing_here") == []

    async def test_rbac_denial_is_403(self) -> None:
        import httpx

        from loom.server import LoomClient, LoomClientError
        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore(), role=Role.VIEWER)
        rt.register(http_wf)
        transport = httpx.ASGITransport(app=create_app(rt))
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )

        with pytest.raises(LoomClientError) as caught:
            await client.start("p2_http", "x")
        assert caught.value.status_code == 403

    async def test_admission_rejection_maps_to_429(self) -> None:
        import httpx

        from loom.server import LoomClient, LoomClientError
        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore(), admission=AdmissionController())
        rt.register(limited_wf)
        transport = httpx.ASGITransport(app=create_app(rt))
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )

        await client.start("p2_limited", "a", wait=True)
        await client.start("p2_limited", "b", wait=True)
        with pytest.raises(LoomClientError) as caught:
            await client.start("p2_limited", "c", wait=True)

        assert caught.value.status_code == 429
        assert caught.value.retryable
