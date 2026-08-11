"""Tests for the coding agent's ReAct tools."""

from __future__ import annotations

import json


class TestSearchToolsets:
    def test_finds_registered_jira(self) -> None:
        from workflow_builder.agents.coding_tools import search_toolsets
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST
        from workflow_builder.toolsets.registry import (
            register_toolset,
            unregister_toolset,
        )

        register_toolset(JIRA_MANIFEST)
        try:
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                search_toolsets.fn("jira")
            )
            cards = json.loads(result)
            assert len(cards) >= 1
            assert cards[0]["toolset_id"] == "jira"
        finally:
            unregister_toolset("jira")

    def test_returns_empty_for_unknown(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import search_toolsets
        result = asyncio.get_event_loop().run_until_complete(
            search_toolsets.fn("nonexistent_xyz_toolset_99")
        )
        cards = json.loads(result)
        assert cards == []


class TestShowToolset:
    def test_shows_jira_ops(self) -> None:
        from workflow_builder.agents.coding_tools import show_toolset
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST
        from workflow_builder.toolsets.registry import (
            register_toolset,
            unregister_toolset,
        )

        register_toolset(JIRA_MANIFEST)
        try:
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                show_toolset.fn("jira")
            )
            data = json.loads(result)
            assert data["toolset_id"] == "jira"
            assert len(data["ops"]) >= 7
        finally:
            unregister_toolset("jira")

    def test_error_for_unknown(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import show_toolset
        result = asyncio.get_event_loop().run_until_complete(
            show_toolset.fn("nonexistent")
        )
        data = json.loads(result)
        assert "error" in data


class TestGetToolContract:
    def test_gets_jira_search_contract(self) -> None:
        from workflow_builder.agents.coding_tools import get_tool_contract
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST
        from workflow_builder.toolsets.registry import (
            register_toolset,
            unregister_toolset,
        )

        register_toolset(JIRA_MANIFEST)
        try:
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                get_tool_contract.fn("jira.issues.search")
            )
            data = json.loads(result)
            assert data["op_id"] == "issues.search"
            assert data["effect"] == "read"
            assert "input_schema" in data
        finally:
            unregister_toolset("jira")

    def test_error_for_invalid_path(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import get_tool_contract
        result = asyncio.get_event_loop().run_until_complete(
            get_tool_contract.fn("bad_path")
        )
        data = json.loads(result)
        assert "error" in data


class TestGetToolDocs:
    def test_returns_jira_docs(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import get_tool_docs
        result = asyncio.get_event_loop().run_until_complete(
            get_tool_docs.fn("jira")
        )
        assert "jira_search_issues" in result
        assert "jira_create_issue" in result
        assert "JiraIssue" in result

    def test_error_for_unknown_toolset(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import get_tool_docs
        result = asyncio.get_event_loop().run_until_complete(
            get_tool_docs.fn("unknown_toolset")
        )
        data = json.loads(result)
        assert "error" in data


class TestValidateCode:
    def test_valid_code(self) -> None:
        from workflow_builder.agents.coding_tools import validate_code

        code = '''\
from workflow_builder import Context, step, workflow

@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"

@workflow(name="hello")
async def hello(ctx: Context, name: str) -> str:
    return await ctx.step(greet, name)
'''
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            validate_code.fn(code)
        )
        assert "Valid" in result

    def test_invalid_code_syntax(self) -> None:
        import asyncio

        from workflow_builder.agents.coding_tools import validate_code
        result = asyncio.get_event_loop().run_until_complete(
            validate_code.fn("def broken(")
        )
        issues = json.loads(result)
        assert len(issues) > 0
        assert issues[0]["severity"] == "error"
        assert issues[0]["category"] == "syntax"

    def test_missing_workflow(self) -> None:
        from workflow_builder.agents.coding_tools import validate_code

        code = '''\
from workflow_builder import step

@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"
'''
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            validate_code.fn(code)
        )
        issues = json.loads(result)
        assert any(i["category"] == "structure" for i in issues)


class TestBuildCodingTools:
    def test_returns_five_tools(self) -> None:
        from workflow_builder.agents.coding_tools import build_coding_tools

        tools = build_coding_tools()
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert "search_toolsets" in names
        assert "show_toolset" in names
        assert "get_tool_contract" in names
        assert "get_tool_docs" in names
        assert "validate_code" in names

    def test_all_have_descriptions(self) -> None:
        from workflow_builder.agents.coding_tools import build_coding_tools

        for t in build_coding_tools():
            assert t.description, f"Tool {t.name} missing description"
