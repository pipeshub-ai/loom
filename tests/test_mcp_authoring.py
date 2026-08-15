"""Tests for the MCP authoring tools.

Three levels, mirroring ``test_mcp_server.py``:

**Unit** — each ``authoring.py`` coroutine directly, no ``mcp`` import.
**Integration** — registered on a real ``FastMCP`` instance: counts, schema
budget, annotations, and the gate that turns them off.
**End to end** — the full discover -> validate -> smoke -> save loop, using
the tmp filesystem so nothing touches the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.toolsets.jira.manifest import JIRA_MANIFEST
from loom.toolsets.registry import register_toolset

pytest.importorskip("mcp", reason="needs the mcp extra")

CLEAN_WORKFLOW = '''\
from loom import Context, step, workflow


@step
async def double(n: int) -> int:
    """Double a number."""
    return n * 2


@workflow(name="doubler", description="Double the input")
async def doubler(ctx: Context, n: int) -> int:
    """Double it."""
    return await ctx.step(double, n)
'''


def parsed(raw: str) -> dict:
    """Every authoring tool returns JSON text; this asserts that and decodes it."""
    return json.loads(raw)


@pytest.fixture
def jira_registered() -> None:
    """Register Jira for the duration of one test.

    ``isolated_catalog`` (autouse, in conftest) restores the global catalog
    afterwards.
    """
    register_toolset(JIRA_MANIFEST)


# ---------------------------------------------------------------------------
# Unit — coroutines, no MCP
# ---------------------------------------------------------------------------


class TestGetToolContract:
    async def test_returns_schema_and_import_line(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.get_tool_contract("jira.issues.search"))

        assert result["op_id"] == "issues.search"
        assert result["effect"] == "read"
        assert result["toolset_id"] == "jira"
        assert "input_schema" in result and "output_schema" in result
        assert "jira_search_issues" in result["import_line"]
        assert result["import_line"].startswith("from loom.toolsets.jira.tools import")

    async def test_unknown_toolset_returns_error_not_raise(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.get_tool_contract("nonexistent.op"))
        assert "error" in result

    async def test_malformed_path_returns_error_not_raise(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.get_tool_contract("no_dot_here"))
        assert "error" in result
        assert "toolset_id.op_id" in result["error"]

    async def test_unknown_operation_on_known_toolset(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.get_tool_contract("jira.nope.nope"))
        assert "error" in result


class TestGetToolDocs:
    async def test_returns_a_string_with_import_lines(self) -> None:
        from loom.mcp_server import authoring

        result = await authoring.get_tool_docs("jira")
        assert "jira_search_issues" in result or "from loom" in result

    async def test_unknown_toolset_lists_available(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.get_tool_docs("nonexistent_xyz"))
        assert "error" in result
        assert "jira" in result["available"]


class TestCallReadOperation:
    async def test_refuses_write_operation(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.call_read_operation("jira.issues.create", "{}"))
        assert "error" in result
        assert "write" in result["error"]

    async def test_invalid_json_returns_error(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.call_read_operation("jira.issues.search", "{not json"))
        assert "error" in result
        assert "JSON" in result["error"]

    async def test_unknown_op_returns_error(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.call_read_operation("jira.nope.nope", "{}"))
        assert "error" in result

    async def test_repeat_limit_refuses_the_third_identical_call(
        self, jira_registered: None
    ) -> None:
        from loom.mcp_server import authoring

        seen: dict[str, int] = {}
        args = json.dumps({"jql": "project = X"})
        for _ in range(2):
            result = parsed(
                await authoring.call_read_operation("jira.issues.search", args, seen=seen)
            )
            # First two calls fail for lack of credentials, but that is a
            # different error than the repeat-limit one under test.
            assert "already called" not in result.get("error", "")

        third = parsed(
            await authoring.call_read_operation("jira.issues.search", args, seen=seen)
        )
        assert "already called" in third["error"]

    async def test_response_is_capped(self, jira_registered: None) -> None:
        from loom.agents import coding_tools
        from loom.mcp_server import authoring

        async def _huge(**kwargs: object) -> list[dict[str, str]]:
            return [{"key": "x" * 100} for _ in range(2000)]

        original = coding_tools._call_read_operation

        async def _patched(op_path, arguments, *, registry, seen=None):
            # Bypass the real HTTP call but exercise the same size cap that
            # authoring.call_read_operation applies to the JSON it gets back.
            import json as _json

            return _json.dumps({"result": await _huge()})[:100_000]

        coding_tools._call_read_operation = _patched
        try:
            result = await authoring.call_read_operation("jira.issues.search", "{}")
        finally:
            coding_tools._call_read_operation = original

        assert len(result) <= authoring.MAX_RESPONSE_CHARS


class TestValidateWorkflowCode:
    async def test_clean_code_is_valid(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.validate_workflow_code(CLEAN_WORKFLOW))
        assert result["valid"] is True
        assert result["issues"] == []

    async def test_syntax_error_is_invalid(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.validate_workflow_code("def ("))
        assert result["valid"] is False
        assert result["issues"][0]["category"] == "syntax"

    async def test_missing_workflow_decorator_is_flagged(self) -> None:
        from loom.mcp_server import authoring

        code = "from loom import step\n\n@step\nasync def f() -> int:\n    return 1\n"
        result = parsed(await authoring.validate_workflow_code(code))
        assert result["valid"] is False
        assert any(i["category"] == "structure" for i in result["issues"])

    async def test_nondeterminism_in_workflow_body_is_flagged(self) -> None:
        from loom.mcp_server import authoring

        code = (
            "from datetime import datetime\n"
            "from loom import Context, workflow\n\n"
            '@workflow(name="bad")\n'
            "async def bad(ctx: Context, x: str) -> str:\n"
            "    return str(datetime.now())\n"
        )
        result = parsed(await authoring.validate_workflow_code(code))
        assert any(i["category"] == "determinism" for i in result["issues"])

    async def test_disallowed_import_is_flagged(self) -> None:
        from loom.mcp_server import authoring

        code = CLEAN_WORKFLOW + "\nimport totally_not_a_real_package\n"
        result = parsed(
            await authoring.validate_workflow_code(code, allowed_packages="httpx")
        )
        assert any(i["category"] == "imports" for i in result["issues"])

    async def test_registered_toolset_import_is_allowed(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, step, workflow\n"
            "from loom.toolsets.jira.tools import jira_search_issues\n\n"
            "@step\n"
            "async def search(q: str) -> list:\n"
            "    return await jira_search_issues(q)\n\n"
            '@workflow(name="search_flow")\n'
            "async def search_flow(ctx: Context, q: str) -> list:\n"
            "    return await ctx.step(search, q)\n"
        )
        result = parsed(await authoring.validate_workflow_code(code))
        assert not any(i["category"] == "toolset" for i in result["issues"])

    async def test_unregistered_toolset_import_is_flagged(self) -> None:
        """``register_available_toolsets()`` seeds every shipped toolset, so
        this needs one that genuinely is not registered anywhere — an
        invented module path rather than a real toolset like Jira."""
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, workflow\n"
            "from loom.toolsets.not_a_real_toolset.tools import made_up\n\n"
            '@workflow(name="x")\n'
            "async def x(ctx: Context, q: str) -> list:\n"
            "    return []\n"
        )
        result = parsed(await authoring.validate_workflow_code(code))
        assert any(i["category"] == "toolset" for i in result["issues"])


class TestSmokeTestWorkflow:
    async def test_passes_a_clean_workflow(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.smoke_test_workflow(CLEAN_WORKFLOW, "5"))
        assert result["ok"] is True
        assert result["phase"] == "done"
        assert result["workflows_found"] == ["doubler"]
        assert result["status"] == "completed"
        assert result["environmental"] is False

    async def test_catches_a_step_that_raises(self) -> None:
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, step, workflow\n\n"
            "@step\n"
            "async def boom() -> str:\n"
            "    raise RuntimeError('nope')\n\n"
            '@workflow(name="breaker")\n'
            "async def breaker(ctx: Context, x: str) -> str:\n"
            "    return await ctx.step(boom)\n"
        )
        result = parsed(await authoring.smoke_test_workflow(code, '"x"'))
        assert result["ok"] is False

    async def test_invalid_input_json_is_an_error(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.smoke_test_workflow(CLEAN_WORKFLOW, "{not json"))
        assert "error" in result

    async def test_uses_fakes_for_registered_toolsets(self, jira_registered: None) -> None:
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, step, workflow\n"
            "from loom.toolsets.jira.tools import jira_list_projects\n\n"
            "@step\n"
            "async def list_projects() -> list:\n"
            "    return await jira_list_projects()\n\n"
            '@workflow(name="jira_flow")\n'
            "async def jira_flow(ctx: Context, x: str) -> list:\n"
            "    return await ctx.step(list_projects)\n"
        )
        result = parsed(await authoring.smoke_test_workflow(code, '"x"'))
        # A real call with no credentials would fail on a 401; a faked one
        # succeeds against schema-generated stand-ins.
        assert result["ok"] is True, result


class TestSaveWorkflow:
    async def test_writes_the_file_and_finds_the_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import authoring

        monkeypatch.chdir(tmp_path)
        result = parsed(await authoring.save_workflow(CLEAN_WORKFLOW, "flows/doubler.py"))

        assert result["saved"] is True
        assert result["workflows_found"] == ["doubler"]
        assert (tmp_path / "flows" / "doubler.py").read_text() == CLEAN_WORKFLOW

    async def test_refuses_absolute_path(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.save_workflow(CLEAN_WORKFLOW, "/etc/evil.py"))
        assert "error" in result

    async def test_refuses_directory_traversal(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.save_workflow(CLEAN_WORKFLOW, "../evil.py"))
        assert "error" in result

    async def test_refuses_non_python_extension(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.save_workflow(CLEAN_WORKFLOW, "evil.js"))
        assert "error" in result

    async def test_refuses_code_that_does_not_compile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import authoring

        monkeypatch.chdir(tmp_path)
        result = parsed(await authoring.save_workflow("def (", "flows/bad.py"))

        assert result["saved"] is False
        assert not (tmp_path / "flows" / "bad.py").exists()

    async def test_creates_missing_parent_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import authoring

        monkeypatch.chdir(tmp_path)
        result = parsed(
            await authoring.save_workflow(CLEAN_WORKFLOW, "a/b/c/doubler.py")
        )
        assert result["saved"] is True
        assert (tmp_path / "a" / "b" / "c" / "doubler.py").exists()


# ---------------------------------------------------------------------------
# Integration — registered on a real FastMCP server
# ---------------------------------------------------------------------------


@pytest.fixture
def authoring_facade():
    from loom.facade import LocalFacade
    from loom.runtime.engine import Runtime
    from loom.stores.memory import MemoryStore

    return LocalFacade(Runtime(store=MemoryStore()))


@pytest.fixture
def authoring_server(authoring_facade):
    from loom.mcp_server import build_server

    return build_server(authoring_facade, name="loom-authoring-test")


class TestServerRegistration:
    async def test_all_22_tools_are_registered_when_enabled(self, authoring_server) -> None:
        names = {t.name for t in await authoring_server.list_tools()}
        authoring_names = {
            "get_tool_contract",
            "get_tool_docs",
            "call_read_operation",
            "validate_workflow_code",
            "smoke_test_workflow",
            "save_workflow",
        }
        assert authoring_names <= names
        assert len(names) == 22

    async def test_disabled_via_config_drops_to_16(self, authoring_facade) -> None:
        from loom.mcp_server import build_server
        from loom.mcp_server.authoring_config import AuthoringConfig

        server = build_server(
            authoring_facade, name="off", authoring=AuthoringConfig(enabled=False)
        )
        names = {t.name for t in await server.list_tools()}
        assert len(names) == 16
        assert "get_tool_contract" not in names
        assert "save_workflow" not in names

    async def test_disabled_via_env_var(
        self, authoring_facade, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import build_server

        monkeypatch.setenv("LOOM_MCP_AUTHORING", "0")
        server = build_server(authoring_facade, name="off-env")
        names = {t.name for t in await server.list_tools()}
        assert len(names) == 16

    async def test_every_authoring_tool_has_annotations(self, authoring_server) -> None:
        authoring_names = {
            "get_tool_contract",
            "get_tool_docs",
            "call_read_operation",
            "validate_workflow_code",
            "smoke_test_workflow",
            "save_workflow",
        }
        for tool in await authoring_server.list_tools():
            if tool.name in authoring_names:
                assert tool.annotations is not None, tool.name

    async def test_read_only_tools_are_marked_read_only(self, authoring_server) -> None:
        by_name = {t.name: t for t in await authoring_server.list_tools()}
        for name in ("get_tool_contract", "get_tool_docs", "validate_workflow_code"):
            assert by_name[name].annotations.readOnlyHint is True

    async def test_call_read_operation_is_not_marked_read_only(self, authoring_server) -> None:
        """It performs a real call against a live service — not the same
        thing as reading this server's own state."""
        by_name = {t.name: t for t in await authoring_server.list_tools()}
        assert by_name["call_read_operation"].annotations.readOnlyHint is False

    async def test_save_workflow_is_not_marked_destructive(self, authoring_server) -> None:
        """It writes a new file; it does not delete or overwrite state a
        user did not just hand it."""
        by_name = {t.name: t for t in await authoring_server.list_tools()}
        assert by_name["save_workflow"].annotations.destructiveHint is False

    async def test_schema_budget_stays_within_the_raised_limit(self, authoring_server) -> None:
        registered = await authoring_server.list_tools()
        total = sum(
            len(t.name) + len(t.description or "") + len(json.dumps(t.inputSchema))
            for t in registered
        )
        assert total <= 18_000, f"22 tools' schemas total {total} chars"

    async def test_instructions_mention_authoring_when_enabled(self, authoring_server) -> None:
        assert "save_workflow" in (authoring_server.instructions or "")

    async def test_instructions_omit_authoring_when_disabled(self, authoring_facade) -> None:
        from loom.mcp_server import build_server
        from loom.mcp_server.authoring_config import AuthoringConfig

        server = build_server(
            authoring_facade, name="off2", authoring=AuthoringConfig(enabled=False)
        )
        assert "save_workflow" not in (server.instructions or "")

    async def test_create_workflow_prompt_mentions_the_ladder_when_enabled(
        self, authoring_server
    ) -> None:
        result = await authoring_server.get_prompt(
            "create_workflow", {"description": "sync CRM leads"}
        )
        text = str(result.messages[0].content)
        assert "validate_workflow_code" in text
        assert "smoke_test_workflow" in text
        assert "sync CRM leads" in text


