"""Verification stages for generated code.

The pipeline exists so a stage can be added without editing the generator, and
so every kind of failure — a syntax error, a type error, a traceback — reaches
the repair loop by one path. Previously each check was wired in individually
with its own shape, and only one of them could drive a repair.
"""

from __future__ import annotations

import pytest

from loom.agents.checks import (
    CheckContext,
    CheckPipeline,
    CheckResult,
    PipelineReport,
)
from loom.agents.stages import (
    CompileStage,
    LintStage,
    ReplayStage,
    SmokeStage,
    StaticStage,
    TypeStage,
    default_stages,
)
from loom.agents.validator import CodeIssue

GOOD = '''
from loom import Context, step, workflow

@step
async def double(n: int) -> int:
    """Double it."""
    return n * 2

@workflow(name="doubler")
async def doubler(ctx: Context, n: int) -> str:
    """Double and report."""
    return f"# Result\\n\\n{await ctx.step(double, n)}"
'''


class Recorder:
    """A stage that records that it ran, and reports what it was told to."""

    def __init__(self, name: str, cost: int, *, error: str = "", blocking: bool = True):
        self.name, self.cost, self.blocking = name, cost, blocking
        self._error = error
        self.ran = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        self.ran = True
        issues = [CodeIssue(self.name, self._error, "error")] if self._error else []
        return CheckResult(self.name, issues=issues)


class TestPipelineOrdering:
    async def test_stages_run_cheapest_first(self) -> None:
        order: list[str] = []

        class Tracking(Recorder):
            async def run(self, code, context):
                order.append(self.name)
                return await super().run(code, context)

        pipeline = CheckPipeline([Tracking("c", 30), Tracking("a", 10), Tracking("b", 20)])
        await pipeline.run(GOOD, CheckContext())

        assert order == ["a", "b", "c"]

    async def test_a_blocking_failure_stops_the_rest(self) -> None:
        """No point type-checking code that does not compile."""
        first = Recorder("first", 10, error="boom")
        second = Recorder("second", 20)

        report = await CheckPipeline([first, second]).run(GOOD, CheckContext())

        assert first.ran
        assert not second.ran, "a stage ran after a blocking failure"
        assert not report.ok

    async def test_a_non_blocking_failure_does_not(self) -> None:
        first = Recorder("first", 10, error="meh", blocking=False)
        second = Recorder("second", 20)

        report = await CheckPipeline([first, second]).run(GOOD, CheckContext())

        assert second.ran
        assert len(report.errors) == 1

    async def test_the_report_collects_every_stage(self) -> None:
        report = await CheckPipeline(
            [Recorder("a", 10), Recorder("b", 20)]
        ).run(GOOD, CheckContext())

        assert [r.name for r in report.results] == ["a", "b"]
        assert report.ok
        assert report.summary == "a=ok b=ok"

    async def test_a_stage_can_be_added_without_touching_the_others(self) -> None:
        """The point of the abstraction: registration, not surgery."""
        extra = Recorder("house-rule", 15)
        pipeline = CheckPipeline([CompileStage(), extra])

        await pipeline.run(GOOD, CheckContext())

        assert extra.ran
        assert "house-rule" in pipeline.names


class TestSkippingIsNotPassing:
    def test_a_skipped_stage_reports_itself(self) -> None:
        result = CheckResult("lint", skipped=True, reason="ruff is not installed")

        assert result.ok, "a skipped stage is not a failure"
        assert result.skipped
        assert result.reason

    async def test_the_summary_distinguishes_skip_from_ok(self) -> None:
        class Skipper(Recorder):
            async def run(self, code, context):
                return CheckResult(self.name, skipped=True, reason="absent")

        report = await CheckPipeline([Skipper("lint", 10)]).run(GOOD, CheckContext())
        assert report.summary == "lint=skip"


