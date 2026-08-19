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

#: A cap on a fetch, and nothing anywhere asking whether it was enough. Passes
#: compile, static, lint and mypy; only ``coverage`` — given the spec — has
#: anything to say about it.
CAPPED_WORKFLOW = '''\
from loom import Context, step, workflow


@step
async def fetch(query: str) -> list:
    """Fetch matching rows."""
    return await search_rows(query, max_results=100)


async def search_rows(query: str, max_results: int = 50) -> list:
    """Stand-in for a paged toolset read."""
    return []


@workflow(name="reporter", description="Report the rows")
async def reporter(ctx: Context, query: str) -> str:
    """Report them."""
    rows = await ctx.step(fetch, query)
    return f"{len(rows)} found"
'''

#: A word lifted straight from the spec into a fuzzy match operator. Nothing
#: resolved it to an id, so this returns whatever happens to contain the
#: substring — and reports no error when that is nothing.
FUZZY_WORKFLOW = '''\
from loom import Context, step, workflow


@step
async def search(jql: str) -> list:
    """Search issues."""
    return []


@workflow(name="owned_stories", description="Stories owned by someone")
async def owned_stories(ctx: Context, unused: str) -> list:
    """Find them."""
    return await ctx.step(search, 'assignee ~ "vishwjeet"')
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


class TestValidateRunsTheRealPipeline:
    """The stages a client gets, and what it is told about the ones it didn't.

    Before this the tool ran ``compile`` and a hand-rolled ``CodeValidator``
    call — two of the seven non-executing stages — so the five that a coding
    agent gets, including both stages written from observed failures, were
    unreachable over MCP by construction.
    """

    def _rows(self, result: dict) -> dict[str, dict]:
        return {row["name"]: row for row in result["stages"]}

    async def test_every_static_stage_is_reported(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(
            await authoring.validate_workflow_code(CLEAN_WORKFLOW, spec="double a number")
        )
        assert [row["name"] for row in result["stages"]] == list(
            authoring.STATIC_STAGE_NAMES
        )
        assert all(row["status"] == "ok" for row in result["stages"]), result["stages"]

    async def test_coverage_flags_a_cap_when_the_spec_asked_for_all(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(
            await authoring.validate_workflow_code(
                CAPPED_WORKFLOW, spec="report all the rows matching the query"
            )
        )
        assert any(i["category"] == "coverage" for i in result["issues"]), result
        assert self._rows(result)["coverage"]["status"] == "ok"
        # A warning, not a refusal: capping is a legitimate call, and making it
        # without noticing is the defect.
        assert result["valid"] is True

    async def test_the_same_cap_is_silent_without_a_completeness_word(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(
            await authoring.validate_workflow_code(
                CAPPED_WORKFLOW, spec="report the first page of rows"
            )
        )
        assert not any(i["category"] == "coverage" for i in result["issues"])

    async def test_resolution_flags_a_fuzzy_match_on_a_spec_word(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(
            await authoring.validate_workflow_code(
                FUZZY_WORKFLOW, spec="show the stories owned by Vishwjeet"
            )
        )
        found = [i for i in result["issues"] if i["category"] == "resolution"]
        assert found, result
        assert "vishwjeet" in found[0]["message"].lower()
        # Not valid, unlike the coverage warning above: a scope nothing looked
        # up returns whatever contains the substring, and the model asking for
        # the verdict is the one that can still go and resolve it. The issue
        # says how to decline if it already has.
        assert result["valid"] is False
        assert "return the code unchanged" in found[0]["message"]

    async def test_no_spec_still_validates_and_finds_no_intent_issues(self) -> None:
        """Backwards compatible: the parameter is optional, and its absence
        must not turn a clean file into an error."""
        from loom.mcp_server import authoring

        result = parsed(await authoring.validate_workflow_code(FUZZY_WORKFLOW))

        assert result["valid"] is True
        assert not any(
            i["category"] in ("coverage", "resolution") for i in result["issues"]
        )

    async def test_without_a_spec_the_intent_stages_report_skipped_not_ok(self) -> None:
        """The rule the whole payload shape exists for: a check that could not
        run has found nothing, and calling that a pass is how a client
        concludes its code cleared seven stages when it cleared five."""
        from loom.mcp_server import authoring

        rows = self._rows(parsed(await authoring.validate_workflow_code(CAPPED_WORKFLOW)))

        assert rows["coverage"]["status"] == "skipped"
        assert rows["resolution"]["status"] == "skipped"
        assert "spec" in rows["coverage"]["reason"]
        assert rows["static"]["status"] == "ok"

    async def test_a_missing_spec_is_called_out_in_a_note(self) -> None:
        from loom.mcp_server import authoring

        without = parsed(await authoring.validate_workflow_code(CLEAN_WORKFLOW))
        with_spec = parsed(
            await authoring.validate_workflow_code(CLEAN_WORKFLOW, spec="double it")
        )

        assert "spec" in without["note"]
        assert "note" not in with_spec

    async def test_a_skipped_stage_is_never_reported_as_passing(self) -> None:
        """Directly, because ruff and mypy are both installed here — the real
        skip path (``reason='ruff is not installed'``) cannot be reached in
        this environment, and mocking the subprocess would test the mock."""
        from loom.agents.checks import CheckResult, PipelineReport
        from loom.mcp_server import authoring

        report = PipelineReport(
            results=[
                CheckResult("compile"),
                CheckResult("static"),
                CheckResult("grants"),
                CheckResult("coverage"),
                CheckResult("resolution"),
                CheckResult("lint", skipped=True, reason="ruff is not installed"),
                CheckResult("types", skipped=True, reason="mypy is not installed"),
            ]
        )
        rows = {r["name"]: r for r in authoring._stage_rows(report, "some spec")}

        assert rows["lint"] == {
            "name": "lint",
            "status": "skipped",
            "reason": "ruff is not installed",
        }
        assert rows["types"]["status"] == "skipped"
        assert rows["compile"]["status"] == "ok"

    async def test_a_grant_stage_skip_still_carries_its_reason(self) -> None:
        """``GrantStage`` states its reason in ``skipped=`` rather than
        ``reason=``; a skip with no stated reason reads as a pass to anyone
        skimming the rows."""
        from loom.agents.checks import CheckResult, PipelineReport
        from loom.mcp_server import authoring

        report = PipelineReport(
            results=[CheckResult("grants", skipped="no toolset registry to check against")]
        )
        rows = {r["name"]: r for r in authoring._stage_rows(report, "")}

        assert rows["grants"]["status"] == "skipped"
        assert rows["grants"]["reason"] == "no toolset registry to check against"

    async def test_stages_after_a_blocking_failure_are_not_run_not_ok(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.validate_workflow_code("def (", spec="anything"))
        rows = self._rows(result)

        assert result["valid"] is False
        assert rows["compile"]["status"] == "failed"
        assert rows["lint"]["status"] == "not_run"
        assert rows["types"]["status"] == "not_run"

    async def test_the_grant_stage_flags_a_toolset_that_does_not_exist(self) -> None:
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, workflow\n"
            "from loom.security.grants import GrantSet\n\n"
            '@workflow(name="granted", grants=GrantSet(toolsets=["not_a_toolset"]))\n'
            "async def granted(ctx: Context, x: str) -> str:\n"
            "    return x\n"
        )
        result = parsed(await authoring.validate_workflow_code(code, spec="do a thing"))
        assert any(i["category"] == "grants" for i in result["issues"]), result


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


class TestSmokeReportsReplaySeparately:
    """Running once cannot see nondeterminism, and that is the defect the
    engine's own re-entry turns into a wrong answer rather than a crash. It is
    reported beside ``ok`` rather than folded into it, because "it ran" and
    "it ran the same way twice" are different questions."""

    async def test_a_deterministic_workflow_replays_ok(self) -> None:
        from loom.mcp_server import authoring

        result = parsed(await authoring.smoke_test_workflow(CLEAN_WORKFLOW, "5"))

        assert result["ok"] is True
        assert result["replay"]["status"] == "ok"

    async def test_replay_is_skipped_when_the_run_did_not_complete(self) -> None:
        """Not reported ``ok``: two runs that both failed have compared
        nothing, and a determinism claim nobody checked is worse than none."""
        from loom.mcp_server import authoring

        code = (
            "from loom import Context, step, workflow\n\n"
            "@step\n"
            "async def boom() -> str:\n"
            "    raise RuntimeError('nope')\n\n"
            '@workflow(name="breaker2")\n'
            "async def breaker2(ctx: Context, x: str) -> str:\n"
            "    return await ctx.step(boom)\n"
        )
        result = parsed(await authoring.smoke_test_workflow(code, '"x"'))

        assert result["ok"] is False
        assert result["replay"]["status"] == "skipped"
        assert result["replay"]["reason"]

    async def test_a_nondeterministic_body_is_caught(self) -> None:
        """``random`` is imported inside the step, so the AST determinism rule
        — which only reads the workflow body — has nothing to say. Only
        running it twice shows this."""
        from loom.mcp_server import authoring

        code = (
            "import random\n\n"
            "from loom import Context, step, workflow\n\n"
            "@step\n"
            "async def roll() -> int:\n"
            "    return random.randint(1, 10_000_000)\n\n"
            '@workflow(name="roller")\n'
            "async def roller(ctx: Context, x: str) -> int:\n"
            "    return await ctx.step(roll)\n"
        )
        result = parsed(await authoring.smoke_test_workflow(code, '"x"'))

        assert result["ok"] is True, result
        assert result["replay"]["status"] == "failed", result["replay"]
        assert result["replay"]["issues"][0]["category"] == "determinism"


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
    async def test_all_24_tools_are_registered_when_enabled(self, authoring_server) -> None:
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
        assert len(names) == 24

    async def test_disabled_via_config_drops_to_18(self, authoring_facade) -> None:
        from loom.mcp_server import build_server
        from loom.mcp_server.authoring_config import AuthoringConfig

        server = build_server(
            authoring_facade, name="off", authoring=AuthoringConfig(enabled=False)
        )
        names = {t.name for t in await server.list_tools()}
        assert len(names) == 18
        assert "get_tool_contract" not in names
        assert "save_workflow" not in names

    async def test_disabled_via_env_var(
        self, authoring_facade, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.mcp_server import build_server

        monkeypatch.setenv("LOOM_MCP_AUTHORING", "0")
        server = build_server(authoring_facade, name="off-env")
        names = {t.name for t in await server.list_tools()}
        assert len(names) == 18

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
        assert total <= 18_000, f"24 tools' schemas total {total} chars"

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

    async def test_the_prompt_tells_the_model_to_pass_the_spec(
        self, authoring_server
    ) -> None:
        """The two stages that judge intent are unreachable without it, so a
        client that never passes spec gets five checks and is told it got
        seven."""
        result = await authoring_server.get_prompt(
            "create_workflow", {"description": "sync CRM leads"}
        )
        text = str(result.messages[0].content)
        assert "spec=" in text
        assert "coverage" in text and "resolution" in text

    async def test_the_instructions_say_why_the_spec_matters(
        self, authoring_server
    ) -> None:
        instructions = authoring_server.instructions or ""
        assert "spec=" in instructions
        assert "skipped" in instructions

    async def test_the_validate_tool_advertises_a_spec_parameter(
        self, authoring_server
    ) -> None:
        by_name = {t.name: t for t in await authoring_server.list_tools()}
        schema = by_name["validate_workflow_code"].inputSchema
        assert "spec" in schema["properties"]
        assert schema["required"] == ["code"]


class TestNoDriftBetweenTheTwoToolSets:
    """``agents/workflow_tools.py`` and ``mcp_server/tools.py`` expose the same
    capabilities over the same facade to two different callers. They are
    allowed to overlap; they are not allowed to differ.

    They did. ``run_workflow`` deduplicated over MCP and did not in-process, so
    an agent retrying a call it thought had failed started a second run — the
    one failure an idempotency key exists to prevent. And two management tools
    existed only in-process.
    """

    async def test_both_run_workflow_tools_take_an_idempotency_key(self) -> None:
        import inspect

        from loom.agents.workflow_tools import build_workflow_tools
        from loom.mcp_server import tools as mcp_tools

        rt = _bare_runtime()
        by_name = {fn.__name__: fn for fn in build_workflow_tools(rt)}
        agent_side = inspect.signature(by_name["run_workflow"]).parameters
        mcp_side = inspect.signature(mcp_tools.run_workflow).parameters

        assert "idempotency_key" in agent_side
        assert "idempotency_key" in mcp_side

    async def test_the_key_actually_deduplicates_in_process(self) -> None:
        from loom.agents.workflow_tools import build_workflow_tools

        rt = _bare_runtime()
        run = {fn.__name__: fn for fn in build_workflow_tools(rt)}["run_workflow"]

        first = parsed(await run("doubler", "3", "same-key"))
        second = parsed(await run("doubler", "3", "same-key"))

        assert first["run_id"] == second["run_id"]

    async def test_mcp_gained_the_two_tools_that_only_existed_in_process(
        self, authoring_server
    ) -> None:
        names = {t.name for t in await authoring_server.list_tools()}
        assert {"get_workflow_info", "schedule_workflow"} <= names

    async def test_get_workflow_info_answers_for_one_by_name(self) -> None:
        from loom.facade import LocalFacade
        from loom.mcp_server import tools as mcp_tools

        facade = LocalFacade(_bare_runtime())
        found = parsed(await mcp_tools.get_workflow_info(facade, "doubler"))
        assert found["name"] == "doubler"
        assert "input_schema" in found

    async def test_get_workflow_info_names_the_others_when_there_is_no_match(
        self,
    ) -> None:
        from loom.facade import LocalFacade
        from loom.mcp_server import tools as mcp_tools

        facade = LocalFacade(_bare_runtime())
        result = parsed(await mcp_tools.get_workflow_info(facade, "nope"))
        assert "error" in result
        assert "doubler" in result["available"]

    async def test_schedule_workflow_registers_a_durable_trigger(self) -> None:
        from loom.facade import LocalFacade
        from loom.mcp_server import tools as mcp_tools

        facade = LocalFacade(_bare_runtime())
        made = parsed(await mcp_tools.schedule_workflow(facade, "doubler", "0 9 * * *"))
        assert made["workflow"] == "doubler"
        assert made["trigger_id"]

    async def test_scheduling_an_unknown_workflow_is_a_payload_not_a_raise(self) -> None:
        from loom.facade import LocalFacade
        from loom.mcp_server import tools as mcp_tools

        facade = LocalFacade(_bare_runtime())
        assert "error" in parsed(
            await mcp_tools.schedule_workflow(facade, "nope", "0 9 * * *")
        )

    async def test_a_malformed_cron_is_a_payload_not_a_raise(self) -> None:
        """A raise here would end the calling model's turn; an error payload
        is something it can correct and call again with."""
        from loom.facade import LocalFacade
        from loom.mcp_server import tools as mcp_tools

        facade = LocalFacade(_bare_runtime())
        assert "error" in parsed(
            await mcp_tools.schedule_workflow(facade, "doubler", "not a cron")
        )


def _bare_runtime():
    """A Runtime with the doubler from ``CLEAN_WORKFLOW`` registered."""
    from loom import Context, step, workflow
    from loom.runtime.engine import Runtime
    from loom.stores.memory import MemoryStore

    @step
    async def double(n: int) -> int:
        """Double a number."""
        return n * 2

    @workflow(name="doubler", description="Double the input")
    async def doubler(ctx: Context, n: int) -> int:
        """Double it."""
        return await ctx.step(double, n)

    rt = Runtime(store=MemoryStore())
    rt.register(doubler)
    return rt


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
