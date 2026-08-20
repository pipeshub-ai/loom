"""Phase 2 — agent quality.

The eval harness comes first in this file for the same reason it comes first in
the phase: everything else here is a change whose value is a claim until there
is an instrument that can measure it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from loom.agents.messages import ToolCall, assistant, system, tool_result, user
from loom.core.models import Usage

# ---------------------------------------------------------------------------
# M8 — the instrument
# ---------------------------------------------------------------------------


@dataclass
class _FakeSmoke:
    ok: bool = True
    empty_paths: list[str] = field(default_factory=list)


@dataclass
class _FakeIssue:
    severity: str
    message: str = "x"


@dataclass
class _FakeResult:
    """Enough of a ``CodingResult`` for the harness, which reads it structurally."""

    code: str = "from loom import workflow\n"
    issues: list[Any] = field(default_factory=list)
    repair_attempts: int = 0
    smoke: Any = field(default_factory=_FakeSmoke)
    input_tokens: int = 100
    output_tokens: int = 50
    tool_calls: list[Any] = field(default_factory=list)
    review: Any = None
    model_used: str = "fake-model"


class _FakeCoder:
    """A ``Coder`` with no model, no key and no network.

    The harness depends on the protocol rather than on ``WorkflowCodingAgent``
    precisely so this is possible: an eval suite that only runs when somebody
    has credentials is an eval suite that does not run.
    """

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = results or {}
        self.seen: list[str] = []

    async def generate(self, spec: str) -> Any:
        self.seen.append(spec)
        outcome = self.results.get(spec, _FakeResult())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestEvalHarness:
    """`eval/` shipped three Pydantic models and no runner, judge, dataset or
    gate, while `phases/phase-7` specified a model-stratified suite. Without it,
    no change to the agent could be shown to help."""

    def test_the_reference_dataset_is_the_committed_specs(self) -> None:
        from loom.eval import load_reference_dataset

        dataset = load_reference_dataset()

        assert len(dataset.cases) == 10
        assert all(case.input.strip() for case in dataset.cases)
        assert dataset.cases[0].id == "wf01"

    @pytest.mark.asyncio
    async def test_a_clean_generation_passes(self) -> None:
        from loom.eval import EvalRunner, dataset_from

        dataset = dataset_from([("a", "do a thing")], dataset_id="t")
        report = await EvalRunner(coder=_FakeCoder(), model="fake").run(dataset)

        assert report.cases == 1
        assert report.passed == 1
        assert report.pass_rate == 1.0
        assert report.clean_first_pass == 1.0

    @pytest.mark.asyncio
    async def test_a_blocking_error_fails_the_case(self) -> None:
        from loom.eval import EvalRunner, dataset_from

        broken = _FakeResult(issues=[_FakeIssue("error", "no @workflow found")])
        dataset = dataset_from([("a", "spec")], dataset_id="t")
        report = await EvalRunner(
            coder=_FakeCoder({"spec": broken}), model="fake"
        ).run(dataset)

        assert not report.outcomes[0].passed
        assert report.outcomes[0].blocking_errors == 1

    @pytest.mark.asyncio
    async def test_an_empty_answer_costs_score_without_failing_outright(
        self,
    ) -> None:
        from loom.eval import EvalRunner, dataset_from

        hollow = _FakeResult(smoke=_FakeSmoke(ok=True, empty_paths=["stage2.fields"]))
        dataset = dataset_from([("a", "spec")], dataset_id="t")
        report = await EvalRunner(
            coder=_FakeCoder({"spec": hollow}), model="fake"
        ).run(dataset)

        assert report.outcomes[0].empty_output_paths == ["stage2.fields"]
        assert report.outcomes[0].score < 1.0

    @pytest.mark.asyncio
    async def test_a_case_that_raises_is_data_not_a_crash(self) -> None:
        """A harness that dies on one case reports nothing about the other nine."""
        from loom.eval import EvalRunner, dataset_from

        dataset = dataset_from(
            [("bad", "boom"), ("good", "fine")], dataset_id="t"
        )
        coder = _FakeCoder({"boom": RuntimeError("provider exploded")})
        report = await EvalRunner(coder=coder, model="fake").run(dataset)

        assert report.cases == 2
        failed = next(o for o in report.outcomes if o.case_id == "bad")
        assert "provider exploded" in failed.error
        assert not failed.passed
        assert next(o for o in report.outcomes if o.case_id == "good").passed

    @pytest.mark.asyncio
    async def test_repair_rounds_are_reported(self) -> None:
        from loom.eval import EvalRunner, dataset_from

        repaired = _FakeResult(repair_attempts=2)
        report = await EvalRunner(
            coder=_FakeCoder({"spec": repaired}), model="fake"
        ).run(dataset_from([("a", "spec")], dataset_id="t"))

        assert report.mean_repair_rounds == 2.0
        assert report.clean_first_pass == 0.0

    def test_the_gate_passes_when_nothing_regressed(self) -> None:
        from loom.eval import EvalReport, compare
        from loom.eval.runner import CaseOutcome

        report = EvalReport(
            dataset_id="t",
            model="fake",
            outcomes=[CaseOutcome(case_id="a", score=1.0, passed=True)],
        )
        baseline = {"summary": {"pass_rate": 1.0, "mean_score": 1.0}}

        assert compare(baseline, report) == []

    def test_the_gate_fires_on_a_real_regression(self) -> None:
        from loom.eval import EvalReport, compare
        from loom.eval.runner import CaseOutcome

        report = EvalReport(
            dataset_id="t",
            model="fake",
            outcomes=[
                CaseOutcome(case_id="a", score=0.0, passed=False),
                CaseOutcome(case_id="b", score=1.0, passed=True),
            ],
        )
        baseline = {"summary": {"pass_rate": 1.0, "mean_score": 1.0}}

        regressions = compare(baseline, report)

        assert {r.metric for r in regressions} == {"pass_rate", "mean_score"}
        assert "vs baseline" in str(regressions[0])

    def test_the_gate_tolerates_noise(self) -> None:
        """A gate that fires on run-to-run variance is a gate people switch off."""
        from loom.eval import EvalReport, compare
        from loom.eval.runner import CaseOutcome

        report = EvalReport(
            dataset_id="t",
            model="fake",
            outcomes=[CaseOutcome(case_id="a", score=0.97, passed=True)],
        )
        baseline = {"summary": {"mean_score": 1.0}}

        assert compare(baseline, report) == []

    def test_a_report_round_trips_through_json(self, tmp_path) -> None:
        import json

        from loom.eval import EvalReport
        from loom.eval.runner import CaseOutcome

        report = EvalReport(
            dataset_id="t",
            model="fake",
            outcomes=[CaseOutcome(case_id="a", score=1.0, passed=True)],
        )
        path = tmp_path / "r.json"
        report.write(path)

        stored = json.loads(path.read_text())
        assert stored["summary"]["cases"] == 1
        assert stored["cases"][0]["case_id"] == "a"

    def test_the_gate_script_resolves_what_it_imports(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Its imports are inside a function, so nothing checked them until it
        ran — and it shipped naming a symbol the registry does not export. A
        CI gate that fails on its own import is not a gate."""
        import importlib.util
        from pathlib import Path

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "run_eval", root / "scripts" / "run_eval.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module._build_agent("claude-sonnet-5") is None
        assert "no API key" in capsys.readouterr().err

    def test_the_gate_script_exits_two_when_it_cannot_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Exit codes are the contract: 2 is "could not run", not "regressed"."""
        import importlib.util
        from pathlib import Path

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "run_eval_exit", root / "scripts" / "run_eval.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.main(["--baseline", str(tmp_path / "absent.json")]) == 2

    def test_the_structural_judge_is_deterministic(self) -> None:
        """It gates CI, so the same input must give the same number twice."""
        from loom.eval import StructuralJudge
        from loom.eval.dataset import EvalCase

        judge = StructuralJudge()
        case = EvalCase(id="a", input="spec")
        result = _FakeResult()

        first, second = judge.score(case, result), judge.score(case, result)

        assert first.value == second.value
        assert first.passed == second.passed

    def test_expected_toolsets_are_checked_when_declared(self) -> None:
        from loom.eval import StructuralJudge
        from loom.eval.dataset import EvalCase

        judge = StructuralJudge()
        case = EvalCase(id="a", input="spec", expected_toolsets=["gmail_send"])

        missing = judge.score(case, _FakeResult(code="print('nope')"))
        present = judge.score(case, _FakeResult(code="gmail_send(...)"))

        assert missing.value < present.value


# ---------------------------------------------------------------------------
# H5 — the conversation survives a repair round, and the job has a budget
# ---------------------------------------------------------------------------


@dataclass
class _Reply:
    output: Any
    messages: list[Any] = field(default_factory=list)
    turns: int = 1
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))
    tool_calls: list[Any] = field(default_factory=list)


class _RecordingAgent:
    """Captures the context each call was given."""

    name = "coder"

    def __init__(self) -> None:
        self.limits: Any = None
        self.contexts: list[Any] = []
        self.calls = 0

    async def __call__(self, prompt: str, *, context: Any = None, **_: Any) -> Any:
        self.calls += 1
        self.contexts.append(context)
        return _Reply(
            output="code",
            messages=[
                system("instructions"),
                user(prompt),
                assistant(content=f"answer {self.calls}"),
            ],
        )


class TestTheConversationSurvivesARepair:
    """`WorkflowCodingAgent` called `agent(spec)`, then later
    `agent(repair_prompt)` — always without a context, so the runner rebuilt the
    conversation from the system prompt and the new input alone. Every repair
    round lost the toolset schemas the model had fetched, the entity ids it had
    resolved through real API calls, and its own plan."""

    @pytest.mark.asyncio
    async def test_the_first_call_starts_from_nothing(self) -> None:
        from loom.agents.generation import CodingSession, GenerationBudget

        agent = _RecordingAgent()
        session = CodingSession(agent, GenerationBudget(max_turns=10))

        await session.ask("first")

        assert agent.contexts[0].history == []

    @pytest.mark.asyncio
    async def test_a_later_call_sees_the_earlier_turns(self) -> None:
        from loom.agents.generation import CodingSession, GenerationBudget

        agent = _RecordingAgent()
        session = CodingSession(agent, GenerationBudget(max_turns=10))

        await session.ask("discover")
        await session.ask("now repair it")

        history = agent.contexts[1].history
        assert [m.content for m in history] == ["discover", "answer 1"]

    @pytest.mark.asyncio
    async def test_the_system_prompt_is_not_carried_forward(self) -> None:
        """The runner prepends `agent.instructions` on every call, so a system
        message kept here would be sent twice and grow by one each round."""
        from loom.agents.generation import CodingSession, GenerationBudget
        from loom.agents.messages import Role

        agent = _RecordingAgent()
        session = CodingSession(agent, GenerationBudget(max_turns=10))

        await session.ask("one")
        await session.ask("two")

        assert all(m.role is not Role.SYSTEM for m in agent.contexts[1].history)


class TestTheJobHasOneBudget:
    """`turn` and `cumulative_usage` are locals inside the runner's loop, so
    each invocation restarted at turn 1 with the full allowance. A generation
    with three repair rounds got four independent budgets, and `max_cost_usd`
    bounded one call and never the work."""

    @pytest.mark.asyncio
    async def test_turns_accumulate_across_calls(self) -> None:
        from loom.agents.generation import CodingSession, GenerationBudget

        budget = GenerationBudget(max_turns=10)
        session = CodingSession(_RecordingAgent(), budget)

        await session.ask("a")
        await session.ask("b")
        await session.ask("c")

        assert budget.turns_used == 3
        assert budget.turns_left == 7

    @pytest.mark.asyncio
    async def test_each_call_is_capped_by_what_is_left(self) -> None:
        """Turns are what is left of the *job*, not of a call — which is the
        whole point: without this, every invocation restarted at turn 1 with
        the full allowance."""
        from loom.agents.generation import CodingSession, GenerationBudget

        seen: list[int] = []

        class _Watching(_RecordingAgent):
            async def __call__(self, prompt: str, *, context: Any = None, **kw: Any):
                seen.append(self.limits.max_turns)
                return await super().__call__(prompt, context=context, **kw)

        agent = _Watching()
        session = CodingSession(agent, GenerationBudget(max_turns=3))

        await session.ask("a")
        await session.ask("b")

        assert seen == [3, 2]

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_refuses_before_spending(self) -> None:
        from loom.agents.generation import (
            BudgetExhausted,
            CodingSession,
            GenerationBudget,
        )

        agent = _RecordingAgent()
        session = CodingSession(agent, GenerationBudget(max_turns=1))
        await session.ask("a")

        with pytest.raises(BudgetExhausted):
            await session.ask("b")

        assert agent.calls == 1, "the refused call must not have been made"

    @pytest.mark.asyncio
    async def test_tokens_accumulate_across_calls(self) -> None:
        from loom.agents.generation import CodingSession, GenerationBudget

        budget = GenerationBudget(max_turns=10)
        session = CodingSession(_RecordingAgent(), budget)

        await session.ask("a")
        await session.ask("b")

        assert budget.spent.input_tokens == 20
        assert budget.spent.output_tokens == 10

    @pytest.mark.asyncio
    async def test_the_agents_own_limits_are_restored(self) -> None:
        """The session narrows the agent for one call; it must not keep it."""
        from loom.agents.generation import CodingSession, GenerationBudget
        from loom.agents.limits import UsageLimits

        agent = _RecordingAgent()
        agent.limits = UsageLimits(max_turns=99)
        session = CodingSession(agent, GenerationBudget(max_turns=5))

        await session.ask("a")

        assert agent.limits.max_turns == 99

    def test_a_dollar_ceiling_on_an_unpriced_model_is_refused(self) -> None:
        """`estimate_cost` returns 0.0 for a model with no price, so the
        ceiling could never be reached — a budget that reads as enforced and is
        not."""
        from loom.agents.generation import GenerationBudget
        from loom.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError) as caught:
            GenerationBudget.for_agent(
                "a-model-nobody-priced", max_turns=5, max_cost_usd=1.0
            )

        assert "no price on file" in str(caught.value)

    def test_a_priced_model_is_accepted(self) -> None:
        from loom.agents.generation import GenerationBudget

        budget = GenerationBudget.for_agent(
            "claude-sonnet-5", max_turns=5, max_cost_usd=1.0
        )
        assert budget.max_cost_usd == 1.0


# ---------------------------------------------------------------------------
# The escape hatch three stages promise, and nothing delivered
# ---------------------------------------------------------------------------


_SETTLED = """from loom import Context, step, workflow


