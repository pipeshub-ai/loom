"""Tests for the coding agent's ReAct tools."""

from __future__ import annotations

import json

import pytest

from loom.toolsets.jira.manifest import JIRA_MANIFEST
from loom.toolsets.registry import register_toolset


@pytest.fixture
def jira_registered() -> None:
    """Register the Jira manifest for the duration of one test.

    ``isolated_catalog`` (autouse, in conftest) restores the global catalog
    afterwards, so no explicit unregister is needed.
    """
    register_toolset(JIRA_MANIFEST)


class TestSearchToolsets:
    async def test_finds_registered_jira(self, jira_registered: None) -> None:
        from loom.agents.coding_tools import search_toolsets

        cards = json.loads(await search_toolsets.fn("jira"))
        assert len(cards) >= 1
        assert cards[0]["toolset_id"] == "jira"

    async def test_returns_empty_for_unknown(self) -> None:
        from loom.agents.coding_tools import search_toolsets

        cards = json.loads(await search_toolsets.fn("nonexistent_xyz_toolset_99"))
        assert cards == []


class TestShowToolset:
    async def test_shows_jira_ops(self, jira_registered: None) -> None:
        from loom.agents.coding_tools import show_toolset

        data = json.loads(await show_toolset.fn("jira"))
        assert data["toolset_id"] == "jira"
        assert len(data["ops"]) >= 7

    async def test_error_for_unknown(self) -> None:
        from loom.agents.coding_tools import show_toolset

        data = json.loads(await show_toolset.fn("nonexistent"))
        assert "error" in data


class TestGetToolContract:
    async def test_gets_jira_search_contract(self, jira_registered: None) -> None:
        from loom.agents.coding_tools import get_tool_contract

        data = json.loads(await get_tool_contract.fn("jira.issues.search"))
        assert data["op_id"] == "issues.search"
        assert data["effect"] == "read"
        assert "input_schema" in data

    async def test_error_for_invalid_path(self) -> None:
        from loom.agents.coding_tools import get_tool_contract

        data = json.loads(await get_tool_contract.fn("bad_path"))
        assert "error" in data


class TestGetToolDocs:
    async def test_returns_jira_docs(self) -> None:
        from loom.agents.coding_tools import get_tool_docs

        result = await get_tool_docs.fn("jira")
        assert "jira_search_issues" in result
        assert "jira_create_issue" in result
        assert "JiraIssue" in result

    async def test_error_for_unknown_toolset(self) -> None:
        from loom.agents.coding_tools import get_tool_docs

        data = json.loads(await get_tool_docs.fn("unknown_toolset"))
        assert "error" in data


class TestValidateCode:
    async def test_valid_code(self) -> None:
        from loom.agents.coding_tools import validate_code

        code = '''\
from loom import Context, step, workflow

@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"

@workflow(name="hello")
async def hello(ctx: Context, name: str) -> str:
    return await ctx.step(greet, name)
'''
        assert "Valid" in await validate_code.fn(code)

    async def test_invalid_code_syntax(self) -> None:
        from loom.agents.coding_tools import validate_code

        issues = json.loads(await validate_code.fn("def broken("))
        assert len(issues) > 0
        assert issues[0]["severity"] == "error"
        assert issues[0]["category"] == "syntax"

    async def test_missing_workflow(self) -> None:
        from loom.agents.coding_tools import validate_code

        code = '''\
from loom import step

@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"
'''
        issues = json.loads(await validate_code.fn(code))
        assert any(i["category"] == "structure" for i in issues)

    async def test_flags_disallowed_import(self) -> None:
        from loom.agents.coding_tools import build_coding_tools
        from loom.agents.validator import CodeValidator

        code = '''\
import json

import pandas as pd

from loom import Context, step, workflow

@step
async def load(path: str) -> int:
    return len(pd.read_csv(path)) + len(json.dumps({}))

@workflow(name="load")
async def load_wf(ctx: Context, path: str) -> int:
    return await ctx.step(load, path)
'''
        tools = build_coding_tools(validator=CodeValidator(allowed_packages={"httpx"}))
        validate = next(t for t in tools if t.name == "validate_code")

        issues = json.loads(await validate.fn(code))
        flagged = [i for i in issues if i["category"] == "imports"]
        assert any("pandas" in i["message"] for i in flagged)
        # stdlib and the SDK itself are never flagged
        assert not any("json" in i["message"] for i in flagged)
        assert not any("loom" in i["message"] for i in flagged)


class TestBuildCodingTools:
    def test_returns_the_authoring_tools(self) -> None:
        from loom.agents.coding_tools import build_coding_tools

        tools = build_coding_tools()
        names = {t.name for t in tools}
        # The toolset tier, the node tier, and the validator. Asserted by name
        # rather than by count: a count says nothing about which tool went
        # missing, and the whole set is the agent's visible surface.
        assert names == {
            "search_toolsets",
            "show_toolset",
            "get_tool_contract",
            "get_tool_docs",
            "call_read_operation",
            "search_nodes",
            "show_node",
            "node_contract",
            "validate_code",
        }
        assert "ask_user" not in names

    def test_ask_user_is_included_only_when_interaction_is_set(self) -> None:
        from loom.agents.coding_tools import build_coding_tools
        from loom.agents.interaction import CallbackUserInteraction, UserResponse

        tools = build_coding_tools(
            interaction=CallbackUserInteraction(lambda q: UserResponse(answer="x"))
        )
        assert "ask_user" in {t.name for t in tools}
        assert len(tools) == len(build_coding_tools()) + 1

    def test_all_have_descriptions(self) -> None:
        from loom.agents.coding_tools import build_coding_tools

        for t in build_coding_tools():
            assert t.description, f"Tool {t.name} missing description"
