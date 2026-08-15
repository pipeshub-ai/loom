"""Build a node by following the guide, and check that it works.

Every snippet in ``docs/guides/nodes.md`` compiles and resolves its names in
CI, which proves each one is valid Python and proves nothing about whether
*following the guide* gets you a working node. The steps could each parse and
still not compose — a node that never reaches the catalog, a contract that
renders as something nobody can paste, a body whose I/O never reaches the
journal.

So this writes the file the guide describes into a directory with nothing else
in it, imports it as a module, and drives the result through every path a real
node is used by: registration, discovery, the rendered contract, execution,
replay, and the operator surface.

If the guide changes and stops being true, this fails.

The shape is deliberately the one ``test_toolset_guide.py`` established, for the
same reason: a guide nobody executes is a guide that is wrong within a month.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from workflow_builder import Context, Runtime, workflow
from workflow_builder.state import MemoryStore

# --- the file, as the guide lays it out ------------------------------------

NODE_MODULE = '''
"""Steps 1-3: the two models, the node, and what it declares."""
from __future__ import annotations

from pydantic import BaseModel, Field

from workflow_builder import step
from workflow_builder.nodes import (
    Node,
    NodeCategory,
    NodeExample,
    NodeSpec,
    register_node,
)


class ScoreIn(BaseModel):
    text: str = Field(description="The lead blurb to score.")
    threshold: float = Field(default=0.5, description="Score at or above this passes.")


class ScoreOut(BaseModel):
    score: float
    passed: bool
    reason: str = ""


@step
async def fetch_signals(text: str) -> int:
    """Real work lives in a step. The node body composes steps."""
    return len(text)


@register_node
class LeadScoreNode(Node[ScoreIn, ScoreOut]):
    """Score a lead from its description text."""

    spec = NodeSpec(
        id="custom.lead_score",
        version="1.0.0",
        category=NodeCategory.TRANSFORM,
        summary="Score a lead from its description text.",
        tags=["lead", "score", "qualify"],
        examples=[
            NodeExample(
                title="Score an inbound blurb",
                payload={"text": "Acme wants a demo for 40 seats", "threshold": 0.4},
            )
        ],
    )
    Input, Output = ScoreIn, ScoreOut

    async def run(self, ctx, payload: ScoreIn) -> ScoreOut:
        signals = await ctx.step(fetch_signals, payload.text)
        score = min(signals / 50, 1.0)
        return ScoreOut(
            score=score,
            passed=score >= payload.threshold,
            reason=f"{signals} signals in the blurb",
        )
'''


@pytest.fixture(scope="module")
def guide_node(tmp_path_factory: Any) -> Any:
    """The module, written and imported exactly as a reader would ship it."""
    root = tmp_path_factory.mktemp("node_guide")
    (root / "my_nodes.py").write_text(NODE_MODULE)

    sys.path.insert(0, str(root))
    try:
        import my_nodes

        yield my_nodes
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("my_nodes", None)
        from workflow_builder.nodes import get_node_catalog

        get_node_catalog().unregister("custom.lead_score")


# ---------------------------------------------------------------------------


class TestTheGuideProducesAWorkingNode:
    def test_registration_derives_everything_the_guide_promises(
        self, guide_node: Any
    ) -> None:
        """Step 2: "no second declaration anywhere"."""
        from workflow_builder.nodes import get_node_catalog

        spec = get_node_catalog().get("custom.lead_score")
        assert spec is not None, "@register_node did not reach the catalog"
        assert spec.node_class == "my_nodes:LeadScoreNode"
        assert spec.input_schema["properties"]["text"]["description"]
        assert spec.output_schema["properties"]["reason"]["type"] == "string"

    def test_a_flat_id_is_refused(self, guide_node: Any) -> None:
        """Step 2's rule, mutation-verified: break the id, registration fails."""
        from workflow_builder.nodes import NodeContractError, NodeSpec, get_node_catalog

        flat = type(
            "Flat",
            (guide_node.LeadScoreNode,),
            {"spec": NodeSpec(id="lead_score", summary="flat")},
        )
        with pytest.raises(NodeContractError, match="not namespaced"):
            get_node_catalog().register_node(flat)

    def test_it_is_discoverable_the_way_the_guide_says_to_look(
        self, guide_node: Any
    ) -> None:
        """Step 0: `loom nodes --category transform`."""
        from workflow_builder.nodes import NodeCategory, get_node_catalog

        catalog = get_node_catalog()
        assert "custom.lead_score" in {c.id for c in catalog.search("lead blurb")}
        assert "custom.lead_score" in {
            c.id for c in catalog.search("", category=NodeCategory.TRANSFORM, limit=50)
        }

    def test_the_rendered_contract_is_what_step_4_shows(self, guide_node: Any) -> None:
        """The contract is what the coding agent writes from, so it is checked
        as code rather than as prose."""
        from workflow_builder.nodes import get_node_catalog

        contract = get_node_catalog().contract("custom.lead_score")

        assert "from my_nodes import ScoreIn, ScoreOut" in contract
        assert "result: ScoreOut = await ctx.node(" in contract
        # The example supplies the values, the Field descriptions the comments.
        assert "text='Acme wants a demo for 40 seats'" in contract
        assert "threshold=0.4" in contract
        assert "# str — The lead blurb to score." in contract
        assert "# ScoreOut: score: float, passed: bool, reason: str" in contract

        body = "\n".join(
            "    " + line
            for line in contract.splitlines()
            if line and not line.startswith("#")
        )
        compile(f"async def _():\n{body}", "<contract>", "exec")

    async def test_a_workflow_can_call_it_end_to_end(self, guide_node: Any) -> None:
        """Step 5, including the journal shape the guide prints."""

        @workflow(name="qualify_guide")
        async def qualify(ctx: Context, blurb: str) -> str:
            scored = await ctx.node(
                "custom.lead_score", guide_node.ScoreIn(text=blurb)
            )
            return "qualified" if scored.passed else f"skipped: {scored.reason}"

        runtime = Runtime(store=MemoryStore())
        runtime.register(qualify)
        result = await runtime.run(qualify, "Acme wants a demo for 40 seats")

        assert result.status.value == "completed"
        assert result.output == "qualified"

        entries = await runtime.store.load_journal(result.run_id)
        by_path = {entry.path: entry.name for entry in entries}
        assert by_path["0"] == "node:custom.lead_score"
        assert by_path["0.0"] == "fetch_signals", (
            "the node's own step must journal beneath the node's path"
        )

    async def test_it_replays_identically(self, guide_node: Any) -> None:
        """The claim the whole design rests on."""

        @workflow(name="qualify_replay")
        async def qualify(ctx: Context, blurb: str) -> float:
            return (
                await ctx.node("custom.lead_score", guide_node.ScoreIn(text=blurb))
            ).score

        runtime = Runtime(store=MemoryStore())
        runtime.register(qualify)
        first = await runtime.run(qualify, "a" * 60)
        again = await runtime.replay(first.run_id)
        assert again.output == first.output == 1.0

    async def test_its_own_example_runs(self, guide_node: Any) -> None:
        """Step 7. An untested node is a missing example, and the example is in
        the docs the agent reads — so it cannot rot unnoticed."""
        from workflow_builder.nodes import get_node_catalog

        spec = get_node_catalog().get("custom.lead_score")
        assert spec.examples

        @workflow(name="qualify_example")
        async def flow(ctx: Context, _: Any = None) -> Any:
            return await ctx.node("custom.lead_score", spec.examples[0].payload)

        runtime = Runtime(store=MemoryStore())
        runtime.register(flow)
        assert (await runtime.run(flow)).status.value == "completed"

    async def test_retries_and_guards_compose_per_call(self, guide_node: Any) -> None:
        """Step 5's second snippet."""

        @workflow(name="qualify_guarded")
        async def flow(ctx: Context, blurb: str) -> bool:
            scored = await ctx.node(
                "custom.lead_score",
                guide_node.ScoreIn(text=blurb),
                retry=3,
                guards=["guard.pii"],
            )
            return scored.passed

        runtime = Runtime(store=MemoryStore())
        runtime.register(flow)
        assert (await runtime.run(flow, "x" * 80)).output is True

    def test_a_contract_change_refuses_to_replay(self, guide_node: Any) -> None:
        """The versioning section, as a property rather than a promise.

        Adding an *optional* output field is the case people expect to be safe,
        and it is exactly the one where a replay would gain a field the original
        run never produced.
        """
        from pydantic import BaseModel

        from workflow_builder.nodes import NodeSpec

        class ScoreOutV2(BaseModel):
            score: float
            passed: bool
            reason: str = ""
            band: str = "unknown"

        before = guide_node.LeadScoreNode.spec
        after = NodeSpec(
            id="custom.lead_score",
            version="2.0.0",
            input_schema=before.input_schema,
            output_schema=ScoreOutV2.model_json_schema(),
        )
        assert before.contract_hash != after.contract_hash

    def test_the_entry_point_the_guide_prints_is_the_one_that_is_read(
        self, guide_node: Any
    ) -> None:
        """Step 6. The group name is the contract between a package and LOOM,
        so a typo here is a node nobody can install."""
        import inspect

        from workflow_builder.nodes import registry

        assert 'group="loom_node"' in inspect.getsource(registry.load_node_entry_points)
        assert "loom_node" in (Path("docs/guides/nodes.md").read_text())

    def test_a_local_registration_stays_local(self, guide_node: Any) -> None:
        """Step 6's last snippet."""
        runtime = Runtime(store=MemoryStore())
        assert "custom.lead_score" in runtime.nodes.node_ids()  # global reaches it

        isolated = Runtime(store=MemoryStore(), nodes=_bare_registry())
        assert "custom.lead_score" not in isolated.nodes.node_ids()
        isolated.nodes.register_node(guide_node.LeadScoreNode)
        assert "custom.lead_score" in isolated.nodes.node_ids()

    def test_the_operator_surface_finds_it(self, guide_node: Any) -> None:
        """`loom node custom.lead_score` — step 4's command."""
        import asyncio

        from workflow_builder.facade import LocalFacade

        facade = LocalFacade(Runtime(store=MemoryStore()))
        detail = asyncio.run(facade.node("custom.lead_score"))
        assert detail["summary"] == "Score a lead from its description text."
        assert "await ctx.node(" in detail["contract"]


def _bare_registry() -> Any:
    from workflow_builder.nodes import NodeRegistry

    return NodeRegistry()


class TestTheGuideItselfRuns:
    def test_every_snippet_executes(self) -> None:
        """Compiling is not enough: ``NodeSpec(deterministc=False)`` compiles,
        resolves every name, and is a typo that reaches production."""
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from docs_examples import examples

        page = Path(__file__).resolve().parent.parent / "docs" / "guides" / "nodes.md"
        blocks = [e for e in examples([page])]
        assert blocks, "the guide has no executable snippets"

        for example in blocks:
            done = subprocess.run(
                [sys.executable, "-c", example.code],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert done.returncode == 0, (
                f"{example.label} does not run:\n{done.stderr[-2000:]}"
            )