@step
async def render(rows: list) -> str:
    return str(rows)


@workflow(name="w")
async def w(ctx: Context, i=None) -> str:
    return await ctx.step(render, [])
"""


class _Declining:
    """A model that reviewed the finding and stood by the file.

    Rung 4 of the resolution ladder, and what `outcome` and `judgement` invite
    too: returning the code unchanged is how a model says "I checked, and the
    finding is wrong here".
    """

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, prompt: str):
        from loom.agents.coding_agent import CodingOutput

        self.asked += 1
        return type("R", (), {"output": CodingOutput(code=_SETTLED)})()


def _report_of(category: str, severity: str = "error"):
    from loom.agents.checks import CheckResult, PipelineReport
    from loom.agents.validator import CodeIssue

    report = PipelineReport()
    report.results.append(
        CheckResult(category, issues=[CodeIssue(category, "a finding", severity)])
    )
    return report


class TestTheEscapeHatchIsHonoured:
    """Three stages raise their findings as *errors* only because
    `report.errors` drives the repair loop, and each tells the model that
    returning the file unchanged is the accepted answer.

    The loop honoured half of that: it stopped. The finding stayed an error, so
    `is_clean` was `False` and callers refused to run the code — a workflow that
    had walked every rung of the resolution ladder and written down each
    namespace it checked came back reported as broken.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stage", sorted(["outcome", "resolution", "judgement"]))
    async def test_a_declined_finding_stops_blocking(self, stage: str) -> None:
        from loom.agents.checks import CheckContext, CheckPipeline
        from loom.agents.coding_agent import WorkflowCodingAgent, _settle_advisories

        agent = WorkflowCodingAgent.__new__(WorkflowCodingAgent)
        agent._max_repair = 3
        agent._pipeline = CheckPipeline([])
        session = _Declining()

        _, _, declined = await agent._repair_from(
            session, _SETTLED, _report_of(stage), CheckContext(spec="a spec")
        )
        settled = _settle_advisories(list(_report_of(stage).issues), declined=declined)

        assert session.asked == 1, "asked once, then accepted"
        assert declined is True
        assert [i.severity for i in settled] == ["warning"]

    def test_the_finding_is_kept_not_swallowed(self) -> None:
        """Downgraded, never dropped: the reader still needs to see it."""
        from loom.agents.coding_agent import _settle_advisories

        settled = _settle_advisories(list(_report_of("resolution").issues), declined=True)

        assert "a finding" in settled[0].message
        assert "stood by the code" in settled[0].message

    def test_a_real_defect_is_never_downgraded(self) -> None:
        from loom.agents.coding_agent import _settle_advisories

        settled = _settle_advisories(list(_report_of("static").issues), declined=True)

        assert settled[0].severity == "error"

    def test_running_out_of_turns_is_not_a_judgement(self) -> None:
        """A repair that never declined has decided nothing, and downgrading
        there would turn "could not fix it" into "decided it was fine"."""
        from loom.agents.coding_agent import _settle_advisories

        settled = _settle_advisories(
            list(_report_of("resolution").issues), declined=False
        )

        assert settled[0].severity == "error"

    def test_a_clean_result_becomes_runnable(self) -> None:
        """What the caller actually gates on. The cookbook prints "Code has
        errors - skipping execution" from exactly this."""
        from loom.agents.coding_agent import CodingResult, _settle_advisories
        from loom.agents.validator import CodeIssue

        finding = CodeIssue("resolution", "a fuzzy text search", "error")

        assert not CodingResult(code="x=1", issues=[finding]).is_clean
        assert CodingResult(
            code="x=1", issues=_settle_advisories([finding], declined=True)
        ).is_clean

    def test_every_advisory_stage_actually_makes_the_promise(self) -> None:
        """The set and the messages cannot drift apart.

        A stage listed here that does not offer the escape hatch would have its
        findings silently downgraded; one that offers it without being listed
        would go on blocking correct code.
        """
        import inspect

        from loom.agents import stages as stage_module
        from loom.agents.stages import ADVISORY_STAGES, ESCAPE_HATCH

        offered = {
            getattr(obj, "name", "")
            for _, obj in inspect.getmembers(stage_module, inspect.isclass)
            if getattr(obj, "name", "")
            and ESCAPE_HATCH in inspect.getsource(obj)
        }

        assert offered == ADVISORY_STAGES

    @pytest.mark.asyncio
    async def test_whitespace_alone_does_not_defeat_the_hatch(self) -> None:
        """`_extract_code` strips its result and the incoming code may not be,
        so a byte-identical reply could read as *changed* on a trailing newline
        — spending a repair round and losing the acceptance."""
        from loom.agents.checks import CheckContext, CheckPipeline
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent.__new__(WorkflowCodingAgent)
        agent._max_repair = 3
        agent._pipeline = CheckPipeline([])

        _, _, declined = await agent._repair_from(
            _Declining(),
            _SETTLED + "\n\n",
            _report_of("resolution"),
            CheckContext(spec="a spec"),
        )

        assert declined is True