class TestValidateThenSmokeChain:
    """The loop a host model actually drives: write, validate, fix, smoke."""

    async def test_validate_then_smoke_on_the_same_code(self, authoring_server) -> None:
        validated = await authoring_server.call_tool(
            "validate_workflow_code", {"code": CLEAN_WORKFLOW}
        )
        assert parsed(_text_of(validated))["valid"] is True

        smoked = await authoring_server.call_tool(
            "smoke_test_workflow", {"code": CLEAN_WORKFLOW, "workflow_input_json": "3"}
        )
        assert parsed(_text_of(smoked))["ok"] is True


def _text_of(result) -> str:
    """The text payload of a tool result, across SDK return shapes."""
    blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, list | tuple):
        return blocks[0].text
    return str(blocks)


# ---------------------------------------------------------------------------
# End to end — the full authoring loop against the tmp filesystem
# ---------------------------------------------------------------------------


class TestFullAuthoringLoop:
    async def test_search_contract_validate_smoke_save(
        self, jira_registered: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import authoring, tools

        monkeypatch.chdir(tmp_path)

        found = parsed(await tools.search_toolsets("jira"))
        assert "jira" in {c["toolset_id"] for c in found["toolsets"]}

        contract = parsed(await authoring.get_tool_contract("jira.projects.list"))
        assert contract["effect"] == "read"

        code = (
            "from loom import Context, step, workflow\n"
            "from loom.toolsets.jira.tools import jira_list_projects\n\n"
            "@step\n"
            "async def list_projects() -> list:\n"
            "    return await jira_list_projects()\n\n"
            '@workflow(name="list_saas_projects")\n'
            "async def list_saas_projects(ctx: Context, x: str) -> list:\n"
            "    return await ctx.step(list_projects)\n"
        )

        validated = parsed(await authoring.validate_workflow_code(code))
        assert validated["valid"] is True, validated["issues"]

        smoked = parsed(await authoring.smoke_test_workflow(code, '"go"'))
        assert smoked["ok"] is True, smoked

        saved = parsed(await authoring.save_workflow(code, "flows/list_saas_projects.py"))
        assert saved["saved"] is True
        assert saved["workflows_found"] == ["list_saas_projects"]
