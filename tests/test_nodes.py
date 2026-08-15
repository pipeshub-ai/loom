"""The node system: contract, catalog, guards, HIL, and agent visibility.

Organised by the property each group defends rather than by module, because the
properties are what would silently stop being true.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from pydantic import BaseModel, Field

from loom import Context, Runtime, workflow
from loom.core.exceptions import GuardrailTripwire
from loom.nodes import (
    HumanChannelMissing,
    Node,
    NodeCategory,
    NodeContractError,
    NodeExample,
    NodeNotFound,
    NodeRegistry,
    NodeSpec,
    get_node_catalog,
    load_builtin_nodes,
)
from loom.nodes.guard import GuardInput, GuardVerdict
from loom.nodes.human import (
    ApprovalIn,
    AutoRespondChannel,
    ChoiceIn,
    LogChannel,
    ReviewIn,
)
from loom.stores import MemoryStore

load_builtin_nodes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ScoreIn(BaseModel):
    text: str = Field(description="The blurb to score.")
    threshold: float = 0.5


class ScoreOut(BaseModel):
    score: float
    passed: bool


class ScoreNode(Node[ScoreIn, ScoreOut]):
    """Score a lead from its blurb."""

    spec = NodeSpec(
        id="custom.score",
        category=NodeCategory.TRANSFORM,
        examples=[NodeExample(payload={"text": "acme wants a demo"})],
    )
    Input, Output = ScoreIn, ScoreOut

    async def run(self, ctx: Any, payload: ScoreIn) -> ScoreOut:
        async def compute() -> float:
            return len(payload.text) / 100

        score = await ctx.call("compute", compute)
        return ScoreOut(score=score, passed=score >= payload.threshold)


class ScoreOutV2(BaseModel):
    """The same node's output, one field wider — an ordinary compatible-looking
    upgrade, and exactly the one that must not replay silently."""

    score: float
    passed: bool
    band: str = "unknown"


class ScoreNodeV2(ScoreNode):
    """Score a lead from its blurb."""

    spec = NodeSpec(id="custom.score", version="2.0.0", category=NodeCategory.TRANSFORM)
    Input, Output = ScoreIn, ScoreOutV2


@pytest.fixture
def registry() -> NodeRegistry:
    reg = NodeRegistry()
    reg.register_node(ScoreNode)
    return reg


@pytest.fixture
def runtime(registry: NodeRegistry) -> Runtime:
    return Runtime(store=MemoryStore(), nodes=registry)


# ---------------------------------------------------------------------------
# The contract is checked where the author is, not where the caller is
# ---------------------------------------------------------------------------


class TestRegistrationValidatesTheContract:
    def test_a_well_formed_node_registers_and_derives_everything(
        self, registry: NodeRegistry
    ) -> None:
        spec = registry.get("custom.score")
        assert spec is not None
        assert spec.node_class.endswith(":ScoreNode")
        assert spec.input_schema["properties"]["text"]["description"]
        assert spec.output_schema["properties"]["passed"]["type"] == "boolean"
        assert spec.summary == "Score a lead from its blurb."

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            ({"Input": dict}, "pydantic BaseModel"),
            ({"Output": str}, "pydantic BaseModel"),
            ({"spec": NodeSpec(id="flat")}, "not namespaced"),
            ({"run": lambda self, ctx, payload: None}, "async def"),
        ],
    )
    def test_a_malformed_node_is_refused(
        self, registry: NodeRegistry, mutate: dict[str, Any], expected: str
    ) -> None:
        """Every guard is mutation-verified: break one thing, this must fail."""
        broken = type("Broken", (ScoreNode,), dict(mutate))
        with pytest.raises(NodeContractError, match=expected):
            registry.register_node(broken)

    def test_a_node_without_run_is_refused(self, registry: NodeRegistry) -> None:
        class Bodyless(Node[ScoreIn, ScoreOut]):
            spec = NodeSpec(id="custom.bodyless", category=NodeCategory.CUSTOM)
            Input, Output = ScoreIn, ScoreOut

        with pytest.raises(NodeContractError, match="does not implement run"):
            registry.register_node(Bodyless)


# ---------------------------------------------------------------------------
# The catalog stays cheap as it grows
# ---------------------------------------------------------------------------


class TestTheCatalogScales:
    def test_the_prompt_block_does_not_grow_with_the_catalog(self) -> None:
        """**The one hard budget.** Exact equality, not a tolerance.

        A tolerance is a budget that erodes. The toolset side shows what the
        alternative costs: ``describe(detail="index")`` enumerates operation
        names, so it runs ~830 characters per toolset — fine at four, 21k tokens
        at a hundred. Nothing here may enumerate.
        """
        catalog = NodeRegistry(parent=get_node_catalog())
        baseline = catalog.prompt_block()

        for i in range(500):
            catalog.register(
                NodeSpec(
                    id=f"custom.generated_{i}",
                    category=NodeCategory.CUSTOM,
                    summary=f"A synthetic node number {i} with a long summary line.",
                    tags=[f"tag{i}", "synthetic"],
                )
            )
        grown = catalog.prompt_block()

        # The custom category appears now, so the block gains one line and no more.
        assert len(grown.splitlines()) == len(baseline.splitlines()) + 1
        assert "generated_1" not in grown
        assert len(grown) < 1000, (
            f"{len(grown)} chars for a 500-node catalog — something enumerates it"
        )

    def test_registering_a_spec_imports_nothing(self, tmp_path: Any) -> None:
        """Layer 1 is pure data.

        Checked in a fresh interpreter, because this one has already imported
        every node module — asserting against ``sys.modules`` here could only be
        vacuous, which is worse than not checking.
        """
        probe = (
            "NODE = 'loom.nodes.human.nodes:ApprovalNode';"
            "import sys;"
            "from loom.nodes import NodeRegistry, NodeSpec;"
            "r = NodeRegistry();"
            "r.register(NodeSpec(id='x.y', node_class=NODE));"
            "r.search('y');"
            "print('loom.nodes.human.nodes' in sys.modules)"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert done.stdout.strip() == "False", "registering a spec imported the node"

    def test_resolution_is_what_imports(self) -> None:
        probe = (
            "NODE = 'loom.nodes.human.nodes:ApprovalNode';"
            "import sys;"
            "from loom.nodes import NodeRegistry, NodeSpec;"
            "r = NodeRegistry();"
            "r.register(NodeSpec(id='x.y', node_class=NODE));"
            "r.resolve('x.y');"
            "print('loom.nodes.human.nodes' in sys.modules)"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert done.stdout.strip() == "True"

    def test_an_empty_query_with_a_category_lists_it(self) -> None:
        """The affordance that makes the categorised catalog worth having.

        A model with no keyword otherwise has nowhere to start; the toolset
        ``search(query)`` has no equivalent.
        """
        found = get_node_catalog().search("", category=NodeCategory.HUMAN)
        assert {c.id for c in found} >= {"human.approval", "human.choice"}
        assert all(c.category is NodeCategory.HUMAN for c in found)

    def test_a_wrong_id_offers_near_matches(self, registry: NodeRegistry) -> None:
        with pytest.raises(NodeNotFound) as caught:
            registry.resolve("custom.scor")
        assert "custom.score" in caught.value.suggestions

    def test_a_local_node_shadows_the_global_one(self) -> None:
        local = NodeRegistry(parent=get_node_catalog())
        local.register(
            NodeSpec(id="human.approval", category=NodeCategory.HUMAN, summary="mine")
        )
        assert local.get("human.approval").summary == "mine"
        found = [c for c in local.search("approval") if c.id == "human.approval"]
        assert len(found) == 1, "the same id came back twice, with two contracts"


# ---------------------------------------------------------------------------
# The contract tier returns code, not schema
# ---------------------------------------------------------------------------


class TestTheAgentGetsCode:
    def test_the_contract_is_a_runnable_call(self, registry: NodeRegistry) -> None:
        contract = registry.contract("custom.score")
        assert "await ctx.node(" in contract
        assert "'custom.score'" in contract
        assert "ScoreIn(" in contract
        assert "text='acme wants a demo'" in contract, "the example supplies values"
        assert "threshold=0.5" in contract, "the default supplies the rest"
        assert "result: ScoreOut" in contract, "the caller needs the result type"
        assert "# ScoreOut: score: float, passed: bool" in contract

    def test_it_carries_the_import_line(self) -> None:
        """The lesson ``tools_module`` taught, applied before it is relearned.

        An id like ``human.approval`` exists in no namespace, and a model asked
        to write code against one invents an import to match.
        """
        contract = get_node_catalog().contract("human.approval")
        assert "from loom.nodes.human import ApprovalIn, ApprovalOut" in contract

    def test_it_says_what_a_schema_cannot(self) -> None:
        contract = get_node_catalog().contract("human.approval")
        assert "suspends: yes" in contract
        assert "requires: human_channel" in contract
        assert "Parks the run" in contract

    def test_pydantic_formats_render_as_python_types(self) -> None:
        """A timedelta must not render as ``str``, or the agent writes a string."""
        contract = get_node_catalog().contract("human.approval")
        assert "timedelta" in contract
        assert "decided_at: datetime" in contract

    def test_every_builtin_renders_a_contract(self) -> None:
        catalog = get_node_catalog()
        for node_id in catalog.node_ids():
            contract = catalog.contract(node_id)
            assert "await ctx.node(" in contract, node_id
            # The rendered call must be syntactically valid Python. Wrapped in
            # an async def because it contains `await`, which is what the agent
            # will paste it into.
            body = "\n".join(
                "    " + line
                for line in contract.splitlines()
                if line and not line.startswith("#")
            )
            compile(f"async def _():\n{body}", f"<{node_id}>", "exec")

    def test_the_agent_tools_are_present_and_bound(self) -> None:
        from loom.agents.coding_tools import build_coding_tools

        names = {t.name for t in build_coding_tools()}
        assert {"search_nodes", "show_node", "node_contract"} <= names

    async def test_search_reports_the_catalog_when_nothing_matches(self) -> None:
        """A dead end must hand back the next move, not just a negative."""
        from loom.agents.coding_tools import search_nodes

        answer = await search_nodes.fn("wobbulator")
        assert "categories" in answer and "human" in answer


# ---------------------------------------------------------------------------
# Nodes add no durability semantics
# ---------------------------------------------------------------------------


class TestNodesJournalLikeAnythingElse:
    async def test_a_node_call_journals_and_replays_identically(
        self, runtime: Runtime
    ) -> None:
        @workflow(name="scoring")
        async def scoring(ctx: Context, text: str) -> str:
            out = await ctx.node("custom.score", ScoreIn(text=text))
            return f"{out.score:.2f}/{out.passed}"

        runtime.register(scoring)
        first = await runtime.run(scoring, "a" * 60)
        assert first.status.value == "completed"

        entries = await runtime.store.load_journal(first.run_id)
        node_entry = next(e for e in entries if e.name == "node:custom.score")
        assert node_entry.metadata["node_id"] == "custom.score"
        assert node_entry.metadata["contract"], "the contract hash must be journaled"
        # The body's own durable call nests beneath the node's path.
        assert any(e.path.startswith(f"{node_entry.path}.") for e in entries)

        replayed = await runtime.replay(first.run_id)
        assert replayed.output == first.output

    async def test_a_changed_contract_refuses_to_replay(
        self, runtime: Runtime, registry: NodeRegistry
    ) -> None:
        """The load-bearing replay hazard.

        Decoding an old payload into a new model would let a node upgrade
        quietly change what an old run replays to — the run would appear to have
        done something it never did.
        """

        @workflow(name="scoring2")
        async def scoring(ctx: Context, text: str) -> float:
            out = await ctx.node("custom.score", ScoreIn(text=text))
            return out.score

        runtime.register(scoring)
        done = await runtime.run(scoring, "abc")
        assert done.status.value == "completed"

        registry.register_node(ScoreNodeV2)
        assert registry.get("custom.score").contract_hash != ScoreNode.spec.contract_hash

        failed = await runtime.replay(done.run_id)
        assert failed.status.value == "failed"
        assert "contract" in (failed.error.message if failed.error else "").lower()

    async def test_a_payload_that_does_not_fit_fails_at_the_call_site(
        self, runtime: Runtime
    ) -> None:
        @workflow(name="bad_payload")
        async def bad(ctx: Context, _: Any = None) -> Any:
            return await ctx.node("custom.score", {"threshold": "not a number"})

        runtime.register(bad)
        result = await runtime.run(bad)
        assert result.status.value == "failed"
        assert "ScoreIn" in (result.error.message if result.error else "")

    async def test_a_body_returning_the_wrong_shape_is_a_contract_error(
        self, runtime: Runtime, registry: NodeRegistry
    ) -> None:
        class Liar(ScoreNode):
            spec = NodeSpec(id="custom.liar", category=NodeCategory.CUSTOM)
            Input, Output = ScoreIn, ScoreOut

            async def run(self, ctx: Any, payload: ScoreIn) -> Any:
                return "not a model"

        registry.register_node(Liar)

        @workflow(name="liar_flow")
        async def flow(ctx: Context, _: Any = None) -> Any:
            return await ctx.node("custom.liar", ScoreIn(text="x"))

        runtime.register(flow)
        result = await runtime.run(flow)
        assert result.status.value == "failed"
        assert "contract" in (result.error.message if result.error else "").lower()


# ---------------------------------------------------------------------------
# Guards: four verdicts, three attachment points
# ---------------------------------------------------------------------------


class TestGuards:
    @pytest.fixture
    def guarded_runtime(self) -> Runtime:
        return Runtime(store=MemoryStore())

    async def test_allow_passes_the_value_through(self, guarded_runtime: Runtime) -> None:
        @workflow(name="g_allow")
        async def flow(ctx: Context, text: str) -> Any:
            from loom.nodes.guard import PolicyIn

            return await ctx.guard("guard.policy", PolicyIn(value=text, deny_if=["nope"]))

        guarded_runtime.register(flow)
        result = await guarded_runtime.run(flow, "fine")
        assert result.status.value == "completed"

    async def test_reject_raises_rather_than_returning_falsy(
        self, guarded_runtime: Runtime
    ) -> None:
        """The one semantic that changes with the wider reach.

        In an agent loop REJECT hands the model an explanation so it can adapt.
        In a workflow body there is nobody to adapt, so a falsy return value
        would simply be ignored and the guarded work would proceed.
        """

        @workflow(name="g_reject")
        async def flow(ctx: Context, text: str) -> Any:
            from loom.nodes.guard import PolicyIn

            return await ctx.guard(
                "guard.policy", PolicyIn(value=text, deny_if=["DROP TABLE"])
            )

        guarded_runtime.register(flow)
        result = await guarded_runtime.run(flow, "x; DROP TABLE users")
        assert result.status.value == "failed"
        assert "rejected" in (result.error.message if result.error else "")

    async def test_replace_substitutes_and_continues(
        self, guarded_runtime: Runtime
    ) -> None:
        @workflow(name="g_replace")
        async def flow(ctx: Context, text: str) -> Any:
            from loom.nodes.guard import PiiIn

            return await ctx.guard(
                "guard.pii", PiiIn(value=text, kinds=["api_key"], redact=True)
            )

        guarded_runtime.register(flow)
        result = await guarded_runtime.run(flow, "key sk-abcdefghijklmnop1234 here")
        assert result.status.value == "completed"
        assert "[redacted:api_key]" in result.output
        assert "sk-abcdefghijklmnop1234" not in result.output

    async def test_tripwire_fails_the_run(self, guarded_runtime: Runtime) -> None:
        from loom.nodes.guard import enforce

        with pytest.raises(GuardrailTripwire):
            enforce(GuardVerdict.tripwire("policy"), guard="g", value=1)

    async def test_a_guard_that_raises_is_a_tripwire_not_an_allow(self) -> None:
        """A check that cannot run has found nothing, so it must not open the gate."""
        from loom.nodes.guard import apply_guards

        def explode(_: Any) -> Any:
            raise RuntimeError("the policy service is down")

        with pytest.raises(GuardrailTripwire, match="not treated as a pass"):
            await apply_guards(
                [explode], "value", ctx=None, registry=None, phase="input", subject="x"
            )

    async def test_guards_attached_to_a_node_run_before_its_body(self) -> None:
        registry = NodeRegistry(parent=get_node_catalog())

        class Recorder(Node[GuardInput, GuardVerdict]):
            spec = NodeSpec(id="custom.recorded", category=NodeCategory.CUSTOM)
            Input, Output = GuardInput, GuardVerdict
            ran = False

            async def run(self, ctx: Any, payload: GuardInput) -> GuardVerdict:
                type(self).ran = True
                return GuardVerdict.allow()

        class Blocker(Node[GuardInput, GuardVerdict]):
            spec = NodeSpec(id="guard.always_no", category=NodeCategory.GUARD)
            Input, Output = GuardInput, GuardVerdict

            async def run(self, ctx: Any, payload: GuardInput) -> GuardVerdict:
                return GuardVerdict.reject("never")

        registry.register_node(Recorder)
        registry.register_node(Blocker)
        runtime = Runtime(store=MemoryStore(), nodes=registry)

        @workflow(name="guarded_node")
        async def flow(ctx: Context, _: Any = None) -> Any:
            return await ctx.node(
                "custom.recorded", GuardInput(value=1), guards=["guard.always_no"]
            )

        runtime.register(flow)
        result = await runtime.run(flow)
        assert result.status.value == "failed"
        assert Recorder.ran is False, "the guard ran after the body"

    async def test_the_agent_guardrail_abstraction_is_reused_not_forked(self) -> None:
        from loom.agents.guardrails import reject as agent_reject
        from loom.nodes.guard import as_verdict

        verdict = as_verdict(agent_reject("no"))
        assert verdict.blocked and verdict.message == "no"


# ---------------------------------------------------------------------------
# Human-in-the-loop
# ---------------------------------------------------------------------------


@workflow(name="refund_flow")
async def refund_flow(ctx: Context, amount: float) -> str:
    decision = await ctx.node(
        "human.approval",
        ApprovalIn(subject="refund", prompt=f"Approve ${amount}?", assignees=["fin@x"]),
    )
    return f"approved:{decision.responder}" if decision.approved else "held"


class TestHumanInTheLoop:
    async def test_no_channel_raises_before_the_run_parks(self) -> None:
        """The worst outcome in this design is a run parked with nobody told.

        It is indistinguishable from patience, so it is found a day late.
        """
        runtime = Runtime(store=MemoryStore())
        runtime.register(refund_flow)
        result = await runtime.run(refund_flow, 420.0)
        assert result.status.value == "failed"
        assert "human channel" in (result.error.message if result.error else "")

        with pytest.raises(HumanChannelMissing):
            runtime.nodes.resolve("human.approval", runtime=runtime)

    async def test_it_parks_delivers_once_and_resumes_typed(self) -> None:
        channel = LogChannel()
        runtime = Runtime(store=MemoryStore(), human=channel)
        runtime.register(refund_flow)

        parked = await runtime.run(refund_flow, 420.0)
        assert parked.status.value == "suspended"
        assert len(channel.requests) == 1
        request = channel.requests[0]
        assert request.subject == "refund"
        assert request.assignees == ["fin@x"]
        assert "approved" in request.response_schema["properties"], (
            "the channel must be told the shape of the answer it is collecting"
        )

        # Re-entering must not re-notify. Without this a person gets the same
        # approval once per crash, which is how a team learns to ignore it.
        await runtime.resume(parked.run_id)
        await runtime.resume(parked.run_id)
        assert len(channel.requests) == 1

        await runtime.approve(parked.run_id, "refund", approved=True)
        done = await runtime.resume(parked.run_id)
        assert done.status.value == "completed"
        assert done.output.startswith("approved")

    async def test_the_old_wait_for_approval_still_resolves_the_same_way(self) -> None:
        """A workflow written before nodes existed must keep working."""

        @workflow(name="old_style")
        async def old(ctx: Context, _: Any = None) -> str:
            return "yes" if await ctx.wait_for_approval("refund") else "no"

        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(old)
        parked = await runtime.run(old)
        await runtime.approve(parked.run_id, "refund", approved=True)
        assert (await runtime.resume(parked.run_id)).output == "yes"

    async def test_a_rejection_comes_back_typed(self) -> None:
        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(refund_flow)
        parked = await runtime.run(refund_flow, 20.0)
        await runtime.approve(parked.run_id, "refund", approved=False)
        assert (await runtime.resume(parked.run_id)).output == "held"

    async def test_a_choice_outside_the_offered_options_is_refused(self) -> None:
        """Accepting one would let a channel widen the workflow's branch set."""

        @workflow(name="routing")
        async def route(ctx: Context, _: Any = None) -> Any:
            return await ctx.node(
                "human.choice",
                ChoiceIn(subject="route", options=["billing", "support"]),
            )

        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(route)
        parked = await runtime.run(route)
        await runtime.send_event(
            parked.run_id, "approval:route", {"selected": ["legal"]}
        )
        result = await runtime.resume(parked.run_id)
        assert result.status.value == "failed"
        assert "not among the offered options" in (
            result.error.message if result.error else ""
        )

    async def test_review_reports_whether_the_draft_was_edited(self) -> None:
        @workflow(name="review")
        async def review(ctx: Context, draft: str) -> Any:
            out = await ctx.node("human.review_edit", ReviewIn(subject="d", draft=draft))
            return f"{out.edited}:{out.content}"

        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(review)
        parked = await runtime.run(review, "hello")
        await runtime.send_event(parked.run_id, "approval:d", {"content": "hi there"})
        assert (await runtime.resume(parked.run_id)).output == "True:hi there"

    async def test_the_auto_channel_lets_a_hil_workflow_smoke_test(self) -> None:
        """Without this, the check pipeline teaches the agent to delete approvals.

        A generated workflow containing an approval has nobody to answer it in
        the sandbox: it hangs to the timeout, reports a failure, and the cheapest
        repair a model can find is removing the approval — which then passes
        every check while having stripped out the control the spec asked for.
        """
        channel = AutoRespondChannel(approve=True)
        runtime = Runtime(store=MemoryStore(), human=channel)
        channel.bind(runtime)
        runtime.register(refund_flow)

        result = await runtime.run(refund_flow, 5.0)
        assert result.status.value == "completed"
        assert result.output == "approved:auto"

    async def test_the_channel_is_told_how_to_render_every_human_node(self) -> None:
        """Every ``human.*`` node must carry a response schema, or a provider
        cannot build a form for it and has to special-case node ids."""
        catalog = get_node_catalog()
        human_nodes = catalog.search("", category=NodeCategory.HUMAN, limit=50)
        assert len(human_nodes) >= 5
        for card in human_nodes:
            assert catalog.get(card.id).requires == ["human_channel"], card.id
            assert catalog.get(card.id).suspends, card.id