#: The workflow one real run produced, line by line.
#:
#: Assembled from a list rather than a triple-quoted block so it needs no
#: backslashes: the first version of this fixture escaped its way into
#: source that did not parse, and source that does not parse yields *no
#: findings* — which reads as a pass on exactly the tests that use it.
FIXTURE = "\n".join([
    'from loom import Context, step, workflow',
    'from loom.toolsets.jira.tools import jira_search_issues',
    '',
    '',
    '@step',
    'async def format_overdue_report(issues) -> str:',
    '    if not issues:',
    '        return (',
    '            \'No overdue tickets mentioning "saas" were found \'',
    '            \'(searched all projects, due date in the past, text ~ "saas").\'',
    '        )',
    "    return 'report'",
    '',
    '',
    "@workflow(name='overdue_saas_tickets')",
    'async def overdue_saas_tickets_workflow(ctx: Context, input_data) -> str:',
    '    jql = (',
    '        \'due < now() AND due is not EMPTY AND text ~ "saas" \'',
    "        'ORDER BY due ASC'",
    '    )',
    '    issues = await ctx.step(jira_search_issues, jql, 200)',
    '    return await ctx.step(format_overdue_report, issues)',
    '',
])


class TestThePromptNamesWhatTheToolsAccept:
    """From a real run: the agent's *first* call was
    `call_read_operation("jira.jira_list_projects")`, which does not exist.

    It was not guessing. The prompt block printed a line labelled
    `Operations:` containing **function** names, two lines below a sentence
    explaining that an operation id looks like `messages.search`, and one line
    above an instruction to call `get_tool_contract("jira.<op_id>")`. The model
    used what the line labelled "Operations" said. It cost a turn on the first
    call of every run.
    """

    @pytest.fixture(autouse=True)
    def _catalog(self):
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        self.catalog = get_catalog()

    def test_every_operation_the_prompt_names_can_be_looked_up(self) -> None:
        """The invariant that was broken. Anything the prompt puts in front of
        the model under "Operations" has to be a name the tools take."""
        import re

        described = self.catalog.describe(detail="index")
        toolset = None
        unusable: list[str] = []
        for line in described.splitlines():
            header = re.match(r"### (\S+)", line.strip())
            if header:
                toolset = header.group(1)
            elif toolset and line.strip().startswith("Operations"):
                names = line.split(":", 1)[1]
                for name in (n.strip() for n in names.split(",")):
                    manifest = self.catalog.get(toolset)
                    if manifest is None or manifest.find_operation(name) is None:
                        unusable.append(f"{toolset}.{name}")

        assert unusable == [], (
            "the prompt names operations the tools cannot resolve: "
            f"{unusable[:6]}"
        )

    def test_the_jira_ids_the_run_needed_are_present(self) -> None:
        described = self.catalog.describe(detail="index")

        assert "projects.list" in described
        assert "fields.resolve" in described
        assert "jira.jira_list_projects" not in described

    def test_the_import_line_still_carries_the_function_names(self) -> None:
        """The two lines are complementary, not duplicates: Import is what
        generated *code* calls, Operations is what a *tool call* names."""
        described = self.catalog.describe(detail="index")

        assert "jira_list_projects" in described, "code still needs the symbol"

    def test_the_resolver_advice_names_paths_the_tool_accepts(self) -> None:
        """`_where_to_look` named `op.function` while the same sentence said to
        call it with `call_read_operation`, which takes `<toolset>.<op_id>`."""
        from loom.agents.stages import ResolutionStage

        advice = ResolutionStage(self.catalog)._where_to_look()
        paths = [
            token.strip()
            for token in advice.split("declare", 1)[-1].split("—")[0].split(",")
            if "." in token
        ]
        assert paths, "no resolvers named"

        for entry in paths:
            path = entry.split("(")[0].strip()
            toolset_id, _, op_id = path.partition(".")
            manifest = self.catalog.get(toolset_id)
            assert manifest is not None, path
            assert manifest.find_operation(op_id) is not None, path


