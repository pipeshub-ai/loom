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
