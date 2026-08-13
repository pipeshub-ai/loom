"""Tests for Phase 9 — MCP Server.

Covers: RuntimeBridge, tool handlers, resource handlers,
prompt builders, server factory.
"""

from __future__ import annotations

import pytest

from workflow_builder import Context, step, workflow

# ---------------------------------------------------------------------------
# RuntimeBridge
# ---------------------------------------------------------------------------


@step
async def enrich(payload: dict) -> dict:
    """Pretend to enrich a lead."""
    return {"enriched": True, **payload}


@workflow(name="lead_outreach")
async def lead_outreach(ctx: Context, payload: dict) -> dict:
    """Lead enrichment and outreach."""
    return await ctx.step(enrich, payload or {})


@workflow(name="other")
async def other_workflow(ctx: Context, payload: dict) -> str:
    """A second workflow, for filter tests."""
    await ctx.step(enrich, payload or {})
    return "ok"


# Bound to a non-test_ name so pytest does not try to collect it as a test.
@workflow(name="test_wf")
async def sample_wf(ctx: Context, payload: dict) -> dict:
    """A test workflow."""
    return await ctx.step(enrich, payload or {})


@workflow(name="wf1")
async def wf1(ctx: Context, payload: dict) -> dict:
    """Workflow 1."""
    return await ctx.step(enrich, payload or {})


@workflow(name="awaits_approval")
async def awaits_approval(ctx: Context, payload: dict) -> str:
    """Parks until someone approves, so there is a live run to act on."""
    approved = await ctx.wait_for_approval("release")
    return "approved" if approved else "rejected"


class TestRuntimeBridge:
    @pytest.fixture()
    def bridge(self):
        from workflow_builder.mcp_server.bridge import RuntimeBridge

        b = RuntimeBridge(store_url="memory://")
        b.register_workflow(
            lead_outreach,
            description="Lead enrichment and outreach",
            input_schema={"type": "object"},
        )
        return b

    @pytest.mark.asyncio()
    async def test_bridge_drives_the_real_runtime(self, bridge) -> None:
        """The bridge reports what actually executed, not a canned shape."""
        result = await bridge.run_workflow("lead_outreach", {"source": "test"})

        # The output is what the step returned...
        assert result["output"] == {"enriched": True, "source": "test"}
        # ...and the same run is visible through the Runtime itself.
        record = await bridge.runtime.get(result["run_id"])
        assert record is not None
        assert record.workflow == "lead_outreach"

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
        bridge.register_workflow(other_workflow, description="Other")
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
        # Cancel a run that is actually in flight — a suspended one waiting on
        # an approval. Cancelling a finished run is a no-op by design.
        bridge.register_workflow(awaits_approval)
        run = await bridge.run_workflow("awaits_approval", {})
        assert run["status"] == "suspended"

        result = await bridge.cancel_run(run["run_id"])
        assert result["status"] == "cancelled"
        status = await bridge.get_run_status(run["run_id"])
        assert status["status"] == "cancelled"

    @pytest.mark.asyncio()
    async def test_cancel_completed_run_leaves_it_completed(self, bridge) -> None:
        run = await bridge.run_workflow("lead_outreach", {})
        await bridge.cancel_run(run["run_id"])
        status = await bridge.get_run_status(run["run_id"])
        assert status["status"] == "completed"

    @pytest.mark.asyncio()
    async def test_send_event_resumes_a_parked_run(self, bridge) -> None:
        bridge.register_workflow(awaits_approval)
        run = await bridge.run_workflow("awaits_approval", {})
        assert run["status"] == "suspended"

        await bridge.send_event(
            run["run_id"], "approval:release", {"approved": True}
        )
        resumed = await bridge.resume_run(run["run_id"])
        assert resumed["status"] == "completed"

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


# Tool and resource handler tests moved to tests/test_mcp_server.py when the
# handlers were retargeted from RuntimeBridge onto the shared RuntimeFacade.
# That file covers the same behaviour and more, against the real FastMCP server
# and a live stdio subprocess.


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