class TestAWrongArgumentNameIsAnsweredNotJustReported:
    """The run's other guess: `fields.resolve` called with `name=` when the
    contract says `field_name=`. The reply was the raw TypeError plus a note
    about credentials, so the model spent a turn on `get_tool_contract` to
    learn a name the manifest already had."""

    @pytest.mark.asyncio
    async def test_a_signature_mismatch_lists_what_is_accepted(self) -> None:
        import json

        from loom.agents.coding_tools import _call_read_operation
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        reply = json.loads(
            await _call_read_operation(
                "jira.fields.resolve",
                {"name": "due date"},
                registry=get_catalog(),
                seen={},
            )
        )

        assert reply["accepts"] == ["field_name"]
        assert reply["required"] == ["field_name"]
        assert "credentials" not in reply["note"], (
            "a signature mismatch is not a credentials failure"
        )

    @pytest.mark.asyncio
    async def test_the_original_error_is_kept_verbatim(self) -> None:
        import json

        from loom.agents.coding_tools import _call_read_operation
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        reply = json.loads(
            await _call_read_operation(
                "jira.fields.resolve", {"name": "x"}, registry=get_catalog(), seen={}
            )
        )

        assert "unexpected keyword argument" in reply["error"]


class TestALookupReplyKeepsItsShape:
    """From a real run: the workflow was generated with `issues.result`, and
    smoke failed with `'Results' object has no attribute 'result'`.

    The model had not invented that. Over 8,000 characters the lookup tool
    returned `{"result": <the serialized payload, as a string>}` — the envelope
    nested inside itself, under the same key that otherwise holds the data, cut
    mid-token so it did not even parse. The model read `result.result` and
    wrote `.result` in the workflow.
    """

    @staticmethod
    async def _reply(rows: int, width: int = 120):
        import json
        import sys
        import types

        import loom.agents.coding_tools as ct
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
        from loom.toolsets.pagination import Results

        found = Results(
            [
                {"key": f"PA-{i}", "summary": "x" * width, "status": "To Do"}
                for i in range(rows)
            ]
        )
        found.complete = False
        found.total = 312

        module = types.ModuleType("stub_read_tools")

        async def stub_search(jql: str, max_results: int = 20):
            return found

        module.stub_search = stub_search
        sys.modules["stub_read_tools"] = module

        registry = ToolsetRegistry()
        registry.register(
            ToolsetManifest(
                id="stub",
                version="1.0.0",
                summary="s",
                tools_module="stub_read_tools",
                groups={
                    "issues": [
                        OperationSpec(
                            id="issues.search",
                            function="stub_search",
                            summary="search",
                            effect=EffectClass.READ,
                            input_schema={
                                "type": "object",
                                "properties": {"jql": {"type": "string"}},
                                "required": ["jql"],
                            },
                        )
                    ]
                },
            )
        )
        return json.loads(
            await ct._call_read_operation(
                "stub.issues.search", {"jql": "x"}, registry=registry, seen={}
            )
        )

    @pytest.mark.asyncio
    async def test_a_small_result_is_a_list(self) -> None:
        reply = await self._reply(rows=2)

        assert isinstance(reply["result"], list)

    @pytest.mark.asyncio
    async def test_an_oversized_result_is_still_a_list(self) -> None:
        """The shape must not depend on the size. It did, and the large case is
        the one a lookup meets most."""
        reply = await self._reply(rows=60)

        assert reply["truncated"] is True
        assert isinstance(reply["result"], list), "shape changed with size"

    @pytest.mark.asyncio
    async def test_the_envelope_is_not_nested_inside_itself(self) -> None:
        """The direct cause of `issues.result` reaching the workflow."""
        import json

        reply = await self._reply(rows=60)

        assert chr(34) + "result" + chr(34) not in json.dumps(reply["result"])

    @pytest.mark.asyncio
    async def test_every_row_shown_is_whole(self) -> None:
        """Cutting the rendering left half a row and unparseable JSON; dropping
        rows leaves every row that is shown readable."""
        reply = await self._reply(rows=60)

        assert reply["result"]
        assert all(
            set(row) == {"key", "summary", "status"} for row in reply["result"]
        )

    @pytest.mark.asyncio
    async def test_it_says_how_many_rows_it_left_out(self) -> None:
        reply = await self._reply(rows=60)

        assert "left out" in reply["note"]
        assert reply["coverage"]["total"] == 312, "coverage survives truncation"


class TestMechanicalFixesCostNoModelCall:
    """A repair round costs a model call, several seconds, and — because a
    reply that changes anything is a reply that did not *decline* — the ability
    to accept an advisory finding. Spending one on deleting an unused import is
    a bad trade twice over, and the SDK manufactures that finding by telling
    the model to import `Retry` unconditionally."""

    def test_the_prompts_own_mandated_import_is_removed_when_unused(self) -> None:
        from loom.agents.tidy import tidy

        source = chr(10).join([
            "from loom import Context, Retry, step, workflow",
            "",
            "",
            "@workflow(name='w')",
            "async def w(ctx: Context, i=None) -> int:",
            "    return 1",
            "",
        ])

        result = tidy(source)

        assert "Retry" not in result.code
        assert "Context" in result.code and "workflow" in result.code
        assert result.changed

    def test_a_models_own_unused_import_goes_too(self) -> None:
        from loom.agents.tidy import tidy

        source = chr(10).join(["from datetime import date", "", "x = 1", ""])

        assert "datetime" not in tidy(source).code

    def test_code_that_needs_nothing_is_returned_unchanged(self) -> None:
        from loom.agents.tidy import tidy

        source = chr(10).join(["import os", "", "x = os.getcwd()", ""])

        assert tidy(source).code == source

    def test_only_behaviour_preserving_rules_are_applied(self) -> None:
        """The licence for rewriting what a model wrote is that the change
        cannot alter behaviour. Anything needing judgement stays a finding."""
        from loom.agents.tidy import TIDY_RULES

        assert set(TIDY_RULES) <= {"F401", "F541", "UP035", "UP008"}

    def test_generate_tidies_before_the_pipeline_sees_the_code(self) -> None:
        import inspect

        from loom.agents.coding_agent import WorkflowCodingAgent

        source = inspect.getsource(WorkflowCodingAgent.generate)

        assert source.index("tidy(code)") < source.index("self._pipeline.run")


