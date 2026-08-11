"""Tests for Phase 9 — MCP Server.

Covers: RuntimeBridge, tool handlers, resource handlers,
prompt builders, server factory.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# RuntimeBridge
# ---------------------------------------------------------------------------


class TestRuntimeBridge:
    @pytest.fixture()
    def bridge(self):
        from workflow_builder.mcp_server.bridge import RuntimeBridge

        b = RuntimeBridge(store_url="memory://")
        b.register_workflow(
            "lead_outreach",
            description="Lead enrichment and outreach",
            input_schema={"type": "object"},
        )
        return b

    @pytest.mark.asyncio()
    async def test_list_workflows(self, bridge) -> None:
        wfs = await bridge.list_workflows()
        assert len(wfs) == 1
        assert wfs[0]["id"] == "lead_outreach"

    @pytest.mark.asyncio()
    async def test_list_workflows_empty(self) -> None:
        from workflow_builder.mcp_server.bridge import RuntimeBridge

        b = RuntimeBridge()
        wfs = await b.list_workflows()
        assert wfs == []

    @pytest.mark.asyncio()
    async def test_run_workflow(self, bridge) -> None:
        result = await bridge.run_workflow(
            "lead_outreach", {"source": "test"}
        )
        assert "run_id" in result
        assert result["status"] == "completed"

    @pytest.mark.asyncio()
    async def test_run_unknown_workflow(self, bridge) -> None:
        result = await bridge.run_workflow("nonexistent", {})
        assert "error" in result

    @pytest.mark.asyncio()
    async def test_get_run_status(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        status = await bridge.get_run_status(run["run_id"])
        assert status["workflow_id"] == "lead_outreach"
        assert status["status"] == "completed"

    @pytest.mark.asyncio()
    async def test_get_run_status_not_found(
        self, bridge
    ) -> None:
        status = await bridge.get_run_status("nonexistent")
        assert "error" in status

    @pytest.mark.asyncio()
    async def test_list_runs(self, bridge) -> None:
        await bridge.run_workflow("lead_outreach", {})
        await bridge.run_workflow("lead_outreach", {})
        runs = await bridge.list_runs()
        assert len(runs) == 2

    @pytest.mark.asyncio()
    async def test_list_runs_filter_workflow(
        self, bridge
    ) -> None:
        bridge.register_workflow("other", description="Other")
        await bridge.run_workflow("lead_outreach", {})
        await bridge.run_workflow("other", {})
        runs = await bridge.list_runs(
            workflow_id="lead_outreach"
        )
        assert len(runs) == 1

    @pytest.mark.asyncio()
    async def test_list_runs_limit(self, bridge) -> None:
        for _ in range(5):
            await bridge.run_workflow("lead_outreach", {})
        runs = await bridge.list_runs(limit=3)
        assert len(runs) == 3

    @pytest.mark.asyncio()
    async def test_cancel_run(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        result = await bridge.cancel_run(run["run_id"])
        assert result["status"] == "cancelled"
        status = await bridge.get_run_status(run["run_id"])
        assert status["status"] == "cancelled"

    @pytest.mark.asyncio()
    async def test_resume_run(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        result = await bridge.resume_run(run["run_id"])
        assert result["status"] == "completed"

    @pytest.mark.asyncio()
    async def test_send_event(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        result = await bridge.send_event(
            run["run_id"], "approval", {"approved": True}
        )
        assert result["delivered"] is True

    @pytest.mark.asyncio()
    async def test_get_run_journal(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        journal = await bridge.get_run_journal(
            run["run_id"]
        )
        assert len(journal) >= 1
        assert journal[0]["kind"] == "step"

    @pytest.mark.asyncio()
    async def test_get_journal_not_found(
        self, bridge
    ) -> None:
        journal = await bridge.get_run_journal("nope")
        assert journal == []

    @pytest.mark.asyncio()
    async def test_replay_run(self, bridge) -> None:
        run = await bridge.run_workflow(
            "lead_outreach", {}
        )
        result = await bridge.replay_run(run["run_id"])
        assert result["replay_status"] == "completed"

    @pytest.mark.asyncio()
    async def test_replay_not_found(self, bridge) -> None:
        result = await bridge.replay_run("nope")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------


class TestToolHandlers:
    @pytest.fixture()
    def bridge(self):
        from workflow_builder.mcp_server.bridge import RuntimeBridge

        b = RuntimeBridge()
        b.register_workflow(
            "test_wf", description="A test workflow"
        )
        return b

    @pytest.mark.asyncio()
    async def test_handle_list_workflows(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_list_workflows,
        )

        result = await handle_list_workflows(bridge)
        assert "test_wf" in result
        assert "A test workflow" in result

    @pytest.mark.asyncio()
    async def test_handle_list_workflows_empty(self) -> None:
        from workflow_builder.mcp_server.bridge import (
            RuntimeBridge,
        )
        from workflow_builder.mcp_server.tools import (
            handle_list_workflows,
        )

        b = RuntimeBridge()
        result = await handle_list_workflows(b)
        assert "No workflows" in result

    @pytest.mark.asyncio()
    async def test_handle_run_workflow(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_run_workflow,
        )

        result = await handle_run_workflow(
            bridge, "test_wf", '{"key": "val"}'
        )
        assert "completed" in result or "Run" in result

    @pytest.mark.asyncio()
    async def test_handle_run_invalid_json(
        self, bridge
    ) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_run_workflow,
        )

        result = await handle_run_workflow(
            bridge, "test_wf", "not json"
        )
        assert "Error" in result

    @pytest.mark.asyncio()
    async def test_handle_get_run_status(
        self, bridge
    ) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_get_run_status,
            handle_run_workflow,
        )

        await handle_run_workflow(
            bridge, "test_wf", "{}"
        )
        # Extract run_id from bridge directly
        runs = await bridge.list_runs()
        run_id = runs[0]["run_id"]
        status = await handle_get_run_status(bridge, run_id)
        assert "test_wf" in status

    @pytest.mark.asyncio()
    async def test_handle_list_runs(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_list_runs,
            handle_run_workflow,
        )

        await handle_run_workflow(bridge, "test_wf", "{}")
        result = await handle_list_runs(bridge)
        assert "test_wf" in result

    @pytest.mark.asyncio()
    async def test_handle_list_runs_empty(self) -> None:
        from workflow_builder.mcp_server.bridge import (
            RuntimeBridge,
        )
        from workflow_builder.mcp_server.tools import (
            handle_list_runs,
        )

        b = RuntimeBridge()
        result = await handle_list_runs(b)
        assert "No runs" in result

    @pytest.mark.asyncio()
    async def test_handle_cancel_run(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_cancel_run,
        )

        run = await bridge.run_workflow("test_wf", {})
        result = await handle_cancel_run(
            bridge, run["run_id"]
        )
        assert "cancelled" in result

    @pytest.mark.asyncio()
    async def test_handle_send_event(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_send_event,
        )

        run = await bridge.run_workflow("test_wf", {})
        result = await handle_send_event(
            bridge,
            run["run_id"],
            "approval",
            '{"ok": true}',
        )
        assert "delivered" in result

    @pytest.mark.asyncio()
    async def test_handle_send_event_bad_json(
        self, bridge
    ) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_send_event,
        )

        result = await handle_send_event(
            bridge, "x", "ev", "bad"
        )
        assert "Error" in result

    @pytest.mark.asyncio()
    async def test_handle_get_run_logs(
        self, bridge
    ) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_get_run_logs,
        )

        run = await bridge.run_workflow("test_wf", {})
        result = await handle_get_run_logs(
            bridge, run["run_id"]
        )
        assert "step" in result

    @pytest.mark.asyncio()
    async def test_handle_replay_run(self, bridge) -> None:
        from workflow_builder.mcp_server.tools import (
            handle_replay_run,
        )

        run = await bridge.run_workflow("test_wf", {})
        result = await handle_replay_run(
            bridge, run["run_id"]
        )
        assert "completed" in result


# ---------------------------------------------------------------------------
# Resource Handlers
# ---------------------------------------------------------------------------


class TestResourceHandlers:
    @pytest.fixture()
    def bridge(self):
        from workflow_builder.mcp_server.bridge import RuntimeBridge

        b = RuntimeBridge()
        b.register_workflow("wf1", description="Workflow 1")
        return b

    @pytest.mark.asyncio()
    async def test_workflow_list(self, bridge) -> None:
        from workflow_builder.mcp_server.resources import (
            handle_workflow_list,
        )

        result = await handle_workflow_list(bridge)
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "wf1"

    @pytest.mark.asyncio()
    async def test_workflow_detail(self, bridge) -> None:
        from workflow_builder.mcp_server.resources import (
            handle_workflow_detail,
        )

        result = await handle_workflow_detail(bridge, "wf1")
        parsed = json.loads(result)
        assert parsed["id"] == "wf1"

    @pytest.mark.asyncio()
    async def test_workflow_detail_not_found(
        self, bridge
    ) -> None:
        from workflow_builder.mcp_server.resources import (
            handle_workflow_detail,
        )

        result = await handle_workflow_detail(bridge, "nope")
        assert "not found" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio()
    async def test_run_detail(self, bridge) -> None:
        from workflow_builder.mcp_server.resources import (
            handle_run_detail,
        )

        run = await bridge.run_workflow("wf1", {})
        result = await handle_run_detail(
            bridge, run["run_id"]
        )
        parsed = json.loads(result)
        assert parsed["workflow_id"] == "wf1"

    @pytest.mark.asyncio()
    async def test_run_journal(self, bridge) -> None:
        from workflow_builder.mcp_server.resources import (
            handle_run_journal,
        )

        run = await bridge.run_workflow("wf1", {})
        result = await handle_run_journal(
            bridge, run["run_id"]
        )
        parsed = json.loads(result)
        assert isinstance(parsed, list)


# ---------------------------------------------------------------------------
# Prompt Builders
# ---------------------------------------------------------------------------


class TestPromptBuilders:
    def test_create_workflow_prompt(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_create_workflow_prompt,
        )

        result = build_create_workflow_prompt(
            "Sync CRM data from Airtable"
        )
        assert "Sync CRM data" in result
        assert "@workflow" in result
        assert "@step" in result

    def test_debug_run_prompt(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_debug_run_prompt,
        )

        result = build_debug_run_prompt(
            {"status": "failed", "error": "timeout"},
            [{"kind": "step", "step_id": "s1"}],
        )
        assert "failed" in result
        assert "timeout" in result

    def test_explain_workflow_prompt(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_explain_workflow_prompt,
        )

        result = build_explain_workflow_prompt(
            "crm_sync",
            {"id": "crm_sync", "description": "Sync"},
        )
        assert "crm_sync" in result

    def test_explain_workflow_not_found(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_explain_workflow_prompt,
        )

        result = build_explain_workflow_prompt("x", None)
        assert "Not found" in result

    def test_optimize_prompt(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_optimize_prompt,
        )

        result = build_optimize_prompt("my_flow")
        assert "my_flow" in result
        assert "parallel" in result

    def test_review_prompt(self) -> None:
        from workflow_builder.mcp_server.prompts import (
            build_review_prompt,
        )

        code = "@workflow\nasync def run(ctx): pass"
        result = build_review_prompt(code)
        assert code in result
        assert "correctness" in result.lower()


# ---------------------------------------------------------------------------
# Server Factory
# ---------------------------------------------------------------------------


class TestServerFactory:
    def test_create_server_importable(self) -> None:
        """The create_server function is importable."""
        from workflow_builder.mcp_server import create_server

        assert callable(create_server)