class TestConcreteStages:
    async def test_compile_catches_a_syntax_error(self) -> None:
        result = await CompileStage().run("def broken(:\n", CheckContext())
        assert result.errors
        assert result.errors[0].category == "syntax"

    async def test_compile_passes_valid_code(self) -> None:
        assert (await CompileStage().run(GOOD, CheckContext())).ok

    async def test_static_applies_the_ast_rules(self) -> None:
        result = await StaticStage().run(
            "from loom.stores.memory import MemoryStore\n"
            "store = MemoryStore()\n",
            CheckContext(),
        )
        assert result.issues, "the store rule did not fire"

    async def test_static_honours_the_available_toolsets(self) -> None:
        result = await StaticStage().run(
            "from loom.toolsets.nope.tools import thing\n",
            CheckContext(available_toolsets={"jira"}),
        )
        assert any(i.category == "toolset" for i in result.errors)

    async def test_lint_catches_an_undefined_name(self) -> None:
        result = await LintStage().run("def f():\n    return undefined_thing\n", CheckContext())
        if result.skipped:
            pytest.skip(result.reason)
        assert result.errors
        assert "F821" in result.errors[0].message

    async def test_lint_ignores_style(self) -> None:
        """A model should not spend a repair round on line length."""
        result = await LintStage().run("x = 1  # " + "y" * 200 + "\n", CheckContext())
        if result.skipped:
            pytest.skip(result.reason)
        assert result.ok

    async def test_clean_code_produces_no_type_noise(self) -> None:
        """It reported 41 warnings on a correct workflow — all of them from
        inside loom, followed through its imports. Noise at that
        volume is worse than no type checking, because it buries the real ones."""
        result = await TypeStage().run(GOOD, CheckContext())
        if result.skipped:
            pytest.skip(result.reason)
        assert result.issues == [], [i.message for i in result.issues]

    async def test_it_catches_a_wrong_arity_call(self) -> None:
        """The defect that compiles, lints clean, and fails at run time."""
        code = GOOD + "\n\ndef helper(a: int, b: int) -> int:\n    return a + b\n\n_ = helper(1)\n"
        result = await TypeStage().run(code, CheckContext())
        if result.skipped:
            pytest.skip(result.reason)
        assert any("Missing positional argument" in i.message for i in result.issues)

    async def test_it_catches_a_wrong_return_type(self) -> None:
        code = GOOD.replace("return n * 2", "return 'not an int'")
        result = await TypeStage().run(code, CheckContext())
        if result.skipped:
            pytest.skip(result.reason)
        assert any("Incompatible return value" in i.message for i in result.issues)

    async def test_types_are_reported_as_warnings(self) -> None:
        """Advisory: generated code is not annotated to a strict standard."""
        result = await TypeStage().run(
            "def f(n: int) -> int:\n    return n\n\nf('not an int')\n", CheckContext()
        )
        if result.skipped:
            pytest.skip(result.reason)
        assert result.issues
        assert all(i.severity == "warning" for i in result.issues)

    async def test_a_missing_tool_skips_rather_than_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A check that cannot run has found nothing."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr("pathlib.Path.exists", lambda _self: False)

        for stage in (LintStage(), TypeStage()):
            result = await stage.run(GOOD, CheckContext())
            assert result.skipped
            assert result.ok
            assert "not installed" in result.reason


class TestSmokeAndReplay:
    async def test_smoke_runs_the_code(self) -> None:
        result = await SmokeStage().run(GOOD, CheckContext(workflow_input=21))
        assert result.ok, [i.message for i in result.issues]
        assert result.detail.status == "completed"

    async def test_smoke_reports_a_real_failure_as_an_error(self) -> None:
        broken = GOOD.replace("return n * 2", "return undefined_name")
        result = await SmokeStage().run(broken, CheckContext(workflow_input=1))

        assert result.errors
        assert result.errors[0].category == "runtime"

    async def test_replay_passes_for_a_deterministic_workflow(self) -> None:
        result = await ReplayStage().run(GOOD, CheckContext(workflow_input=21))
        if result.skipped:
            pytest.skip(result.reason)
        assert result.ok

    async def test_replay_catches_output_that_varies(self) -> None:
        """What the static determinism lint cannot see."""
        wobbly = '''
import random
from loom import Context, step, workflow

@step
async def pick() -> int:
    """Return something different each run."""
    return random.randint(1, 10_000_000)

@workflow(name="wobbly")
async def wobbly(ctx: Context, _in: str) -> str:
    """Report a value that will not reproduce."""
    return str(await ctx.step(pick))
'''
        result = await ReplayStage().run(wobbly, CheckContext(workflow_input="x"))
        if result.skipped:
            pytest.skip(result.reason)
        assert result.errors
        assert result.errors[0].category == "determinism"