class TestAnExhaustedRunReportsWhatItSpent:
    """A generation that burned twenty-two turns and four minutes came back
    reading `Tokens in 0`, `Tokens out 0`, `0 tool calls`.

    The accounting lives in a local inside the turn loop, and
    `UsageLimitExceeded` unwound straight past it — so the one number that says
    whether raising the budget is affordable was the number being discarded, at
    exactly the moment somebody is deciding whether to raise it. And "0 tool
    calls" reads as "it did nothing" rather than "it did a great deal and never
    converged", which want opposite responses.
    """

    @pytest.mark.asyncio
    async def test_the_turn_loop_attaches_what_it_spent(self) -> None:
        from loom.agents.agent import Agent
        from loom.agents.limits import UsageLimits
        from loom.agents.messages import ToolCall
        from loom.agents.runner import BuiltInAgentRuntime
        from loom.agents.tools import tool
        from loom.core.exceptions import UsageLimitExceeded
        from loom.testing.mock import MockModelProvider, mock_response

        @tool
        async def look(q: str) -> str:
            """Look something up.

            Args:
                q: anything.
            """
            return "nothing"

        # A loop that keeps calling and never produces a final answer — the
        # shape that exhausts a budget.
        spinning = [
            mock_response(
                tool_calls=[ToolCall(id=str(i), name="look", arguments={"q": "x"})],
                usage=Usage(input_tokens=1_000, output_tokens=50),
            )
            for i in range(6)
        ]
        agent = Agent(
            name="a",
            model=MockModelProvider(responses=spinning),
            tools=[look],
            limits=UsageLimits(max_turns=3),
        )

        with pytest.raises(UsageLimitExceeded) as caught:
            await BuiltInAgentRuntime(agent=agent).execute("go")

        assert caught.value.usage is not None
        assert caught.value.usage.input_tokens > 0, "spend was discarded"
        assert caught.value.tool_calls, "tool calls were discarded"

    @pytest.mark.asyncio
    async def test_the_session_charges_a_call_that_ran_out(self) -> None:
        from loom.agents.generation import CodingSession, GenerationBudget
        from loom.core.exceptions import UsageLimitExceeded

        class _Exhausting:
            name = "coder"
            limits = None

            async def __call__(self, prompt, *, context=None, **kw):
                failure = UsageLimitExceeded(
                    "out of turns", limit_name="max_turns", limit=22, actual=23
                )
                failure.usage = Usage(input_tokens=285_305, output_tokens=14_323)
                failure.turns = 22
                raise failure

        budget = GenerationBudget(max_turns=30)
        session = CodingSession(_Exhausting(), budget)

        with pytest.raises(UsageLimitExceeded):
            await session.ask("a spec")

        assert budget.spent.input_tokens == 285_305
        assert budget.spent.output_tokens == 14_323
        assert budget.turns_used == 22

    def test_a_call_that_failed_for_another_reason_charges_nothing(self) -> None:
        """Only a failure that reports what it spent is charged. Inventing a
        figure for one that does not would be worse than reporting zero."""
        import asyncio

        from loom.agents.generation import CodingSession, GenerationBudget

        class _Broken:
            name = "coder"
            limits = None

            async def __call__(self, prompt, *, context=None, **kw):
                raise RuntimeError("connection reset")

        budget = GenerationBudget(max_turns=30)
        session = CodingSession(_Broken(), budget)

        with pytest.raises(RuntimeError):
            asyncio.run(session.ask("a spec"))

        assert budget.spent.input_tokens == 0


class TestResolutionFlagsQueriesNotProse:
    """Both false positives came from one real run: "List tickets that are
    passed due date in saas", against a live Jira.

    The stage scanned *every* string literal and then flagged *any* spec word
    inside it, so it reported two errors where there was at most one.
    """

    SPEC = "List tickets that are passed due date in saas"

    CODE = FIXTURE

    def test_the_fixture_is_real_python(self) -> None:
        """Guards the guard. Everything else here asserts an absence, and an
        unparseable fixture produces absences for free."""
        import ast

        ast.parse(self.CODE)

    async def _findings(self):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import ResolutionStage

        result = await ResolutionStage().run(
            self.CODE, CheckContext(spec=self.SPEC)
        )
        return result.issues

    @pytest.mark.asyncio
    async def test_the_workflows_own_explanation_is_not_a_query(self) -> None:
        """Rung 4 says: keep the text match and *say so in what the workflow
        returns*. The model did, quoting the JQL for the reader — and the check
        flagged that sentence, punishing it for following the instruction."""
        findings = await self._findings()

        assert not any("No overdue tickets" in i.message for i in findings)

    @pytest.mark.asyncio
    async def test_a_field_name_is_not_an_entity_to_resolve(self) -> None:
        """`due` is in the spec ("passed **due** date") and is also a JQL field.
        Flagging it told the model to go and look up the word `due`."""
        findings = await self._findings()

        assert not any("'due'" in i.message for i in findings)

    @pytest.mark.asyncio
    async def test_the_real_guess_is_still_caught(self) -> None:
        """Narrowing must not blind it: `text ~ "saas"` in a query that reaches
        a search really is the spec's vocabulary standing in for the system's."""
        findings = await self._findings()

        assert len(findings) == 1
        assert "'saas'" in findings[0].message

    @pytest.mark.asyncio
    async def test_the_run_ends_clean_once_the_model_stands_by_it(self) -> None:
        """End to end, as the cookbook takes it: one finding, the model declines
        because it checked every namespace, the finding is accepted, and the
        code runs."""
        from loom.agents.checks import CheckContext, CheckPipeline
        from loom.agents.coding_agent import (
            CodingOutput,
            CodingResult,
            WorkflowCodingAgent,
            _settle_advisories,
        )
        from loom.agents.stages import ResolutionStage

        code = self.CODE
        pipeline = CheckPipeline([ResolutionStage()])
        report = await pipeline.run(code, CheckContext(spec=self.SPEC))
        assert report.errors, "the finding must be raised before it is accepted"

        class _StandsBy:
            async def ask(self, prompt: str):
                return type("R", (), {"output": CodingOutput(code=code)})()

        agent = WorkflowCodingAgent.__new__(WorkflowCodingAgent)
        agent._max_repair = 3
        agent._pipeline = pipeline
        _, _, declined = await agent._repair_from(
            _StandsBy(), code, report, CheckContext(spec=self.SPEC)
        )
        settled = _settle_advisories(list(report.issues), declined=declined)

        assert declined is True
        assert CodingResult(code=code, issues=settled).is_clean

    def test_only_a_literal_that_reaches_a_call_is_a_query(self) -> None:
        from loom.agents.stages import _query_literals

        found = _query_literals(
            "def f():\n"
            "    return 'text ~ prose'\n"
            "\n"
            "def g():\n"
            "    q = 'text ~ query'\n"
            "    return search(q)\n"
        )

        assert "text ~ query" in found
        assert "text ~ prose" not in found

    def test_only_the_operand_of_the_match_operator_counts(self) -> None:
        from loom.agents.stages import _fuzzy_operands

        operands = _fuzzy_operands(
            'due < now() AND due is not EMPTY AND text ~ "saas" ORDER BY due ASC',
            ("~", "contains", "like ", "in text"),
        )

        assert [m.word for m in operands] == ["saas"]
        # The rest of the query is kept as scope, never as an operand: `due` is
        # a JQL field name and a spec word at once, and reporting it as matched
        # on is what told a model to look up the word "due".
        assert "due" in operands[0].scope