# ---------------------------------------------------------------------------
# The standard library
# ---------------------------------------------------------------------------


class TestTheStandardLibrary:
    def test_every_category_is_actually_populated(self) -> None:
        """A regression guard with a specific history.

        Flattening ``nodes/stdlib/`` collapsed the loader's module list to the
        ``loom.nodes`` *package*, which imports fine — so nothing raised, and
        the catalog came back with 10 nodes in two categories instead of 26 in
        six. The loud loader cannot help when the import succeeds and simply
        registers nothing, so the count is asserted directly.
        """
        counts = {c.value: n for c, n in get_node_catalog().categories().items()}
        assert counts.get("human", 0) >= 5, counts
        assert counts.get("guard", 0) >= 5, counts
        assert counts.get("control", 0) >= 5, counts
        assert counts.get("transform", 0) >= 5, counts
        assert counts.get("io", 0) >= 2, counts
        assert counts.get("agent", 0) >= 4, counts
        assert sum(counts.values()) >= 26, counts

    def test_every_builtin_declares_what_the_agent_needs(self) -> None:
        catalog = get_node_catalog()
        assert len(catalog.node_ids()) >= 20
        for node_id in catalog.node_ids():
            spec = catalog.get(node_id)
            assert spec.summary, f"{node_id} has no summary — search returns it"
            assert spec.input_schema, f"{node_id} has no input schema"
            assert spec.output_schema, f"{node_id} has no output schema"
            assert spec.import_line(), f"{node_id} does not say how to import itself"
            assert spec.tags, f"{node_id} has no tags, so search barely finds it"

    def test_a_suspending_node_says_so(self) -> None:
        catalog = get_node_catalog()
        assert catalog.get("io.wait_for_webhook").suspends
        assert not catalog.get("control.switch").suspends

    def test_judgement_and_rules_are_different_categories(self) -> None:
        """So that choosing between them is deliberate rather than accidental."""
        catalog = get_node_catalog()
        assert catalog.get("agent.classify").category is NodeCategory.AGENT
        assert catalog.get("control.switch").category is NodeCategory.CONTROL
        assert catalog.get("agent.classify").deterministic is False
        assert catalog.get("control.switch").deterministic is True

    @pytest.mark.parametrize(
        "node_id",
        [
            "control.switch",
            "control.filter",
            "control.dedupe",
            "control.batch",
            "transform.map_fields",
            "transform.template",
            "transform.extract",
            "transform.join",
            "transform.redact",
            "guard.schema",
            "guard.policy",
            "guard.pii",
        ],
    )
    async def test_each_node_runs_its_own_documented_example(self, node_id: str) -> None:
        """The suite is driven by the catalog, so an untested node is a missing
        example — which is visible in the docs the agent reads."""
        catalog = get_node_catalog()
        spec = catalog.get(node_id)
        assert spec.examples, f"{node_id} ships no example"

        runtime = Runtime(store=MemoryStore())

        @workflow(name=f"example_{node_id.replace('.', '_')}")
        async def flow(ctx: Context, _: Any = None) -> Any:
            return await ctx.node(node_id, spec.examples[0].payload)

        runtime.register(flow)
        result = await runtime.run(flow)
        assert result.status.value == "completed", (
            f"{node_id}'s own example does not run: "
            f"{result.error.message if result.error else ''}"
        )

    async def test_filter_reports_what_it_dropped(self) -> None:
        """A filter that silently removes everything looks like one given nothing."""
        runtime = Runtime(store=MemoryStore())

        @workflow(name="filtering")
        async def flow(ctx: Context, _: Any = None) -> Any:
            out = await ctx.node(
                "control.filter",
                {
                    "items": [{"s": "open"}, {"s": "done"}, {"s": "done"}],
                    "field": "s",
                    "equals": "open",
                },
            )
            return f"{out.kept}/{out.dropped}"

        runtime.register(flow)
        assert (await runtime.run(flow)).output == "1/2"

    async def test_map_fields_counts_misses(self) -> None:
        """A silent None in every row is how a wrong source path hides."""
        runtime = Runtime(store=MemoryStore())

        @workflow(name="mapping")
        async def flow(ctx: Context, _: Any = None) -> Any:
            out = await ctx.node(
                "transform.map_fields",
                {"items": [{"a": 1}, {"a": 2}], "mapping": {"b": "nope"}},
            )
            return out.missing

        runtime.register(flow)
        assert (await runtime.run(flow)).output == {"b": 2}

    async def test_a_strict_template_fails_rather_than_shipping_a_placeholder(
        self,
    ) -> None:
        runtime = Runtime(store=MemoryStore())

        @workflow(name="templating")
        async def flow(ctx: Context, _: Any = None) -> Any:
            return await ctx.node(
                "transform.template", {"template": "Hi $name", "values": {}}
            )

        runtime.register(flow)
        assert (await runtime.run(flow)).status.value == "failed"


