"""Tests for TriggerDispatcher, TriggerStore, and workflow tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# ---------------------------------------------------------------------------
# TriggerRecord model
# ---------------------------------------------------------------------------


class TestTriggerRecord:
    def test_fields_and_defaults(self) -> None:
        from workflow_builder.core.models import TriggerRecord

        t = TriggerRecord(
            trigger_id="t1",
            workflow="wf",
            spec={"cron": "0 9 * * *"},
        )
        assert t.enabled is True
        assert t.run_count == 0
        assert t.timezone == "UTC"
        assert t.next_fire_at is None
        assert t.last_fire_at is None

    def test_serialization_round_trip(self) -> None:
        from workflow_builder.core.models import TriggerRecord

        t = TriggerRecord(
            trigger_id="t1",
            workflow="wf",
            spec={"cron": "0 9 * * *"},
            next_fire_at=datetime.now(UTC),
        )
        data = t.model_dump_json()
        restored = TriggerRecord.model_validate_json(data)
        assert restored.trigger_id == t.trigger_id
        assert restored.workflow == t.workflow

    def test_with_all_fields(self) -> None:
        from workflow_builder.core.models import (
            TriggerKind,
            TriggerRecord,
        )

        now = datetime.now(UTC)
        t = TriggerRecord(
            trigger_id="t2",
            workflow="daily",
            kind=TriggerKind.SCHEDULE,
            spec={"cron": "0 9 * * *", "timezone": "US/Eastern"},
            next_fire_at=now + timedelta(hours=1),
            last_fire_at=now - timedelta(hours=23),
            enabled=True,
            run_count=42,
            timezone="US/Eastern",
        )
        assert t.run_count == 42
        assert t.timezone == "US/Eastern"


# ---------------------------------------------------------------------------
# InMemoryTriggerStore
# ---------------------------------------------------------------------------


class TestInMemoryTriggerStore:
    @pytest.mark.asyncio
    async def test_save_and_get(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        t = TriggerRecord(
            trigger_id="t1",
            workflow="wf",
            spec={"cron": "0 9 * * *"},
            next_fire_at=datetime.now(UTC),
        )
        await store.save_trigger(t)
        got = await store.get_trigger("t1")
        assert got is not None
        assert got.workflow == "wf"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self) -> None:
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        assert await store.get_trigger("nope") is None

    @pytest.mark.asyncio
    async def test_list_triggers_all(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        await store.save_trigger(
            TriggerRecord(trigger_id="a", workflow="w1", spec={})
        )
        await store.save_trigger(
            TriggerRecord(trigger_id="b", workflow="w2", spec={})
        )
        assert len(await store.list_triggers()) == 2

    @pytest.mark.asyncio
    async def test_list_triggers_by_workflow(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        await store.save_trigger(
            TriggerRecord(trigger_id="a", workflow="w1", spec={})
        )
        await store.save_trigger(
            TriggerRecord(trigger_id="b", workflow="w2", spec={})
        )
        assert len(await store.list_triggers(workflow="w1")) == 1

    @pytest.mark.asyncio
    async def test_due_triggers_filters_correctly(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        now = datetime.now(UTC)

        await store.save_trigger(TriggerRecord(
            trigger_id="past", workflow="w", spec={},
            next_fire_at=now - timedelta(minutes=5),
        ))
        await store.save_trigger(TriggerRecord(
            trigger_id="future", workflow="w", spec={},
            next_fire_at=now + timedelta(hours=1),
        ))
        await store.save_trigger(TriggerRecord(
            trigger_id="disabled", workflow="w", spec={},
            next_fire_at=now - timedelta(minutes=1),
            enabled=False,
        ))
        await store.save_trigger(TriggerRecord(
            trigger_id="no_time", workflow="w", spec={},
        ))

        due = await store.due_triggers(now)
        assert len(due) == 1
        assert due[0].trigger_id == "past"

    @pytest.mark.asyncio
    async def test_due_triggers_empty_store(self) -> None:
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        assert await store.due_triggers(datetime.now(UTC)) == []

    @pytest.mark.asyncio
    async def test_due_triggers_respects_limit(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        now = datetime.now(UTC)
        for i in range(10):
            await store.save_trigger(TriggerRecord(
                trigger_id=f"t{i}", workflow="w", spec={},
                next_fire_at=now - timedelta(minutes=i + 1),
            ))
        due = await store.due_triggers(now, limit=3)
        assert len(due) == 3

    @pytest.mark.asyncio
    async def test_due_triggers_timezone_naive_input(self) -> None:
        """Naive datetime should not crash — gets promoted to UTC."""
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        now_aware = datetime.now(UTC)
        await store.save_trigger(TriggerRecord(
            trigger_id="t", workflow="w", spec={},
            next_fire_at=now_aware - timedelta(minutes=1),
        ))
        # Pass naive datetime — should not crash
        naive_now = datetime.utcnow()
        due = await store.due_triggers(naive_now)
        assert len(due) == 1

    @pytest.mark.asyncio
    async def test_update_after_fire(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        now = datetime.now(UTC)
        await store.save_trigger(TriggerRecord(
            trigger_id="t", workflow="w", spec={},
            next_fire_at=now,
        ))

        next_fire = now + timedelta(hours=1)
        await store.update_after_fire("t", now, next_fire)

        t = await store.get_trigger("t")
        assert t is not None
        assert t.last_fire_at == now
        assert t.next_fire_at == next_fire
        assert t.run_count == 1

    @pytest.mark.asyncio
    async def test_update_after_fire_increments(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        now = datetime.now(UTC)
        await store.save_trigger(TriggerRecord(
            trigger_id="t", workflow="w", spec={},
            next_fire_at=now,
        ))
        await store.update_after_fire(
            "t", now, now + timedelta(hours=1)
        )
        await store.update_after_fire(
            "t", now + timedelta(hours=1), now + timedelta(hours=2)
        )
        t = await store.get_trigger("t")
        assert t is not None
        assert t.run_count == 2

    @pytest.mark.asyncio
    async def test_delete_trigger(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        await store.save_trigger(
            TriggerRecord(trigger_id="x", workflow="w", spec={})
        )
        await store.delete_trigger("x")
        assert await store.get_trigger("x") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self) -> None:
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        await store.delete_trigger("nope")  # should not raise

    @pytest.mark.asyncio
    async def test_clear_includes_triggers(self) -> None:
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.state.memory import MemoryStore

        store = MemoryStore()
        await store.save_trigger(
            TriggerRecord(trigger_id="t", workflow="w", spec={})
        )
        store.clear()
        assert await store.list_triggers() == []


# ---------------------------------------------------------------------------
# TriggerDispatcher
# ---------------------------------------------------------------------------


class TestTriggerDispatcher:
    @pytest.mark.asyncio
    async def test_register_creates_trigger(self) -> None:
        from workflow_builder import Context, workflow
        from workflow_builder.runtime.dispatcher import (
            TriggerDispatcher,
        )
        from workflow_builder.runtime.engine import Runtime
        from workflow_builder.triggers.specs import Schedule

        @workflow(
            name="daily",
            triggers=[Schedule("0 9 * * *")],
        )
        async def daily(ctx: Context, _: None = None) -> str:
            return "ok"

        rt = Runtime()
        dispatcher = TriggerDispatcher(rt)
        count = await dispatcher.register(daily)
        assert count == 1

        triggers = await dispatcher._store.list_triggers(
            workflow="daily"
        )
        assert len(triggers) == 1
        assert triggers[0].next_fire_at is not None

    @pytest.mark.asyncio
    async def test_tick_fires_due_trigger(self) -> None:
        from workflow_builder import Context, workflow
        from workflow_builder.runtime.dispatcher import (
            TriggerDispatcher,
        )
        from workflow_builder.runtime.engine import Runtime
        from workflow_builder.triggers.specs import Schedule

        @workflow(
            name="every_min",
            triggers=[Schedule("* * * * *")],
        )
        async def every_min(ctx: Context, _: None = None) -> str:
            return "fired"

        rt = Runtime()
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(every_min)

        # Tick 2 minutes in the future so trigger is due
        future = datetime.now(UTC) + timedelta(minutes=2)
        run_ids = await dispatcher.tick(future)
        assert len(run_ids) >= 1

        record = await rt.get(run_ids[0])
        assert record is not None

    @pytest.mark.asyncio
    async def test_tick_skips_future_triggers(self) -> None:
        from workflow_builder import Context, workflow
        from workflow_builder.runtime.dispatcher import (
            TriggerDispatcher,
        )
        from workflow_builder.runtime.engine import Runtime
        from workflow_builder.triggers.specs import Schedule

        @workflow(
            name="far_future",
            triggers=[Schedule("0 0 1 1 *")],  # jan 1 midnight
        )
        async def far_future(ctx: Context, _: None = None) -> str:
            return "nope"

        rt = Runtime()
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(far_future)

        run_ids = await dispatcher.tick()
        assert len(run_ids) == 0

    @pytest.mark.asyncio
    async def test_tick_handles_deleted_workflow(self) -> None:
        """Trigger for nonexistent workflow should be disabled."""
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.runtime.dispatcher import (
            TriggerDispatcher,
        )
        from workflow_builder.runtime.engine import Runtime

        rt = Runtime()
        dispatcher = TriggerDispatcher(rt)

        # Manually insert a trigger for a workflow that doesn't exist
        await dispatcher._store.save_trigger(TriggerRecord(
            trigger_id="orphan",
            workflow="deleted_workflow",
            spec={},
            next_fire_at=datetime.now(UTC) - timedelta(minutes=1),
        ))

        run_ids = await dispatcher.tick()
        assert len(run_ids) == 0

    @pytest.mark.asyncio
    async def test_interval_reconstruction(self) -> None:
        """Interval triggers should recompute next fire from seconds."""
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.runtime.dispatcher import (
            _next_fire_from_record,
        )

        now = datetime.now(UTC)
        trigger = TriggerRecord(
            trigger_id="int1",
            workflow="w",
            spec={"kind": "schedule", "seconds": 60},
        )
        next_fire = _next_fire_from_record(trigger, now)
        assert next_fire is not None
        assert (next_fire - now).total_seconds() == pytest.approx(
            60, abs=1
        )

    @pytest.mark.asyncio
    async def test_cron_reconstruction(self) -> None:
        """Schedule triggers should recompute next fire from cron."""
        from workflow_builder.core.models import TriggerRecord
        from workflow_builder.runtime.dispatcher import (
            _next_fire_from_record,
        )

        now = datetime.now(UTC)
        trigger = TriggerRecord(
            trigger_id="cron1",
            workflow="w",
            spec={
                "kind": "schedule",
                "cron": "*/5 * * * *",
                "timezone": "UTC",
            },
        )
        next_fire = _next_fire_from_record(trigger, now)
        assert next_fire is not None
        assert next_fire > now

    @pytest.mark.asyncio
    async def test_multiple_triggers_on_workflow(self) -> None:
        from workflow_builder import Context, workflow
        from workflow_builder.runtime.dispatcher import (
            TriggerDispatcher,
        )
        from workflow_builder.runtime.engine import Runtime
        from workflow_builder.triggers.specs import Schedule

        @workflow(
            name="multi",
            triggers=[
                Schedule("0 9 * * *"),
                Schedule("0 17 * * *"),
            ],
        )
        async def multi(ctx: Context, _: None = None) -> str:
            return "ok"

        rt = Runtime()
        dispatcher = TriggerDispatcher(rt)
        count = await dispatcher.register(multi)
        assert count == 2


# ---------------------------------------------------------------------------
# Workflow management tools
# ---------------------------------------------------------------------------


class TestWorkflowTools:
    @pytest.mark.asyncio
    async def test_list_workflows(self) -> None:
        import json

        from workflow_builder import Context, workflow
        from workflow_builder.agents.workflow_tools import (
            build_workflow_tools,
        )
        from workflow_builder.runtime.engine import Runtime

        @workflow(name="test_wf")
        async def wf(ctx: Context, x: str) -> str:
            return x

        rt = Runtime()
        rt.register(wf)
        tools = build_workflow_tools(rt)
        result = await tools[0]()  # list_workflows
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "test_wf"

    @pytest.mark.asyncio
    async def test_run_workflow(self) -> None:
        import json

        from workflow_builder import Context, workflow
        from workflow_builder.agents.workflow_tools import (
            build_workflow_tools,
        )
        from workflow_builder.runtime.engine import Runtime

        @workflow(name="adder")
        async def adder(ctx: Context, x: int) -> int:
            return x + 1

        rt = Runtime()
        rt.register(adder)
        tools = build_workflow_tools(rt)
        result = await tools[2]("adder", "5")  # run_workflow
        data = json.loads(result)
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_workflow_info(self) -> None:
        import json

        from workflow_builder import Context, workflow
        from workflow_builder.agents.workflow_tools import (
            build_workflow_tools,
        )
        from workflow_builder.runtime.engine import Runtime

        @workflow(name="info_test", description="A test workflow")
        async def info_test(ctx: Context, x: str) -> str:
            return x

        rt = Runtime()
        rt.register(info_test)
        tools = build_workflow_tools(rt)
        result = await tools[1]("info_test")  # get_workflow_info
        data = json.loads(result)
        assert data["name"] == "info_test"

    @pytest.mark.asyncio
    async def test_get_workflow_info_not_found(self) -> None:
        import json

        from workflow_builder.agents.workflow_tools import (
            build_workflow_tools,
        )
        from workflow_builder.runtime.engine import Runtime

        rt = Runtime()
        tools = build_workflow_tools(rt)
        result = await tools[1]("nonexistent")
        data = json.loads(result)
        assert "error" in data