class TestResolutionReadsAQuerysScope:
    """A match scoped to a namespace is not the guess an unscoped one is.

    Reading only the operand made ``summary ~ "saas"`` and ``issuetype = Epic
    AND summary ~ "saas"`` the same input. The second is the ladder followed —
    pick a namespace, search it by name — and for an entity whose service
    exposes no other lookup it is the *only* lookup: a Jira epic is an issue,
    so there is no epic endpoint to call instead.

    That mattered because it left the finding with no passing state. The
    escape hatch is "nothing bears that name", which is false when something
    does, so every repair round rewrote correct code into another spelling of
    itself. Severity is the fix: an error drives repair, a warning does not.
    """

    SPEC = "List tickets that are passed due date in saas epic"

    @staticmethod
    def _registry():
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        return registry

    @staticmethod
    def _code(jql: str) -> str:
        return (
            "async def f(ctx):\n"
            f"    jql = {jql!r}\n"
            "    return await ctx.step(search, jql)\n"
        )

    async def _run(self, jql: str, *, registry: bool = True):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import ResolutionStage

        stage = ResolutionStage(self._registry() if registry else None)
        return await stage.run(self._code(jql), CheckContext(spec=self.SPEC))

    async def test_an_unscoped_match_is_still_an_error(self) -> None:
        result = await self._run('summary ~ "saas"')

        assert [i.severity for i in result.issues] == ["error"]

    async def test_a_namespace_scoped_match_is_a_warning(self) -> None:
        """The whole point: warnings do not reach ``report.errors``, so the
        repair loop terminates instead of rewriting correct code."""
        result = await self._run('issuetype = Epic AND summary ~ "saas"')

        assert [i.severity for i in result.issues] == ["warning"]
        assert "namespace search" in result.issues[0].message

    async def test_the_scope_may_sit_on_either_side_of_the_clause(self) -> None:
        """``project = PA`` names the namespace on the left, ``issuetype =
        Epic`` on the right. Which one does is the query language's business."""
        result = await self._run('project = PA AND summary ~ "saas"')

        assert [i.severity for i in result.issues] == ["warning"]

    async def test_a_scope_that_is_not_a_namespace_does_not_excuse_it(self) -> None:
        """``status`` is a field, not a namespace, so this is still a guess —
        and a check that accepted any adjacent clause would excuse everything.
        """
        result = await self._run('status = Open AND text ~ "saas"')

        assert [i.severity for i in result.issues] == ["error"]

    async def test_one_operand_cannot_scope_another(self) -> None:
        """Scope is the query minus *every* operand clause, not minus this one.

        Taking "everything but this clause" per operator would let one
        unresolved guess excuse the next: ``summary ~ "project alpha"`` puts
        the word ``project`` in the scope of ``text ~ "saas"``, and the query
        would read as scoped to a project by a word nothing looked up.
        """
        result = await self._run('summary ~ "project alpha" AND text ~ "saas"')

        assert [i.severity for i in result.issues] == ["error"]

    async def test_a_namespace_named_in_a_field_position_still_counts(self) -> None:
        """The other direction of the same rule — removing operand clauses
        must not remove a genuine scope that happens to share a word."""
        result = await self._run('project = PA AND summary ~ "saas"')

        assert [i.severity for i in result.issues] == ["warning"]

    async def test_a_resolved_identifier_is_clean(self) -> None:
        result = await self._run("parentEpic = PA-1844 AND duedate < now()")

        assert result.issues == []

    async def test_without_a_registry_it_falls_back_to_the_old_behaviour(self) -> None:
        """No registry means no declared namespaces, so nothing is excused.

        Failing that way round is deliberate: the fallback errors, and an
        error the model cannot act on is the loop this fix exists to end — so
        the fallback must never be reached by accident, only by there being no
        registry at all.
        """
        result = await self._run(
            'issuetype = Epic AND summary ~ "saas"', registry=False
        )

        assert [i.severity for i in result.issues] == ["error"]

    def test_the_namespaces_come_from_declarations_not_a_word_list(self) -> None:
        """Nothing in the agent layer names a namespace, or a vendor.

        A hardcoded list is the failure ``opaque_ids`` already avoids: which
        namespaces a service has is the service's own statement. A toolset
        declaring ``resolves="board"`` teaches this check about boards with no
        change to ``stages.py``.
        """
        from loom.agents.stages import ResolutionStage

        assert ResolutionStage(self._registry())._namespaces() >= {
            "epic",
            "project",
            "user",
            "field",
        }
        assert ResolutionStage(None)._namespaces() == frozenset()

    def test_the_agent_is_pointed_at_a_resolver_that_exists(self) -> None:
        """The other half of the deadlock. The message used to enumerate
        namespaces — project, board, epic, label — that the toolset shipped no
        resolver for, so the advice sent the model somewhere it could not go
        and then flagged the one route that worked.
        """
        from loom.agents.stages import ResolutionStage

        where = ResolutionStage(self._registry())._where_to_look()

        assert "jira.issues.resolve_epic (epic)" in where
        assert "jira.projects.resolve (project)" in where


# ---------------------------------------------------------------------------
# H2 — one token convention, and prices that exist
# ---------------------------------------------------------------------------


class TestCostAccounting:
    """`estimate_cost` subtracted cache reads from an input count that, for
    Anthropic, already excluded them: 500 real input tokens beside 20,000 cache
    reads were billed as zero. Cache *writes* were never counted at all."""

    def test_anthropic_shaped_usage_is_priced_correctly(self) -> None:
        from loom.agents.models import estimate_cost

        # 500 fresh, 20,000 read from cache — as `Usage` defines it, the total.
        usage = Usage(
            input_tokens=20_500, cached_input_tokens=20_000, output_tokens=1_000
        )
        expected = (500 * 2.00 + 20_000 * 2.00 * 0.10 + 1_000 * 10.00) / 1_000_000

        assert estimate_cost("claude-sonnet-5", usage) == pytest.approx(expected)

    def test_a_usage_from_before_normalisation_is_read_fail_safe(self) -> None:
        """A stored `Usage`, or third-party code written against Anthropic's own
        field names, reports `input_tokens` *excluding* cache traffic — so its
        total is smaller than its parts.

        Clamping is the tempting reading and it fails *open*: it drops the fresh
        tokens to zero and undercounts, which is the wrong direction for a number
        that backs `max_cost_usd`. Read as fresh-plus-cache, it comes out at the
        same figure the normalised shape does.
        """
        from loom.agents.models import estimate_cost

        legacy = Usage(
            input_tokens=500, cached_input_tokens=20_000, output_tokens=1_000
        )
        normalised = Usage(
            input_tokens=20_500, cached_input_tokens=20_000, output_tokens=1_000
        )

        assert estimate_cost("claude-sonnet-5", legacy) == pytest.approx(
            estimate_cost("claude-sonnet-5", normalised)
        )

    def test_cache_writes_cost_more_than_fresh_tokens(self) -> None:
        from loom.agents.models import estimate_cost

        fresh = Usage(input_tokens=1_000)
        written = Usage(input_tokens=1_000, cache_write_tokens=1_000)

        assert estimate_cost("claude-sonnet-5", written) > estimate_cost(
            "claude-sonnet-5", fresh
        )

    def test_cache_reads_cost_less_than_fresh_tokens(self) -> None:
        from loom.agents.models import estimate_cost

        fresh = Usage(input_tokens=1_000)
        read = Usage(input_tokens=1_000, cached_input_tokens=1_000)

        assert estimate_cost("claude-sonnet-5", read) < estimate_cost(
            "claude-sonnet-5", fresh
        )

    def test_each_vendor_gets_its_own_cache_economics(self) -> None:
        """The flat 0.25 this replaced was right for no vendor."""
        from loom.agents.models import CACHE_RATES

        assert CACHE_RATES["claude"].read == 0.10
        assert CACHE_RATES["claude"].write > 1.0
        assert CACHE_RATES["gpt"].read == 0.50

    def test_an_unknown_vendor_gets_no_discount(self) -> None:
        """Erring towards more expensive keeps a budget honest."""
        from loom.agents.models import DEFAULT_CACHE_RATES

        assert DEFAULT_CACHE_RATES.read == 1.0
        assert DEFAULT_CACHE_RATES.write == 1.0

    def test_the_flagship_models_are_priced(self) -> None:
        """`claude-opus-5` returned 0.0, which silently disables every dollar
        budget downstream."""
        from loom.agents.models import is_priced

        for model in (
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "gpt-5.6-terra",
            "gemini-2.5-pro",
        ):
            assert is_priced(model), model

    def test_longest_prefix_still_wins(self) -> None:
        from loom.agents.models import estimate_cost

        usage = Usage(input_tokens=1_000_000)
        mini = estimate_cost("gpt-4.1-mini-2025-04-14", usage)
        family = estimate_cost("gpt-4.1-2025-04-14", usage)

        assert mini < family

    def test_usage_addition_carries_the_new_field(self) -> None:
        first = Usage(input_tokens=10, cache_write_tokens=3)
        first.add(Usage(input_tokens=5, cache_write_tokens=2))

        assert first.cache_write_tokens == 5