class TestDefaultArrangement:
    def test_it_is_ordered_by_cost(self) -> None:
        pipeline = CheckPipeline(default_stages())
        assert pipeline.names[:2] == ["compile", "static"]
        assert pipeline.names[-1] == "replay"

    def test_smoke_can_be_left_out(self) -> None:
        names = CheckPipeline(default_stages(smoke=False)).names
        assert "smoke" not in names
        assert "compile" in names

    def test_a_supervisor_adds_the_critique_stage(self) -> None:
        names = CheckPipeline(default_stages(supervisor=object())).names
        assert names[-1] == "critique"


class TestReportHelpers:
    def test_detail_returns_a_stage_payload(self) -> None:
        report = PipelineReport(results=[CheckResult("smoke", detail="payload")])
        assert report.detail("smoke") == "payload"
        assert report.detail("absent") is None


class TestGenerateAlwaysAnswers:
    """A caller asked for code and is entitled to an answer it can act on.

    An exception discards whatever the run learned and hands back a stack trace
    instead of a reason — and the commonest cause, an exhausted turn budget, is
    something the caller can actually fix if told.
    """

    async def test_an_agent_failure_becomes_an_issue_not_an_exception(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent
        from loom.core.exceptions import UsageLimitExceeded

        class FakeModel:
            model_name = "fake"

        class Exploding:
            def __init__(self, *a, **k) -> None: ...
            async def __call__(self, *a, **k):
                raise UsageLimitExceeded("agent exceeded its budget of 23 turns")

        import loom.agents.agent as agent_module

        original = agent_module.Agent
        agent_module.Agent = Exploding
        try:
            result = await WorkflowCodingAgent(FakeModel(), smoke_test=False).generate("x")
        finally:
            agent_module.Agent = original

        assert result.code == ""
        assert not result.is_clean
        assert result.issues[0].category == "unsupported"
        # The message must say what the caller can do about it.
        assert "max_discovery_turns" in result.issues[0].message

    def test_the_budgets_are_separate_and_tunable(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(
            object(), max_repair_attempts=2, max_discovery_turns=30
        )
        assert agent._max_discovery == 30
        assert agent._max_repair == 2


class TestToolsetOnlyWorkflows:
    """A workflow built from toolset operations declares no step of its own.

    Those operations are steps already, and the prompt says to call them
    directly — so warning about a missing @step nags every integration
    workflow about doing exactly what it was told.
    """

    def test_no_step_warning_for_a_toolset_workflow(self) -> None:
        from loom.agents.validator import CodeValidator

        code = (
            "from loom import Context, workflow\n"
            "from loom.toolsets.jira.tools import jira_search_issues\n\n"
            "@workflow(name='x')\n"
            "async def x(ctx: Context, q: str) -> str:\n"
            "    return str(await ctx.step(jira_search_issues, q))\n"
        )
        assert not CodeValidator().validate(code)

    def test_the_warning_still_fires_without_toolsets(self) -> None:
        from loom.agents.validator import CodeValidator

        code = (
            "from loom import Context, workflow\n\n"
            "@workflow(name='y')\n"
            "async def y(ctx: Context, q: str) -> str:\n"
            "    return q\n"
        )
        messages = [i.message for i in CodeValidator().validate(code)]
        assert any("No @step" in m for m in messages)


class TestRepairKeepsContext:
    """A repair round is a fresh conversation, and must carry both halves.

    Observed: the model was handed errors and code with no spec, and replied
    asking what workflow it was supposed to be building. That prose was then
    accepted as the code, replacing a candidate that was at least Python.
    """

    def test_the_prompt_carries_the_spec(self) -> None:
        from loom.agents.checks import PipelineReport
        from loom.agents.coding_agent import _repair_prompt

        report = PipelineReport(
            results=[CheckResult("compile", issues=[CodeIssue("syntax", "bad", "error")])]
        )
        prompt = _repair_prompt(report, "code here", spec="List all open bugs")

        assert "List all open bugs" in prompt
        assert "code here" in prompt
        assert "bad" in prompt

    def test_it_forbids_answering_with_a_question(self) -> None:
        from loom.agents.checks import PipelineReport
        from loom.agents.coding_agent import _repair_prompt

        prompt = _repair_prompt(PipelineReport(), "code", spec="do a thing")
        assert "no questions, no prose" in prompt

    async def test_a_regressing_repair_is_discarded(self) -> None:
        """Prose in place of code is the case that motivated this."""
        from loom.agents.checks import CheckContext
        from loom.agents.coding_agent import CodingOutput, WorkflowCodingAgent

        original = "from loom import workflow  # at least Python\n"
        prose = "I need to see the original workflow specification to help.\n"

        class Replying:
            async def __call__(self, _prompt: str):
                return type("R", (), {"output": CodingOutput(code=prose)})()

        agent = WorkflowCodingAgent(object(), smoke_test=False)
        context = CheckContext(spec="build something")
        report = await agent._pipeline.run(original, context)

        code, rounds = await agent._repair_from(Replying(), original, report, context)

        assert code == original, "prose replaced the code"
        assert rounds >= 1

    async def test_an_improving_repair_is_taken(self) -> None:
        from loom.agents.checks import CheckContext
        from loom.agents.coding_agent import CodingOutput, WorkflowCodingAgent

        broken = "def f(:\n"
        fixed = GOOD

        class Fixing:
            async def __call__(self, _prompt: str):
                return type("R", (), {"output": CodingOutput(code=fixed)})()

        agent = WorkflowCodingAgent(object(), smoke_test=False)
        context = CheckContext(spec="double a number")
        report = await agent._pipeline.run(broken, context)

        code, rounds = await agent._repair_from(Fixing(), broken, report, context)

        assert code.strip() == fixed.strip()  # _extract_code strips
        assert rounds == 1


class TestRuntimeFromEnvCanRunAgentNodes:
    """A generated workflow may contain ctx.agent(); the demo must run it.

    The resolution ladder routinely emits an agent node for an ambiguity, so a
    Runtime that cannot call a model turns every such workflow into a failure —
    reported, before this, as "Status: failed / Output: None" with no reason.
    """

    def test_a_provider_key_configures_a_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom import Runtime

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        assert Runtime.from_env().agent_backend is not None

    def test_no_key_means_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Most workflows never call one; absence is not a misconfiguration."""
        from loom import Runtime

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        assert Runtime.from_env().agent_backend is None

    def test_an_explicit_choice_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loom import Runtime

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert Runtime.from_env(agent_backend=None).agent_backend is None

    def test_the_demo_block_prints_the_error(self) -> None:
        """"Status: failed / Output: None" says nothing a reader can act on."""
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "if result.error:" in DEFAULT_SYSTEM_PROMPT
        assert "result.error.message" in DEFAULT_SYSTEM_PROMPT


class TestCodingResultLoad:
    """Generating a file is only useful if you can run it.

    Every caller that wanted to otherwise wrote the same importlib boilerplate —
    the cookbook carried its own copy before this existed.
    """

    def test_it_returns_the_workflow(self) -> None:
        from loom.agents.coding_agent import CodingResult
        from loom.runtime.workflow import WorkflowDefinition

        definition = CodingResult(code=GOOD).load()

        assert isinstance(definition, WorkflowDefinition)
        assert definition.name == "doubler"

    async def test_the_loaded_workflow_actually_runs(self) -> None:
        from loom import Runtime
        from loom.agents.coding_agent import CodingResult
        from loom.stores.memory import MemoryStore

        definition = CodingResult(code=GOOD).load()
        runtime = Runtime(store=MemoryStore())
        runtime.register(definition)

        result = await runtime.run(definition.name, 21)
        assert result.status.value == "completed"
        assert "42" in str(result.output)

    def test_no_code_raises_with_the_reason(self) -> None:
        """A refusal carries why; loading it should repeat that, not say None."""
        from loom.agents.coding_agent import CodingResult
        from loom.agents.validator import CodeIssue

        result = CodingResult(
            code="",
            issues=[CodeIssue("unsupported", "no Slack toolset here", "error")],
        )
        with pytest.raises(ValueError, match="no Slack toolset here"):
            result.load()

    def test_code_without_a_workflow_raises(self) -> None:
        from loom.agents.coding_agent import CodingResult

        with pytest.raises(ValueError, match="declares no @workflow"):
            CodingResult(code="x = 1\n").load()


class TestTheAuthoringPlaneNeedsNoExecutor:
    """P4: authoring must not require the thing that runs workflows.

    It already did not — but "already true" and "guaranteed" differ by exactly
    one test. The boundary held by accident of how the code grew, and the first
    convenience import of ``Runtime`` into a check stage would have erased it
    without anything objecting.

    The smoke stage is the interesting case: it *does* need a Runtime, and
    builds one **inside a subprocess**. That is the boundary working, not a
    violation of it — the executor lives on the far side of a process wall, and
    the authoring process never holds one.
    """

    AUTHORING = (
        "loom/agents/coding_agent.py",
        "loom/agents/stages.py",
        "loom/agents/checks.py",
        "loom/agents/validator.py",
        "loom/agents/coding_tools.py",
    )

    def _source(self, relative: str) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src"
        return (root / relative).read_text(encoding="utf-8")

    @pytest.mark.parametrize("module", AUTHORING)
    def test_no_authoring_module_imports_the_runtime(self, module: str) -> None:
        """A generated file is text until something chooses to run it."""
        import ast

        tree = ast.parse(self._source(module))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported += [
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name == "Runtime"
                ]
            elif isinstance(node, ast.Import):
                imported += [
                    a.name for a in node.names if a.name.endswith("runtime.engine")
                ]

        assert not imported, f"{module} imports {imported}; authoring stays executor-free"

    def test_generating_a_workflow_constructs_no_runtime(self) -> None:
        """The property end to end, not module by module."""
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(model=object())

        assert not any(
            type(value).__name__ == "Runtime" for value in vars(agent).values()
        )

    def test_the_smoke_stage_puts_the_executor_behind_a_process_wall(self) -> None:
        """Where the Runtime legitimately appears — and where it stays."""
        import inspect

        from loom.agents import smoke

        source = inspect.getsource(smoke)
        assert "subprocess" in source
        assert "Runtime(" in source, "the child does build one; that is the point"

        # ...and the parent side never does.
        parent = source[: source.index("HARNESS")] if "HARNESS" in source else ""
        assert "Runtime(" not in parent


class TestIsCleanAgreesWithThePipeline:
    """"verified: False" on correct code is a dead end without a reason.

    Two defects, one symptom. ``is_clean`` read ``smoke.ok`` directly, so an
    *environmental* smoke failure made it False — while the smoke stage that
    produced that very result had already ruled it a **warning**, on the
    grounds that a sandbox with no credential says nothing about the code. Two
    parts of one pipeline disagreeing, with the caller told only "False".

    And there was no way to ask why: the reason is spread across ``issues``,
    ``smoke``, and ``review``.
    """

    def _result(self, **kwargs):
        from loom.agents.coding_agent import CodingResult

        return CodingResult(code="x = 1", **kwargs)

    def _smoke(self, error: str):
        from loom.agents.smoke import SmokeResult

        return SmokeResult(ok=False, phase="run", error=error, traceback="")

    def test_good_code_is_clean_and_lists_nothing(self) -> None:
        result = self._result()

        assert result.is_clean
        assert result.blockers == []

    def test_an_environmental_smoke_failure_is_not_a_blocker(self) -> None:
        """The sandbox having no credential is not a fact about the code."""
        result = self._result(smoke=self._smoke("401 Unauthorized"))

        assert result.smoke.environmental
        assert result.is_clean, "is_clean disagreed with the stage that ruled on it"
        assert result.blockers == []

    def test_a_real_smoke_failure_still_blocks_and_says_so(self) -> None:
        result = self._result(smoke=self._smoke("'NoneType' has no attribute 'x'"))

        assert not result.is_clean
        assert result.blockers == ["smoke: 'NoneType' has no attribute 'x'"]

    def test_an_error_issue_blocks_and_names_its_category(self) -> None:
        from loom.agents.validator import CodeIssue

        result = self._result(
            issues=[
                CodeIssue("imports", "no such name", "error"),
                CodeIssue("types", "returns Any", "warning"),
            ]
        )

        assert not result.is_clean
        assert result.blockers == ["imports: no such name"]
        assert not any("types" in b for b in result.blockers), "warnings do not block"

    def test_blockers_is_empty_exactly_when_clean(self) -> None:
        """The two must not be able to disagree, whatever is set."""
        from loom.agents.validator import CodeIssue

        for result in (
            self._result(),
            self._result(issues=[CodeIssue("x", "y", "error")]),
            self._result(smoke=self._smoke("boom")),
            self._result(smoke=self._smoke("connection refused")),
        ):
            assert result.is_clean is (not result.blockers)