# ---------------------------------------------------------------------------
# Custom nodes: author, register, discover, run
# ---------------------------------------------------------------------------


class TestCustomNodes:
    async def test_the_whole_path_end_to_end(self) -> None:
        registry = NodeRegistry(parent=get_node_catalog())
        registry.register_node(ScoreNode)

        # discover
        assert [c.id for c in registry.search("lead blurb")] == ["custom.score"]
        # inspect
        assert registry.show("custom.score").import_line
        # get the code
        assert "await ctx.node(" in registry.contract("custom.score")
        # run it
        runtime = Runtime(store=MemoryStore(), nodes=registry)

        @workflow(name="custom_e2e")
        async def flow(ctx: Context, _: Any = None) -> Any:
            out = await ctx.node("custom.score", ScoreIn(text="x" * 80))
            return out.passed

        runtime.register(flow)
        assert (await runtime.run(flow)).output is True

    async def test_a_node_cannot_restructure_the_run_it_is_part_of(self) -> None:
        """The narrowing is a security boundary, so it is a real object.

        A third-party node runs inside somebody's workflow. It may do durable
        work; it may not end the run, spawn children, or publish as the parent.
        """
        from loom.nodes.base import NodeContext

        for forbidden in ("continue_as_new", "child", "publish", "signal", "state",
                          "put_artifact", "compensate"):
            assert not hasattr(NodeContext, forbidden), forbidden
        for allowed in ("step", "call", "sleep", "wait_for_event", "report", "now",
                        "uuid4", "random", "agent", "capability"):
            assert hasattr(NodeContext, allowed), allowed

    def test_a_runtime_sees_globally_registered_nodes(self) -> None:
        runtime = Runtime(store=MemoryStore())
        assert "human.approval" in runtime.nodes.node_ids()
        assert runtime.nodes.get("control.switch") is not None

    def test_a_locally_registered_node_stays_local(self) -> None:
        runtime = Runtime(store=MemoryStore())
        runtime.nodes.register_node(ScoreNode)
        assert "custom.score" in runtime.nodes.node_ids()
        assert "custom.score" not in get_node_catalog().node_ids()


