"""Tests for the graph/visualization subsystem (Phase 4).

Covers: WGIR data model, registry extraction, AST extraction,
graph emission, Mermaid export, explainer, GraphPatch validation,
run trace overlay, and time-travel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.graph.canvas import (
    GraphPatch,
    PatchApplier,
    PatchOp,
    PatchValidationError,
)
from loom.graph.emitter import (
    emit_graph_json,
    graph_changed,
    load_graph_json,
)
from loom.graph.explainer import (
    Narration,
    SkeletonExplainer,
    verify_completeness,
)
from loom.graph.export import to_mermaid
from loom.graph.extractor import (
    RegistryCollector,
    extract_from_source,
    merge_passes,
)
from loom.graph.timetravel import TimeTraveler
from loom.graph.trace import RunTrace, overlay_journal
from loom.graph.wgir import (
    EdgeKind,
    NodeKind,
    SourceRange,
    WGIREdge,
    WGIRGraph,
    WGIRNode,
)
from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry
from loom.steps.definition import effect, pure

# ---------------------------------------------------------------------------
# WGIR Data Model
# ---------------------------------------------------------------------------


class TestWGIRModel:
    def test_node_kinds_count(self) -> None:
        assert len(NodeKind) == 18

    def test_edge_kinds_count(self) -> None:
        assert len(EdgeKind) == 5

    def test_graph_serialization(self) -> None:
        graph = WGIRGraph(
            flow_id="test-flow",
            nodes=[
                WGIRNode(id="step1", kind=NodeKind.EFFECT, label="fetch"),
                WGIRNode(id="step2", kind=NodeKind.PURE, label="transform"),
            ],
            edges=[
                WGIREdge(
                    source="step1",
                    target="step2",
                    kind=EdgeKind.DATA,
                    variable="data",
                ),
            ],
        )
        json_str = graph.model_dump_json()
        restored = WGIRGraph.model_validate_json(json_str)
        assert len(restored.nodes) == 2
        assert len(restored.edges) == 1
        assert restored.flow_id == "test-flow"

    def test_compute_hash_deterministic(self) -> None:
        g1 = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        g2 = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        assert g1.compute_hash() == g2.compute_hash()

    def test_compute_hash_changes_on_node_change(self) -> None:
        g1 = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        g2 = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="b", kind=NodeKind.PURE, label="b")],
        )
        assert g1.compute_hash() != g2.compute_hash()

    def test_find_node(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="n1", kind=NodeKind.EFFECT, label="fetch")],
        )
        assert graph.find_node("n1") is not None
        assert graph.find_node("missing") is None

    def test_successors_predecessors(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="b"),
                WGIRNode(id="c", kind=NodeKind.EFFECT, label="c"),
            ],
            edges=[
                WGIREdge(source="a", target="b", kind=EdgeKind.CONTROL),
                WGIREdge(source="a", target="c", kind=EdgeKind.DATA),
            ],
        )
        assert set(graph.successors("a")) == {"b", "c"}
        assert graph.predecessors("b") == ["a"]


# ---------------------------------------------------------------------------
# Registry Extraction
# ---------------------------------------------------------------------------


class TestRegistryCollector:
    def test_collect_step_definitions(self) -> None:
        @effect
        async def fetch_data(url: str) -> str:
            """Fetch data from a URL."""
            return f"data-from-{url}"

        @pure
        async def compute(x: int) -> int:
            """Pure computation."""
            return x * 2

        collector = RegistryCollector()
        nodes = collector.collect_steps([fetch_data, compute])
        assert len(nodes) == 2

        fetch_node = next(n for n in nodes if n.id == "fetch_data")
        assert fetch_node.kind is NodeKind.EFFECT
        assert fetch_node.step_class == "effect"
        assert fetch_node.description.startswith("Fetch data")

        compute_node = next(n for n in nodes if n.id == "compute")
        assert compute_node.kind is NodeKind.PURE
        assert compute_node.step_class == "pure"

    def test_collect_empty(self) -> None:
        collector = RegistryCollector()
        assert collector.collect_steps([]) == []


# ---------------------------------------------------------------------------
# AST Extraction
# ---------------------------------------------------------------------------


class TestASTExtractor:
    def test_extract_step_calls(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    data = await ctx.step(fetch_data, input)
    result = await ctx.step(process, data)
    return result
'''
        ext = extract_from_source(source, flow_id="test")
        step_nodes = [
            n for n in ext.nodes if n.kind not in (NodeKind.RETURN,)
        ]
        assert len(step_nodes) >= 2
        labels = {n.label for n in step_nodes}
        assert "fetch_data" in labels
        assert "process" in labels

    def test_extract_if_statement(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    if input > 10:
        await ctx.step(big_handler, input)
    else:
        await ctx.step(small_handler, input)
'''
        ext = extract_from_source(source, flow_id="test")
        switch_nodes = [n for n in ext.nodes if n.kind is NodeKind.SWITCH]
        assert len(switch_nodes) == 1

    def test_extract_for_loop(self) -> None:
        source = '''
async def my_workflow(ctx, items):
    for item in items:
        await ctx.step(process, item)
'''
        ext = extract_from_source(source, flow_id="test")
        loop_nodes = [n for n in ext.nodes if n.kind is NodeKind.LOOP]
        assert len(loop_nodes) == 1

    def test_extract_map(self) -> None:
        source = '''
async def my_workflow(ctx, items):
    results = await ctx.map(items, process_item)
'''
        ext = extract_from_source(source, flow_id="test")
        map_nodes = [n for n in ext.nodes if n.kind is NodeKind.MAP]
        assert len(map_nodes) == 1

    def test_extract_gather(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    a, b = await ctx.gather(
        ctx.step(fetch_a, input),
        ctx.step(fetch_b, input),
    )
'''
        ext = extract_from_source(source, flow_id="test")
        parallel_nodes = [
            n for n in ext.nodes if n.kind is NodeKind.PARALLEL
        ]
        assert len(parallel_nodes) == 1

    def test_extract_wait(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    await ctx.sleep(60)
    payload = await ctx.wait_for_event("approval")
'''
        ext = extract_from_source(source, flow_id="test")
        wait_nodes = [n for n in ext.nodes if n.kind is NodeKind.WAIT]
        assert len(wait_nodes) == 2

    def test_extract_agent(self) -> None:
        source = '''
async def my_workflow(ctx, ticket):
    result = await ctx.agent(triage_agent, ticket)
'''
        ext = extract_from_source(source, flow_id="test")
        agent_nodes = [n for n in ext.nodes if n.kind is NodeKind.AGENT]
        assert len(agent_nodes) == 1

    def test_extract_emit(self) -> None:
        source = '''
async def my_workflow(ctx, data):
    await ctx.emit("order.completed", data)
'''
        ext = extract_from_source(source, flow_id="test")
        emit_nodes = [n for n in ext.nodes if n.kind is NodeKind.EMIT]
        assert len(emit_nodes) == 1

    def test_control_edges(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    a = await ctx.step(step_a, input)
    b = await ctx.step(step_b, a)
    return b
'''
        ext = extract_from_source(source, flow_id="test")
        assert len(ext.edges) > 0
        assert all(e.kind is EdgeKind.CONTROL for e in ext.edges)

    def test_syntax_error_returns_empty(self) -> None:
        ext = extract_from_source("not valid {{python}}", flow_id="test")
        assert ext.nodes == []

    def test_return_node(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    return "done"
'''
        ext = extract_from_source(source, flow_id="test")
        return_nodes = [n for n in ext.nodes if n.kind is NodeKind.RETURN]
        assert len(return_nodes) == 1


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_registry_and_ast(self) -> None:
        registry = [
            WGIRNode(
                id="fetch",
                kind=NodeKind.EFFECT,
                label="fetch",
                step_class="effect",
            ),
        ]
        ast_nodes = [
            WGIRNode(
                id="fetch",
                kind=NodeKind.EFFECT,
                label="fetch",
                source=SourceRange(
                    file="test.py", start_line=10, end_line=15
                ),
            ),
            WGIRNode(id="switch", kind=NodeKind.SWITCH, label="if"),
        ]
        ast_edges = [
            WGIREdge(
                source="fetch", target="switch", kind=EdgeKind.CONTROL
            ),
        ]

        graph = merge_passes(
            registry, ast_nodes, ast_edges, flow_id="test"
        )
        assert len(graph.nodes) == 2
        assert len(graph.edges) == 1

        # Registry wins on identity
        fetch = graph.find_node("fetch")
        assert fetch is not None
        assert fetch.step_class == "effect"
        # AST wins on source range
        assert fetch.source is not None
        assert fetch.source.start_line == 10

        # Hash is set
        assert graph.extraction_hash != ""


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class TestEmitter:
    def test_emit_and_load(self, tmp_path: Path) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        ).finalize()

        out = tmp_path / "test.graph.json"
        emit_graph_json(graph, out)
        assert out.exists()

        loaded = load_graph_json(out)
        assert loaded.flow_id == "test"
        assert len(loaded.nodes) == 1

    def test_graph_changed_detection(self, tmp_path: Path) -> None:
        g1 = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        ).finalize()

        out = tmp_path / "test.graph.json"
        emit_graph_json(g1, out)

        # Same graph → not changed
        assert graph_changed(out, g1) is False

        # Different graph → changed
        g2 = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.PURE, label="b"),
            ],
        ).finalize()
        assert graph_changed(out, g2) is True

    def test_graph_changed_no_file(self, tmp_path: Path) -> None:
        g = WGIRGraph(flow_id="test").finalize()
        assert graph_changed(tmp_path / "missing.json", g) is True


# ---------------------------------------------------------------------------
# Mermaid Export
# ---------------------------------------------------------------------------


class TestMermaidExport:
    def test_basic_export(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="fetch", kind=NodeKind.EFFECT, label="Fetch Data"),
                WGIRNode(id="process", kind=NodeKind.PURE, label="Process"),
                WGIRNode(id="done", kind=NodeKind.RETURN, label="Return"),
            ],
            edges=[
                WGIREdge(
                    source="fetch", target="process", kind=EdgeKind.DATA
                ),
                WGIREdge(
                    source="process", target="done", kind=EdgeKind.CONTROL
                ),
            ],
        )
        mermaid = to_mermaid(graph)
        assert "flowchart TB" in mermaid
        assert "fetch" in mermaid
        assert "process" in mermaid
        assert "-->" in mermaid

    def test_with_title(self) -> None:
        graph = WGIRGraph(flow_id="test", nodes=[], edges=[])
        mermaid = to_mermaid(graph, title="My Workflow")
        assert "title: My Workflow" in mermaid

    def test_edge_label(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="A"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="B"),
            ],
            edges=[
                WGIREdge(
                    source="a",
                    target="b",
                    kind=EdgeKind.DATA,
                    label="data",
                ),
            ],
        )
        mermaid = to_mermaid(graph)
        assert "|data|" in mermaid

    def test_error_edge_style(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="A"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="B"),
            ],
            edges=[
                WGIREdge(source="a", target="b", kind=EdgeKind.ERROR),
            ],
        )
        mermaid = to_mermaid(graph)
        assert "-.->", mermaid


# ---------------------------------------------------------------------------
# Explainer
# ---------------------------------------------------------------------------


class TestExplainer:
    @pytest.mark.asyncio
    async def test_skeleton_explainer(self) -> None:
        graph = WGIRGraph(
            flow_id="support-triage",
            nodes=[
                WGIRNode(
                    id="classify",
                    kind=NodeKind.AGENT,
                    label="classify",
                    description="Classify the support ticket",
                ),
                WGIRNode(
                    id="route",
                    kind=NodeKind.SWITCH,
                    label="route",
                    description="Route based on priority",
                ),
            ],
        )

        explainer = SkeletonExplainer()
        narration = await explainer.narrate(graph)

        assert isinstance(narration, Narration)
        assert "support-triage" in narration.summary
        assert "classify" in narration.node_descriptions
        assert "route" in narration.node_descriptions
        assert narration.full_text != ""

    @pytest.mark.asyncio
    async def test_completeness_check(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.PURE, label="b"),
            ],
        )
        # Complete narration
        narration = Narration(
            node_descriptions={"a": "does A", "b": "does B"}
        )
        assert verify_completeness(narration, graph) == []

        # Incomplete narration
        partial = Narration(node_descriptions={"a": "does A"})
        missing = verify_completeness(partial, graph)
        assert "b" in missing


# ---------------------------------------------------------------------------
# GraphPatch
# ---------------------------------------------------------------------------


class TestGraphPatch:
    def _sample_graph(self) -> WGIRGraph:
        return WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(
                    id="fetch",
                    kind=NodeKind.EFFECT,
                    label="fetch",
                    source=SourceRange(
                        file="test.py", start_line=10, end_line=15
                    ),
                ),
                WGIRNode(
                    id="process",
                    kind=NodeKind.PURE,
                    label="process",
                    source=SourceRange(
                        file="test.py", start_line=17, end_line=20
                    ),
                ),
            ],
            edges=[
                WGIREdge(
                    source="fetch",
                    target="process",
                    kind=EdgeKind.DATA,
                    variable="data",
                ),
            ],
        )

    def test_set_layout_always_valid(self) -> None:
        applier = PatchApplier()
        result = applier.validate(
            GraphPatch(
                op=PatchOp.SET_LAYOUT,
                target="fetch",
                params={"x": 100, "y": 200},
            ),
            self._sample_graph(),
        )
        assert result.applied is True

    def test_set_param_valid(self) -> None:
        applier = PatchApplier()
        result = applier.validate(
            GraphPatch(
                op=PatchOp.SET_PARAM,
                target="fetch",
                params={"param": "url", "value": "https://new.api"},
            ),
            self._sample_graph(),
        )
        assert result.applied is True

    def test_set_param_missing_node(self) -> None:
        applier = PatchApplier()
        with pytest.raises(PatchValidationError, match="not found"):
            applier.validate(
                GraphPatch(op=PatchOp.SET_PARAM, target="missing"),
                self._sample_graph(),
            )

    def test_remove_blocked_by_data_dep(self) -> None:
        applier = PatchApplier()
        with pytest.raises(PatchValidationError, match="output referenced"):
            applier.validate(
                GraphPatch(op=PatchOp.REMOVE_NODE, target="fetch"),
                self._sample_graph(),
            )

    def test_remove_leaf_ok(self) -> None:
        applier = PatchApplier()
        result = applier.validate(
            GraphPatch(op=PatchOp.REMOVE_NODE, target="process"),
            self._sample_graph(),
        )
        assert result.applied is True

    def test_insert_node(self) -> None:
        applier = PatchApplier()
        result = applier.validate(
            GraphPatch(
                op=PatchOp.INSERT_NODE,
                target="new_step",
                params={"after": "fetch"},
            ),
            self._sample_graph(),
        )
        assert result.applied is True

    def test_set_policy(self) -> None:
        applier = PatchApplier()
        result = applier.validate(
            GraphPatch(
                op=PatchOp.SET_POLICY,
                target="fetch",
                params={"max_retries": 5},
            ),
            self._sample_graph(),
        )
        assert result.applied is True


# ---------------------------------------------------------------------------
# Run Trace Overlay
# ---------------------------------------------------------------------------


class TestRunTrace:
    def test_overlay_journal(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="fetch", kind=NodeKind.EFFECT, label="fetch"),
                WGIRNode(id="process", kind=NodeKind.PURE, label="process"),
            ],
        )
        journal = [
            JournalEntry(
                path="fetch",
                name="fetch",
                kind=EntryKind.STEP,
                status=EntryStatus.COMPLETED,
                output="data-123",
            ),
        ]

        trace = overlay_journal(graph, journal, run_id="run-1")
        assert isinstance(trace, RunTrace)
        assert trace.node_traces["fetch"].status == "completed"
        assert trace.node_traces["process"].status == "pending"
        assert "data-123" in trace.node_traces["fetch"].output_preview

    def test_overlay_empty_journal(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
            ],
        )
        trace = overlay_journal(graph, [])
        assert trace.node_traces["a"].status == "pending"


# ---------------------------------------------------------------------------
# Time-Travel
# ---------------------------------------------------------------------------


class TestTimeTravel:
    def test_snapshot_at_sequence(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="b"),
                WGIRNode(id="c", kind=NodeKind.EFFECT, label="c"),
            ],
        )
        journal = [
            JournalEntry(
                path="a",
                name="a",
                kind=EntryKind.STEP,
                status=EntryStatus.COMPLETED,
            ),
            JournalEntry(
                path="b",
                name="b",
                kind=EntryKind.STEP,
                status=EntryStatus.COMPLETED,
            ),
            JournalEntry(
                path="c",
                name="c",
                kind=EntryKind.STEP,
                status=EntryStatus.COMPLETED,
            ),
        ]

        tt = TimeTraveler(graph, journal)
        assert tt.max_seq == 2

        # At seq 0: only 'a' completed
        snap = tt.snapshot_at(0)
        assert snap.find_node("a").metadata["status"] == "completed"
        assert snap.find_node("b").metadata["status"] == "pending"
        assert snap.find_node("c").metadata["status"] == "pending"

        # At seq 1: 'a' and 'b' completed
        snap = tt.snapshot_at(1)
        assert snap.find_node("a").metadata["status"] == "completed"
        assert snap.find_node("b").metadata["status"] == "completed"
        assert snap.find_node("c").metadata["status"] == "pending"

        # At seq 2: all completed
        snap = tt.snapshot_at(2)
        assert snap.find_node("c").metadata["status"] == "completed"

    def test_snapshot_with_failure(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="b"),
            ],
        )
        journal = [
            JournalEntry(
                path="a",
                name="a",
                kind=EntryKind.STEP,
                status=EntryStatus.COMPLETED,
            ),
            JournalEntry(
                path="b",
                name="b",
                kind=EntryKind.STEP,
                status=EntryStatus.FAILED,
            ),
        ]

        tt = TimeTraveler(graph, journal)
        snap = tt.snapshot_at(1)
        assert snap.find_node("b").metadata["status"] == "failed"