class TestEveryProviderReportsTheSameShape:
    """A conformance check rather than three separate ones: the point of the
    port is that nothing downstream should have to know which vendor answered.
    `requests` was set by OpenAI and not by Anthropic, so `max_requests` could
    never trip on Claude."""

    def test_anthropic_reports_a_request_and_a_total(self) -> None:
        from loom.agents.providers.anthropic_provider import _parse_response

        raw = _AnthropicRaw(
            input_tokens=100, cache_read=40, cache_creation=10, output_tokens=7
        )
        response = _parse_response(raw)

        assert response.usage.requests == 1
        assert response.usage.input_tokens == 150
        assert response.usage.cached_input_tokens == 40
        assert response.usage.cache_write_tokens == 10

    def test_openai_reports_a_request_and_a_total(self) -> None:
        from loom.agents.providers.openai_provider import _parse_response

        response = _parse_response(_OpenAIRaw())

        assert response.usage.requests == 1
        assert response.usage.input_tokens == 150
        assert response.usage.cached_input_tokens == 40


class _AnthropicUsage:
    def __init__(self, input_tokens, cache_read, cache_creation, output_tokens):
        self.input_tokens = input_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation
        self.output_tokens = output_tokens


class _AnthropicRaw:
    def __init__(self, **kw):
        self.content = []
        self.usage = _AnthropicUsage(**kw)
        self.stop_reason = "end_turn"
        self.model = "claude-sonnet-5"


class _OpenAIDetails:
    cached_tokens = 40


class _OpenAIUsage:
    prompt_tokens = 150
    completion_tokens = 7
    prompt_tokens_details = _OpenAIDetails()
    completion_tokens_details = None


class _OpenAIMessage:
    content = "hi"
    tool_calls = None


class _OpenAIChoice:
    message = _OpenAIMessage()
    finish_reason = "stop"


class _OpenAIRaw:
    choices: ClassVar[list] = [_OpenAIChoice()]
    usage = _OpenAIUsage()
    model = "gpt-5.6-terra"


# ---------------------------------------------------------------------------
# H3 — parallel tool calls reach Anthropic in a shape it accepts
# ---------------------------------------------------------------------------


class TestAnthropicMessageShape:
    """The runner appends one TOOL message per call, and each became its own
    user turn — so a turn with two parallel tool calls produced two consecutive
    user messages. Parallel tool use is the normal case for an agent, and
    `claude-sonnet-5` is the coding agent's default model."""

    def test_two_tool_results_share_one_user_turn(self) -> None:
        from loom.agents.providers.anthropic_provider import _split_messages

        converted, _ = _split_messages([
            system("s"),
            user("go"),
            assistant(
                content=None,
                tool_calls=[
                    ToolCall(id="a", name="x", arguments={}),
                    ToolCall(id="b", name="y", arguments={}),
                ],
            ),
            tool_result("a", "ra", name="x"),
            tool_result("b", "rb", name="y"),
        ])

        assert [m["role"] for m in converted] == ["user", "assistant", "user"]
        assert len(converted[-1]["content"]) == 2

    def test_roles_alternate_throughout(self) -> None:
        from loom.agents.providers.anthropic_provider import _split_messages

        converted, _ = _split_messages([
            user("one"),
            assistant(content=None, tool_calls=[ToolCall(id="a", name="x", arguments={})]),
            tool_result("a", "ra", name="x"),
            assistant(content=None, tool_calls=[
                ToolCall(id="b", name="y", arguments={}),
                ToolCall(id="c", name="z", arguments={}),
            ]),
            tool_result("b", "rb", name="y"),
            tool_result("c", "rc", name="z"),
            assistant(content="done"),
        ])

        import itertools

        roles = [m["role"] for m in converted]
        assert all(a != b for a, b in itertools.pairwise(roles)), roles

    def test_a_real_user_turn_does_not_absorb_a_tool_result(self) -> None:
        """Only a turn that is itself tool results absorbs another. A user turn
        carrying text is somebody's actual message."""
        from loom.agents.providers.anthropic_provider import _split_messages

        converted, _ = _split_messages([
            user("please do it"),
            tool_result("a", "ra", name="x"),
        ])

        assert len(converted) == 2
        assert converted[0]["content"] == "please do it"


# ---------------------------------------------------------------------------
# M5 — the turn loop
# ---------------------------------------------------------------------------