# ---------------------------------------------------------------------------
# The operator surface
# ---------------------------------------------------------------------------


class TestPendingRequestsAreFindable:
    """`loom pending` is what makes a parked run a queue item.

    Before it, finding one meant already knowing it existed — which is the
    failure the whole HIL design is trying to remove.
    """

    @pytest.fixture
    async def parked(self) -> Any:
        from loom.facade import LocalFacade

        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(refund_flow)
        result = await runtime.run(refund_flow, 420.0)
        assert result.status.value == "suspended"
        return LocalFacade(runtime), result.run_id

    async def test_a_parked_run_is_listed_with_what_it_is_asking(
        self, parked: Any
    ) -> None:
        """The regression guard for a bug that returned an empty list.

        ``runtime.history()`` yields ``StepRecord`` — the *public* view, whose
        ``kind`` is a plain string and whose ``EntryStatus.SUSPENDED`` surfaces
        as ``StepStatus.RUNNING``. Filtering on the engine's own enums matched
        nothing and reported "nothing is waiting on a person", which reads as a
        fact rather than as a failure to look.
        """
        facade, run_id = parked
        waiting = await facade.pending()

        assert len(waiting) == 1, "a run parked on a person was not listed"
        row = waiting[0]
        assert row["run_id"] == run_id
        assert row["subject"] == "refund"
        assert row["assignees"] == ["fin@x"]
        assert row["node_id"] == "human.approval"
        assert "approved" in row["response_schema"]["properties"]
        assert row["next_action"] == f"loom respond {run_id} refund --approve"

    async def test_it_says_when_nobody_was_actually_notified(
        self, parked: Any
    ) -> None:
        """LogChannel records without delivering, and must not claim otherwise.

        A row that looked delivered would make a stuck run indistinguishable
        from a patient one — the exact confusion this surface exists to end.
        """
        facade, _ = parked
        row = (await facade.pending())[0]
        assert row["delivered"] is False
        assert row["channel"] == "log"

    async def test_answering_it_clears_it(self, parked: Any) -> None:
        facade, run_id = parked
        run = await facade.respond(
            run_id, "refund", {"approved": True, "responder": "dana@acme.com"}
        )
        assert run["status"] == "completed"
        assert run["output"] == "approved:dana@acme.com"
        assert await facade.pending() == []

    async def test_a_completed_run_is_not_listed(self) -> None:
        from loom.facade import LocalFacade

        channel = AutoRespondChannel(approve=True)
        runtime = Runtime(store=MemoryStore(), human=channel)
        channel.bind(runtime)
        runtime.register(refund_flow)
        await runtime.run(refund_flow, 1.0)
        assert await LocalFacade(runtime).pending() == []

    async def test_the_older_wait_for_approval_is_listed_too(self) -> None:
        """It has no ticket, so the node fields are empty — but it is waiting on
        a person, and omitting it would make the queue quietly incomplete."""
        from loom.facade import LocalFacade

        @workflow(name="old_style_pending")
        async def old(ctx: Context, _: Any = None) -> str:
            return "yes" if await ctx.wait_for_approval("legacy") else "no"

        runtime = Runtime(store=MemoryStore(), human=LogChannel())
        runtime.register(old)
        parked = await runtime.run(old)

        waiting = await LocalFacade(runtime).pending()
        assert [row["subject"] for row in waiting] == ["legacy"]
        assert waiting[0]["run_id"] == parked.run_id
        assert waiting[0]["prompt"] == ""

    async def test_the_node_catalog_is_reachable_through_the_facade(self) -> None:
        from loom.facade import LocalFacade

        facade = LocalFacade(Runtime(store=MemoryStore()))
        assert {row["id"] for row in await facade.nodes(category="human")} >= {
            "human.approval"
        }
        detail = await facade.node("human.approval")
        assert "await ctx.node(" in detail["contract"]

    def test_every_facade_adapter_carries_the_new_capabilities(self) -> None:
        """One port, every surface. The parity suite already asserts the whole
        port; this names the four so a failure says which."""
        from loom.facade import LocalFacade, RemoteFacade
        from loom.identity.facade import AuthorizedFacade

        for adapter in (LocalFacade, RemoteFacade, AuthorizedFacade):
            for method in ("pending", "respond", "nodes", "node"):
                assert hasattr(adapter, method), f"{adapter.__name__}.{method}"


