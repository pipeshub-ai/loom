"""The authoring loop's return path: what the run produced, back to the model.

The pipeline could always tell whether generated code *ran*. It could never
tell whether the code *answered*, because ``smoke_run`` recorded the output and
only ``ReplayStage`` read it — and that stage asks whether two runs are equal,
never whether either makes sense.

The failure that produced these tests: a workflow asked to "collect every
visible form control" returned ``{"field_count": 0, "fields": []}`` against a
real page, and passed every gate. It compiled, imported, ran, terminated
``completed``, and replayed identically. Nothing was wrong with the code as
written; the selector was wrong for the page, and no check in the system was
looking at the answer.
"""

from __future__ import annotations

import pathlib

import pytest

from loom.agents.checks import CheckContext, CheckPipeline, PipelineReport
from loom.agents.coding_agent import _repair_prompt
from loom.agents.stages import (
    CataloguePreferenceStage,
    OutcomeStage,
    ProjectionStage,
    SmokeStage,
)
from loom.agents.validator import CodeIssue

# The shape of the real failure, reduced to something that needs no network:
# a workflow that looks for things, finds none, and says so cheerfully.
FINDS_NOTHING = '''
from loom import Context, step, workflow


@step
async def read_controls(url: str) -> list:
    """Every native form control on the page. There are none: the page builds
    its inputs out of divs, which is what made the real bug invisible."""
    return []


@workflow(name="inspector")
async def inspector(ctx: Context, url: str) -> dict:
    """Describe a form."""
    fields = await ctx.step(read_controls, url)
    return {"url": url, "field_count": len(fields), "fields": fields}
'''

FINDS_SOMETHING = FINDS_NOTHING.replace(
    "    return []", '    return [{"name": "guests", "type": "select"}]'
)

CALLS_A_NODE = """
from loom import Context, workflow


@workflow(name="fetcher")
async def fetcher(ctx: Context, url: str) -> dict:
    \"\"\"Fetch a URL through a catalogued node.\"\"\"
    result = await ctx.node("io.http_request", {"url": url, "method": "GET"})
    return {"status": result.status}
"""

BY_HAND = """
from loom import Context, step, workflow
import httpx


@step
async def fetch(url: str) -> dict:
    \"\"\"Fetch a URL the long way round.\"\"\"
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()


@workflow(name="by_hand")
async def by_hand(ctx: Context, url: str) -> dict:
    \"\"\"Fetch it.\"\"\"
    return await ctx.step(fetch, url)
"""

WANTS_EVERYTHING = (
    "Inspect a booking page. Find every visible form control and return each "
    "one with its name and type."
)

WANTS_ONE_THING = (
    "Inspect a booking page and return the page title, plus whatever form "
    "controls happen to be present."
)


def context(spec: str, **extra) -> CheckContext:
    return CheckContext(workflow_input="https://example.invalid/booking", spec=spec, **extra)


async def outcome_of(code: str, spec: str) -> tuple[PipelineReport, list[CodeIssue]]:
    """Run smoke, then the stage that reads it — the real ordering."""
    report = await CheckPipeline([SmokeStage(), OutcomeStage()]).run(code, context(spec))
    result = report.result("outcome")
    assert result is not None, report.summary
    return report, result.issues


class TestASilentZeroIsCaught:
    """The regression this whole phase exists to prevent."""

    async def test_an_empty_result_against_a_universal_spec_is_reported(self) -> None:
        report, issues = await outcome_of(FINDS_NOTHING, WANTS_EVERYTHING)

        smoke = report.result("smoke")
        assert smoke is not None and smoke.ok, "the code runs; that was never in doubt"
        assert [i.category for i in issues] == ["outcome"]
        assert "fields" in issues[0].message

    async def test_it_drives_a_repair(self) -> None:
        """An error, not a warning, and the distinction is the whole point: the
        repair loop runs on ``report.errors``, so a warning would be a finding
        nobody ever sees."""
        report, _ = await outcome_of(FINDS_NOTHING, WANTS_EVERYTHING)

        assert report.errors

    async def test_it_does_not_stop_the_stages_after_it(self) -> None:
        """Non-blocking: an unconvincing answer is still an answer, and replay
        has a different question to ask about it."""
        assert OutcomeStage().blocking is False