class TestToolCallsInOneTurnRunConcurrently:
    """They are independent by construction — the model asked for all of them
    before seeing any answer — so running them one after another spent the sum
    of their latencies for no reason."""

    @pytest.mark.asyncio
    async def test_four_slow_tools_take_one_tool_of_time(self) -> None:
        from loom.agents.agent import Agent
        from loom.agents.runner import BuiltInAgentRuntime
        from loom.agents.tools import tool

        @tool
        async def wait_a_bit(tag: str) -> str:
            """Sleep briefly.

            Args:
                tag: anything.
            """
            await asyncio.sleep(0.1)
            return tag

        agent = Agent(name="a", model=None, tools=[wait_a_bit])
        runtime = BuiltInAgentRuntime(agent=agent)
        calls = [
            ToolCall(id=str(i), name="wait_a_bit", arguments={"tag": str(i)})
            for i in range(4)
        ]

        started = asyncio.get_running_loop().time()
        outcomes = await runtime._dispatch_turn(
            calls,
            agent=agent,
            tool_map={"wait_a_bit": wait_a_bit},
            context=None,
            turn=1,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert [o.raw for o in outcomes] == ["0", "1", "2", "3"]
        assert elapsed < 0.3, f"took {elapsed:.2f}s — still serial?"

    @pytest.mark.asyncio
    async def test_results_come_back_in_call_order_not_completion_order(
        self,
    ) -> None:
        from loom.agents.agent import Agent
        from loom.agents.runner import BuiltInAgentRuntime
        from loom.agents.tools import tool

        @tool
        async def delayed(tag: str, ms: int) -> str:
            """Sleep then answer.

            Args:
                tag: what to return.
                ms: how long to wait.
            """
            await asyncio.sleep(ms / 1000)
            return tag

        agent = Agent(name="a", model=None, tools=[delayed])
        runtime = BuiltInAgentRuntime(agent=agent)
        calls = [
            ToolCall(id="1", name="delayed", arguments={"tag": "slow", "ms": 80}),
            ToolCall(id="2", name="delayed", arguments={"tag": "fast", "ms": 1}),
        ]

        outcomes = await runtime._dispatch_turn(
            calls, agent=agent, tool_map={"delayed": delayed}, context=None, turn=1
        )

        assert [o.raw for o in outcomes] == ["slow", "fast"]

    @pytest.mark.asyncio
    async def test_one_failing_tool_does_not_discard_its_siblings(self) -> None:
        from loom.agents.agent import Agent
        from loom.agents.runner import BuiltInAgentRuntime
        from loom.agents.tools import tool

        @tool
        async def maybe(tag: str) -> str:
            """Fail for one tag.

            Args:
                tag: which tag.
            """
            if tag == "boom":
                raise ValueError("nope")
            return tag

        agent = Agent(name="a", model=None, tools=[maybe])
        runtime = BuiltInAgentRuntime(agent=agent)
        calls = [
            ToolCall(id="1", name="maybe", arguments={"tag": "boom"}),
            ToolCall(id="2", name="maybe", arguments={"tag": "fine"}),
        ]

        outcomes = await runtime._dispatch_turn(
            calls, agent=agent, tool_map={"maybe": maybe}, context=None, turn=1
        )

        assert "Tool error: ValueError: nope" in outcomes[0].raw
        assert outcomes[1].raw == "fine"

    @pytest.mark.asyncio
    async def test_an_unknown_tool_is_answered_not_raised(self) -> None:
        from loom.agents.agent import Agent
        from loom.agents.runner import BuiltInAgentRuntime

        agent = Agent(name="a", model=None, tools=[])
        runtime = BuiltInAgentRuntime(agent=agent)

        outcomes = await runtime._dispatch_turn(
            [ToolCall(id="1", name="ghost", arguments={})],
            agent=agent,
            tool_map={},
            context=None,
            turn=1,
        )

        assert "Unknown tool 'ghost'" in outcomes[0].raw

    @pytest.mark.asyncio
    async def test_a_workflow_context_supplies_the_concurrency_primitive(
        self,
    ) -> None:
        """Inside a workflow, `ctx.gather` is used rather than
        `asyncio.gather`, because a tool that journals must take its paths from
        a branch-local scope. Using the raw primitive would reintroduce the
        ordering defect one layer up, in code no workflow author wrote."""
        from loom.agents.agent import Agent
        from loom.agents.executor import AgentContext
        from loom.agents.runner import BuiltInAgentRuntime
        from loom.agents.tools import tool

        used: list[str] = []

        class _Ctx:
            async def gather(self, *aws, return_exceptions=False):
                used.append("ctx.gather")
                return await asyncio.gather(*aws, return_exceptions=return_exceptions)

        @tool
        async def echo(tag: str) -> str:
            """Echo.

            Args:
                tag: anything.
            """
            return tag

        agent = Agent(name="a", model=None, tools=[echo])
        runtime = BuiltInAgentRuntime(agent=agent)
        await runtime._dispatch_turn(
            [ToolCall(id="1", name="echo", arguments={"tag": "x"})],
            agent=agent,
            tool_map={"echo": echo},
            context=AgentContext(workflow_ctx=_Ctx()),
            turn=1,
        )

        assert used == ["ctx.gather"]


class TestHistoryIsBoundedByTokensToo:
    """A count of messages is a poor proxy for context: forty ordinary turns
    are small, and forty turns each carrying a page of tool output are not."""

    def test_a_token_ceiling_drops_the_oldest_turns(self) -> None:
        from loom.agents.memory import trim_history

        messages = [user("x" * 400) for _ in range(10)]

        trimmed = trim_history(messages, max_messages=10, max_tokens=200)

        assert len(trimmed) < len(messages)
        assert trimmed[-1] is messages[-1], "the newest turn is never dropped"

    def test_no_ceiling_is_the_previous_behaviour(self) -> None:
        from loom.agents.memory import trim_history

        messages = [user("x" * 4000) for _ in range(5)]

        assert trim_history(messages, max_messages=10) == messages

    def test_the_system_prompt_is_kept(self) -> None:
        from loom.agents.memory import trim_history
        from loom.agents.messages import Role

        messages = [system("keep me"), *[user("x" * 400) for _ in range(10)]]

        trimmed = trim_history(messages, max_messages=10, max_tokens=100)

        assert trimmed[0].role is Role.SYSTEM


class TestNativeStructuredOutputIsReachable:
    """The runner passed `supports_native=False` literally, so `OutputMode.NATIVE`
    was unreachable and OpenAI's `_response_format` — which exists and is
    correct — was dead code."""

    def test_a_provider_can_declare_it(self) -> None:
        from loom.agents.models import supports_native_output

        class _Native:
            model_name = "x"
            supports_native_output = True

        class _Not:
            model_name = "x"

        assert supports_native_output(_Native())
        assert not supports_native_output(_Not())

    def test_shipped_providers_leave_it_off(self) -> None:
        """Turning it on changes what the model returns, and that is a decision
        to make against a live API rather than in a refactor."""
        from loom.agents.models import supports_native_output
        from loom.agents.providers import anthropic_provider, openai_provider

        for module in (anthropic_provider, openai_provider):
            provider = next(
                getattr(module, n)
                for n in dir(module)
                if n.endswith("Provider") and not n.startswith("_")
            )
            instance = provider.__new__(provider)
            assert not supports_native_output(instance)


# ---------------------------------------------------------------------------
# M3 — finding the operation, not just the toolset
# ---------------------------------------------------------------------------


class TestOperationSearch:
    """Toolset-level search answered "is there a Jira integration"; the next
    question is "which of its forty operations transitions an issue", and only
    `show_toolset` answered that — by listing all forty."""

    @pytest.fixture(autouse=True)
    def _catalog(self):
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        self.catalog = get_catalog()

    def test_it_finds_the_operation_for_a_described_task(self) -> None:
        found = self.catalog.search_operations("transition an issue to done")

        assert found
        assert found[0].op_id == "issues.transition"
        assert found[0].toolset_id == "jira"

    def test_it_finds_an_email_send(self) -> None:
        found = self.catalog.search_operations("send an email")

        assert any(m.toolset_id == "gmail" for m in found[:3])

    def test_a_match_carries_what_code_has_to_write(self) -> None:
        found = self.catalog.search_operations("transition an issue")

        assert found[0].import_line.startswith("from ")
        assert found[0].effect

    def test_an_import_line_names_one_function_not_forty(self) -> None:
        """A match on one operation should not hand the model the whole toolset."""
        found = self.catalog.search_operations("transition an issue")

        assert found[0].import_line.count(",") == 0

    def test_it_can_be_narrowed_to_one_toolset(self) -> None:
        found = self.catalog.search_operations("search", toolset_id="jira")

        assert found
        assert all(m.toolset_id == "jira" for m in found)

    def test_no_match_is_an_empty_list_not_an_error(self) -> None:
        assert self.catalog.search_operations("xyzzy plugh frobnicate") == []

    def test_scoring_is_normalised_for_document_length(self) -> None:
        """An unnormalised count ranked whichever toolset had the most prose —
        Salesforce outranking DuckDuckGo for "search the web" is not a relevance
        judgement."""
        ranked = [card.toolset_id for card in self.catalog.search("search the web")]

        assert set(ranked[:3]) & {"exa", "tavily", "duckduckgo"}

    def test_stopwords_do_not_dilute_a_query(self) -> None:
        from loom.toolsets.catalog import _terms

        assert _terms("send an email to the user") == ["send", "email", "user"]

    def test_the_agent_is_given_the_tool(self) -> None:
        from loom.agents.coding_tools import build_coding_tools

        names = {t.name for t in build_coding_tools()}

        assert "search_operations" in names

    def test_the_prompt_tells_the_model_when_to_use_it(self) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "search_operations" in DEFAULT_SYSTEM_PROMPT