class TestAGuardReturnsWhatTheRunShouldUse:
    """ALLOW and REPLACE must hand back the same *type*.

    ``ctx.guard("guard.pii", PiiIn(value=draft, redact=True))`` passes
    configuration and a subject in one object. REPLACE returned the redacted
    string and ALLOW returned the ``PiiIn`` — so the same call produced a string
    or a model depending on whether the guard found anything, and the caller
    published a repr of the config.
    """

    @pytest.fixture
    def runtime(self) -> Runtime:
        return Runtime(store=MemoryStore())

    async def test_allow_returns_the_subject_not_the_wrapper(
        self, runtime: Runtime
    ) -> None:
        from loom.nodes.guard import PiiIn

        @workflow(name="guard_allow_type")
        async def flow(ctx: Context, draft: str) -> Any:
            return await ctx.guard("guard.pii", PiiIn(value=draft, redact=True))

        runtime.register(flow)
        assert (await runtime.run(flow, "nothing secret here")).output == (
            "nothing secret here"
        )

    async def test_replace_returns_the_same_type_as_allow(
        self, runtime: Runtime
    ) -> None:
        from loom.nodes.guard import PiiIn

        @workflow(name="guard_replace_type")
        async def flow(ctx: Context, draft: str) -> Any:
            return await ctx.guard("guard.pii", PiiIn(value=draft, redact=True))

        runtime.register(flow)
        result = await runtime.run(flow, "key sk-abcdefghijklmnop1234 here")
        assert isinstance(result.output, str)
        assert "[redacted:api_key]" in result.output

    async def test_a_bare_value_passes_straight_through(
        self, runtime: Runtime
    ) -> None:
        @workflow(name="guard_bare")
        async def flow(ctx: Context, text: str) -> Any:
            return await ctx.guard("guard.policy", text)

        runtime.register(flow)
        assert (await runtime.run(flow, "fine")).output == "fine"

    async def test_a_node_guard_still_sees_the_whole_payload(self) -> None:
        """Node guards must not unwrap: there the value *is* the node's Input,
        and a node whose Input happens to have a ``value`` field must receive
        all of it."""
        registry = NodeRegistry(parent=get_node_catalog())
        seen: list[Any] = []

        class Watcher(Node[GuardInput, GuardVerdict]):
            spec = NodeSpec(id="guard.watcher", category=NodeCategory.GUARD)
            Input, Output = GuardInput, GuardVerdict

            async def run(self, ctx: Any, payload: GuardInput) -> GuardVerdict:
                seen.append(payload)
                return GuardVerdict.allow()

        registry.register_node(Watcher)
        runtime = Runtime(store=MemoryStore(), nodes=registry)

        @workflow(name="node_guard_payload")
        async def flow(ctx: Context, _: Any = None) -> Any:
            out = await ctx.node(
                "control.batch",
                {"items": [1, 2, 3], "size": 2},
                guards=["guard.watcher"],
            )
            return out.count

        runtime.register(flow)
        assert (await runtime.run(flow)).output == 2
        # The guard saw the node's payload, wrapped — not a field plucked out.
        assert seen and getattr(seen[0].value, "items", None) == [1, 2, 3]