class TestItStaysQuiet:
    """A stage that cries wolf is one people switch off. Two conditions have to
    coincide, the same discipline ``CoverageStage`` uses."""

    async def test_a_full_result_says_nothing(self) -> None:
        _, issues = await outcome_of(FINDS_SOMETHING, WANTS_EVERYTHING)

        assert issues == []

    async def test_a_spec_that_asked_for_no_such_thing_says_nothing(self) -> None:
        """Empty is very often the right answer. Only a spec that asked for
        completeness makes an empty collection worth questioning."""
        _, issues = await outcome_of(FINDS_NOTHING, WANTS_ONE_THING)

        assert issues == []

    async def test_an_invented_input_is_not_judged(self) -> None:
        """When the harness made the input up from a type annotation, an empty
        result says nothing about the code — the same reason ``SmokeStage``
        calls that run unverifiable rather than failed."""
        from loom.agents.smoke import SmokeResult

        report = PipelineReport()
        report.results.append(
            _smoke(empty_paths=["fields"], synthetic_input=True)
        )
        result = await OutcomeStage().run(
            FINDS_NOTHING, context(WANTS_EVERYTHING, prior=report)
        )

        assert result.skipped and result.issues == []
        assert isinstance(SmokeResult, type)  # the import is the point

    async def test_a_result_with_nothing_empty_says_nothing(self) -> None:
        report = PipelineReport()
        report.results.append(_smoke(output_preview="{'rows': [1, 2]}"))
        result = await OutcomeStage().run(
            FINDS_NOTHING, context(WANTS_EVERYTHING, prior=report)
        )

        assert result.issues == []

    async def test_a_stage_run_alone_reads_nothing(self) -> None:
        """No pipeline, no prior report. It reports that, rather than inventing
        a verdict — a check that could not run has found nothing."""
        result = await OutcomeStage().run(FINDS_NOTHING, context(WANTS_EVERYTHING))

        assert result.skipped and result.issues == []

    async def test_a_failed_run_is_left_to_smoke(self) -> None:
        report = PipelineReport()
        report.results.append(_smoke(ok=False, output_preview=""))
        result = await OutcomeStage().run(
            FINDS_NOTHING, context(WANTS_EVERYTHING, prior=report)
        )

        assert result.skipped and result.issues == []


class TestALargeResultIsStillJudged:
    """The bug in this stage's own first implementation.

    It parsed ``output_preview`` to find empty collections, which passed every
    test written for it and then went silent on the real workflow it existed to
    catch: a 1500-character page excerpt pushed the result past the 400-character
    cap, and the empty ``fields`` list truncated away with everything else. The
    fact is now computed in the runner, where the output is whole.
    """

    async def test_an_empty_collection_behind_a_long_excerpt_is_found(self) -> None:
        report = PipelineReport()
        report.results.append(
            _smoke(
                output_preview="{'stage1': {'text': '" + "x" * 380,  # truncated
                empty_paths=["stage2.fields"],
            )
        )

        result = await OutcomeStage().run(
            FINDS_NOTHING, context(WANTS_EVERYTHING, prior=report)
        )

        assert [i.category for i in result.issues] == ["outcome"]
        assert "stage2.fields" in result.issues[0].message

    async def test_the_runner_reports_the_paths_itself(self) -> None:
        """End to end through the subprocess: the walker runs where the output
        is, and the path arrives on the result."""
        report = await CheckPipeline([SmokeStage()]).run(
            FINDS_NOTHING, context(WANTS_EVERYTHING)
        )
        smoke = report.detail("smoke")

        assert smoke.ok, smoke.error
        assert smoke.empty_paths == ["fields"]


