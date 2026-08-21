"""Prompt builders and the server factory.

The tool and resource handlers moved to ``tests/test_mcp_server.py`` when they
were retargeted from the old ``RuntimeBridge`` onto the shared
``RuntimeFacade``; that file covers them against a real MCP server and a live
stdio subprocess. The bridge itself is gone, and so are the 176 lines here
that were its only caller.
"""

from __future__ import annotations


class TestPromptBuilders:
    def test_create_workflow_prompt(self) -> None:
        from loom.mcp_server.prompts import (
            build_create_workflow_prompt,
        )

        result = build_create_workflow_prompt(
            "Sync CRM data from Airtable"
        )
        assert "Sync CRM data" in result
        assert "@workflow" in result
        assert "@step" in result

    def test_debug_run_prompt(self) -> None:
        from loom.mcp_server.prompts import (
            build_debug_run_prompt,
        )

        result = build_debug_run_prompt(
            {"status": "failed", "error": "timeout"},
            [{"kind": "step", "step_id": "s1"}],
        )
        assert "failed" in result
        assert "timeout" in result

    def test_explain_workflow_prompt(self) -> None:
        from loom.mcp_server.prompts import (
            build_explain_workflow_prompt,
        )

        result = build_explain_workflow_prompt(
            "crm_sync",
            {"id": "crm_sync", "description": "Sync"},
        )
        assert "crm_sync" in result

    def test_explain_workflow_not_found(self) -> None:
        from loom.mcp_server.prompts import (
            build_explain_workflow_prompt,
        )

        result = build_explain_workflow_prompt("x", None)
        assert "Not found" in result

    def test_optimize_prompt(self) -> None:
        from loom.mcp_server.prompts import (
            build_optimize_prompt,
        )

        result = build_optimize_prompt("my_flow")
        assert "my_flow" in result
        assert "parallel" in result

    def test_review_prompt(self) -> None:
        from loom.mcp_server.prompts import (
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
        from loom.mcp_server import create_server

        assert callable(create_server)