class TestEveryDurableCallIsModelled:
    """The line a stage cannot hold.

    A ``ctx`` method missing from ``_CTX_CALL_MAP`` is wrong for *every*
    workflow at once, so no check over generated code can find it — every
    sample is equally and invisibly wrong. ``ctx.node`` sat in exactly that
    state for the node system's whole life: a workflow calling a catalogued
    node projected to a graph that never mentioned it, so the canvas, the
    narration and the committed ``graph.json`` all under-reported the flow, and
    the node system is precisely the part of loom that visual builders lean on.
    """

    def test_the_extractor_models_every_journaled_ctx_call(self) -> None:
        from loom.graph.extractor import _CTX_CALL_MAP, DURABLE_CTX_CALLS

        unmodelled = DURABLE_CTX_CALLS - set(_CTX_CALL_MAP)

        assert not unmodelled, (
            f"{sorted(unmodelled)} are journaled and would not draw. Add them "
            f"to _CTX_CALL_MAP, or take them out of DURABLE_CTX_CALLS if they "
            f"stopped being journaled."
        )

    def test_a_node_call_projects(self) -> None:
        from loom.graph.extractor import extract_from_source

        labels = [n.label for n in extract_from_source(CALLS_A_NODE).nodes]

        assert "io.http_request" in labels

    def test_a_node_draws_as_what_it_is(self) -> None:
        """A parked run and a model call are the two things a reader most needs
        to spot; a generic tool icon for both hides them."""
        from loom.graph.extractor import extract_from_source

        source = CALLS_A_NODE.replace("io.http_request", "human.approval")
        kinds = {n.label: str(n.kind) for n in extract_from_source(source).nodes}

        assert kinds["human.approval"] == "human"


class TestProjection:
    async def test_a_fully_projected_workflow_is_quiet(self) -> None:
        result = await ProjectionStage().run(CALLS_A_NODE, context(WANTS_ONE_THING))

        assert result.issues == []

    async def test_a_workflow_with_no_durable_calls_is_quiet(self) -> None:
        source = (
            "from loom import Context, workflow\n\n"
            "@workflow(name='trivial')\n"
            "async def trivial(ctx: Context, n: int) -> int:\n"
            "    return n + 1\n"
        )
        result = await ProjectionStage().run(source, context(WANTS_ONE_THING))

        assert result.issues == []

    async def test_a_call_the_graph_drops_is_reported(self) -> None:
        """Simulated by taking a modelled method back out of the map — the exact
        state ``ctx.node`` was in, without needing a real extractor bug to
        reproduce it."""
        from loom.graph.extractor import _CTX_CALL_MAP

        removed = _CTX_CALL_MAP.pop("node")
        try:
            result = await ProjectionStage().run(CALLS_A_NODE, context(WANTS_ONE_THING))
        finally:
            _CTX_CALL_MAP["node"] = removed

        assert [i.category for i in result.issues] == ["projection"]
        assert "ctx.node" in result.issues[0].message

    async def test_it_reports_rather_than_demands_a_repair(self) -> None:
        """A hole here is the extractor's defect, not the code's. An error would
        ask the model to reshape correct code until it happened to draw."""
        from loom.graph.extractor import _CTX_CALL_MAP

        removed = _CTX_CALL_MAP.pop("node")
        try:
            result = await ProjectionStage().run(CALLS_A_NODE, context(WANTS_ONE_THING))
        finally:
            _CTX_CALL_MAP["node"] = removed

        assert result.errors == []
        assert ProjectionStage().blocking is False

    def test_a_durable_call_in_an_if_test_draws(self) -> None:
        """Found by this stage on the shipped cookbook.

        ``if await ctx.wait_for_approval("refund"):`` is an approval *and* a
        branch. The walker descended into the branches and never into the test,
        so the human gate — the thing a reviewer most needs to see — was absent
        from the workflow's own graph while the switch it fed drew fine.
        """
        from loom.graph.extractor import extract_from_source

        source = (
            "from loom import Context, workflow\n\n"
            "@workflow(name='gated')\n"
            "async def gated(ctx: Context, n: int) -> str:\n"
            "    if await ctx.wait_for_approval('refund'):\n"
            "        return 'yes'\n"
            "    return 'no'\n"
        )
        drawn = [(n.label, str(n.kind)) for n in extract_from_source(source).nodes]

        assert ("wait_for_approval", "human") in drawn
        assert drawn.index(("wait_for_approval", "human")) < drawn.index(("if", "switch"))

    def test_a_durable_call_in_a_loop_iterable_draws(self) -> None:
        from loom.graph.extractor import extract_from_source

        source = (
            "from loom import Context, workflow\n\n"
            "@workflow(name='looped')\n"
            "async def looped(ctx: Context, n: int) -> int:\n"
            "    for row in await ctx.step(fetch, n):\n"
            "        pass\n"
            "    return n\n"
        )
        labels = [node.label for node in extract_from_source(source).nodes]

        assert "fetch" in labels

    async def test_the_shipped_workflows_all_project(self) -> None:
        """The corpus gate. Every workflow loom ships draws every durable call
        it makes — which was not true when this stage was written."""
        import glob

        findings = []
        for path in sorted(glob.glob("examples/reference/*.py")) + sorted(
            glob.glob("examples/cookbook/*.py")
        ):
            code = pathlib.Path(path).read_text()
            result = await ProjectionStage().run(code, context(""))
            findings.extend(f"{pathlib.Path(path).name}: {i.message}" for i in result.issues)

        assert not findings, findings

    def test_the_two_walkers_agree_about_nested_functions(self) -> None:
        """``durable_ctx_calls`` and the AST pass must scope identically, or the
        difference between them is just the walkers disagreeing."""
        from loom.graph.extractor import durable_ctx_calls

        source = (
            "from loom import Context, workflow\n\n"
            "@workflow(name='nested')\n"
            "async def nested(ctx: Context, n: int) -> int:\n"
            "    async def helper():\n"
            "        return await ctx.step(inner, n)\n"
            "    return await ctx.step(outer, n)\n"
        )

        assert [m for m, _ in durable_ctx_calls(source)] == ["step"]


class TestCataloguePreference:
    """The catalogue is what keeps loom legible to a visual builder. A step
    that re-implements a node draws as an opaque effect and teaches the next
    author nothing."""

    @pytest.fixture(autouse=True)
    def _catalogue_loaded(self):
        """What ``WorkflowCodingAgent.__init__`` does before any stage runs.

        Stated here rather than left to the stage: a verification stage that
        populates a process-global registry to do its job makes its own
        findings depend on who ran first, and it was enough to shift what an
        unrelated suite saw.
        """
        from loom.nodes.registry import load_builtin_nodes

        load_builtin_nodes()

    async def test_a_hand_rolled_http_call_is_pointed_at_the_node(self) -> None:
        result = await CataloguePreferenceStage().run(BY_HAND, context(""))

        assert [i.category for i in result.issues] == ["catalogue"]
        assert "io.http_request" in result.issues[0].message

    async def test_using_the_node_is_quiet(self) -> None:
        result = await CataloguePreferenceStage().run(CALLS_A_NODE, context(""))

        assert result.issues == []

    async def test_an_import_used_only_for_a_type_is_quiet(self) -> None:
        """The question is what the code does, not what it mentions."""
        source = BY_HAND.replace(
            "    async with httpx.AsyncClient() as client:\n"
            "        return (await client.get(url)).json()",
            "    return {}",
        )
        result = await CataloguePreferenceStage().run(source, context(""))

        assert result.issues == []

    async def test_advice_is_withheld_when_the_node_is_not_registered(self) -> None:
        """Pointing at something this environment does not have is worse than
        saying nothing — the rule ``CodeValidator`` already follows for
        toolsets."""

        class Empty:
            def search(self, _query, limit=0):
                return []

        result = await CataloguePreferenceStage(registry=Empty()).run(
            BY_HAND, context("")
        )

        assert result.issues == []

    async def test_it_advises_rather_than_demands(self) -> None:
        result = await CataloguePreferenceStage().run(BY_HAND, context(""))

        assert result.errors == []
        assert CataloguePreferenceStage().blocking is False


class TestTheReportReachesTheModel:
    """Phase 0's actual thesis. The verdict existed all along; nothing handed it
    back to the agent that wrote the code."""

    def test_the_repair_prompt_carries_what_the_code_returned(self) -> None:
        report = PipelineReport()
        report.results.append(_smoke(output_preview="{'field_count': 0, 'fields': []}"))
        report.results.append(
            _result("outcome", [CodeIssue("outcome", "came back empty", "error")])
        )

        prompt = _repair_prompt(report, FINDS_NOTHING, WANTS_EVERYTHING)

        assert "'field_count': 0" in prompt
        assert "came back empty" in prompt
        assert WANTS_EVERYTHING in prompt

    def test_it_still_carries_a_traceback(self) -> None:
        """The addition is additive. A crash reaches the model the way it did."""
        report = PipelineReport()
        report.results.append(
            _smoke(ok=False, traceback="ValueError: nope", output_preview="")
        )

        prompt = _repair_prompt(report, FINDS_NOTHING, "")

        assert "ValueError: nope" in prompt


class TestTheEscapeHatch:
    """What makes an *error* safe here.

    Reporting a suspicion as an error is what gets it in front of the model —
    and it is also how a model is pressed into 'fixing' a result that was
    correct. The repair loop already holds the answer: unchanged code ends it.
    """

    async def test_unchanged_code_ends_the_repair(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent, _extract_code

        # The loop compares the *extracted* candidate against the code it holds,
        # so "unchanged" means unchanged after extraction. Anything else tests
        # the fence parser rather than the escape hatch.
        settled = _extract_code(FINDS_NOTHING)
        report = PipelineReport()
        report.results.append(
            _result("outcome", [CodeIssue("outcome", "came back empty", "error")])
        )

        calls = 0

        class _Session:
            """The `Asking` contract, which is all the repair loop needs.

            The loop drives a `CodingSession` in production so the model keeps
            the schemas and resolved ids discovery paid for; here it only has
            to answer.
            """

            async def ask(self, prompt: str):
                nonlocal calls
                calls += 1
                # The model was asked about the finding, and declines by
                # returning the file it already wrote.
                assert "came back empty" in prompt
                return _Reply(settled)

        coder = WorkflowCodingAgent.__new__(WorkflowCodingAgent)
        coder._max_repair = 3
        code, rounds = await coder._repair_from(
            _Session(), settled, report, context(WANTS_EVERYTHING)
        )

        assert code == settled, "the model's judgement stands"
        assert calls == 1, "asked once, then accepted — not three times"
        assert rounds == 1


class TestNothingElseMoved:
    """The guard on the phase: the default pipeline gains a stage and loses
    nothing, and a workflow with a real answer is untouched by any of it."""

    def test_outcome_joins_the_default_pipeline_between_smoke_and_replay(self) -> None:
        from loom.agents.stages import default_stages

        names = [stage.name for stage in sorted(default_stages(), key=lambda s: s.cost)]

        assert names.index("smoke") < names.index("outcome") < names.index("replay")

    def test_the_default_pipeline_keeps_every_stage_it_had(self) -> None:
        from loom.agents.stages import default_stages

        names = {stage.name for stage in default_stages()}

        assert {
            "compile", "static", "grants", "coverage", "placement", "resolution",
            "identifiers", "lint", "types", "smoke", "replay",
        } <= names

    def test_smoke_only_pipelines_are_unaffected(self) -> None:
        from loom.agents.stages import default_stages

        assert "outcome" not in {s.name for s in default_stages(smoke=False)}

    async def test_a_good_workflow_reports_no_errors(self) -> None:
        """The false-positive guard. A workflow that answers its spec must come
        through the new stage silent."""
        report, issues = await outcome_of(FINDS_SOMETHING, WANTS_EVERYTHING)

        assert issues == []
        assert report.ok, report.summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Reply:
    """The shape ``_repair_from`` reads back off the agent."""

    def __init__(self, code: str) -> None:
        self.output = code


def _smoke(**fields):
    """A ``CheckResult`` carrying a ``SmokeResult``, as the pipeline builds it."""
    from loom.agents.checks import CheckResult
    from loom.agents.smoke import SmokeResult

    defaults = {"ok": True, "phase": "done", "status": "completed", "steps_executed": 1}
    return CheckResult("smoke", detail=SmokeResult(**{**defaults, **fields}))


def _result(name: str, issues: list[CodeIssue]):
    from loom.agents.checks import CheckResult

    return CheckResult(name, issues=issues)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """These workflows reach nothing, but the smoke subprocess should not either
    if a future edit makes one of them try."""
    monkeypatch.setenv("LOOM_STORE", "memory://")


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class TestAuthoringHasASurface:
    """Until this, the coding agent had none.

    Every capability it grew — probes, the outcome check, the plan — was
    reachable only by writing a Python driver, which is a large part of why its
    loop went so long without pressure on it. `author` lives on the port, so
    the CLI, the MCP server and anything else built on `RuntimeFacade` get the
    same one, and `test_surface_parity` fails the build when an adapter
    implements less than the whole thing.
    """

    def test_the_port_declares_it(self) -> None:
        from loom.facade import RuntimeFacade

        assert hasattr(RuntimeFacade, "author")

    def test_every_adapter_implements_it(self) -> None:
        from loom.facade import LocalFacade, RemoteFacade
        from loom.identity.facade import AuthorizedFacade

        for adapter in (LocalFacade, RemoteFacade, AuthorizedFacade):
            assert hasattr(adapter, "author"), adapter.__name__

    async def test_it_is_refused_remotely_with_the_reason(self) -> None:
        """Authoring runs where the code will run. It reads *this* process's
        toolsets, nodes and probes to decide what the workflow may call, and a
        server's model key would be spending someone else's budget."""
        from loom.core.exceptions import ConfigurationError
        from loom.facade import RemoteFacade

        facade = RemoteFacade.__new__(RemoteFacade)
        with pytest.raises(ConfigurationError) as caught:
            await facade.author("anything")

        assert "--server" in str(caught.value)

    async def test_no_model_key_says_which_to_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a stack trace from inside a vendor SDK.

        The keys are cleared explicitly: this asserts what happens when *none*
        is set, and a developer machine with one exported turned it into a
        failure that looked like a code defect.
        """
        from loom.core.exceptions import ConfigurationError
        from loom.facade import LocalFacade
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                    "GOOGLE_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        facade = LocalFacade(Runtime(store=MemoryStore()))
        with pytest.raises(ConfigurationError) as caught:
            await facade.author("anything")

        message = str(caught.value)
        assert "ANTHROPIC_API_KEY" in message and "OPENAI_API_KEY" in message

    def test_authoring_needs_its_own_scope(self) -> None:
        """Not folded into publishing: authoring spends tokens and, with
        observation on, reaches systems the spec names. Being trusted to
        publish code someone has read implies neither."""
        from loom.identity.scopes import Scope

        assert Scope.WORKFLOWS_AUTHOR.value == "workflows:author"
        assert Scope.WORKFLOWS_AUTHOR is not Scope.WORKFLOWS_PUBLISH

    def test_one_place_knows_which_providers_the_environment_has(
        self, monkeypatch
    ) -> None:
        """The Runtime picking an agent backend and the coding agent picking a
        model were reading the same three keys from two places."""
        from loom.agents import providers

        for key in providers.env_keys():
            monkeypatch.delenv(key, raising=False)

        assert providers.from_env() is None
        assert "ANTHROPIC_API_KEY" in providers.env_keys()
