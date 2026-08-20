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
from loom.graph.layout import compute_layout
from loom.graph.reactflow import to_react_flow
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

    def test_a_step_calls_explicit_name_kwarg_overrides_the_function_label(self) -> None:
        """`ctx.step(bridge, "jira.create_issue", name="jira.create_issue")`
        journals under `name`, not `bridge`'s own name -- the graph label
        must match what a run trace actually shows for the same call."""
        source = '''
async def my_workflow(ctx, input):
    a = await ctx.step(generic_bridge, "jira.create_issue", name="jira.create_issue")
    b = await ctx.step(generic_bridge, "slack.post_message", name="slack.post_message")
'''
        ext = extract_from_source(source, flow_id="test")
        labels = {n.label for n in ext.nodes if n.kind is not NodeKind.RETURN}
        assert labels == {"jira.create_issue", "slack.post_message"}

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

    def test_agent_with_a_literal_prompt_gets_a_short_label_and_the_full_text_as_description(
        self,
    ) -> None:
        """A long inline prompt used to become the node's *label* verbatim --
        an unreadable card title and (via `_alloc_id`) an unreadable node id.
        The label should read as a short title; the full prompt should still
        reach the graph, just as `description` instead."""
        prompt = (
            "Write exactly one short, family-friendly joke for Abhishek. "
            "Return only the joke: no greeting, explanation, quotation marks, "
            "follow-up question, or extra lines."
        )
        source = f'''
async def my_workflow(ctx, input):
    result = await ctx.agent(
        {prompt!r}
    )
    return result.text().strip()
'''
        ext = extract_from_source(source, flow_id="test")
        agent_node = next(n for n in ext.nodes if n.kind is NodeKind.AGENT)

        assert len(agent_node.label) < len(prompt)
        assert agent_node.label.startswith("Write exactly one short")
        assert agent_node.description == prompt
        # The id is derived from the (already short) label, not the raw
        # prompt, and stays free of spaces/punctuation.
        assert len(agent_node.id) <= 48
        assert " " not in agent_node.id

    def test_a_short_agent_prompt_is_not_truncated(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    result = await ctx.agent("Say hi")
'''
        ext = extract_from_source(source, flow_id="test")
        agent_node = next(n for n in ext.nodes if n.kind is NodeKind.AGENT)
        assert agent_node.label == "Say hi"
        assert agent_node.description == "Say hi"

    def test_return_description_names_the_returned_expression(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    result = await ctx.step(fetch_data, input)
    return result.text().strip()
'''
        ext = extract_from_source(source, flow_id="test")
        return_node = next(n for n in ext.nodes if n.kind is NodeKind.RETURN)
        assert "result.text().strip()" in return_node.description

    def test_switch_and_loop_descriptions_name_their_condition(self) -> None:
        source = '''
async def my_workflow(ctx, items):
    if len(items) > 10:
        await ctx.step(big_handler, items)
    for item in items:
        await ctx.step(process, item)
'''
        ext = extract_from_source(source, flow_id="test")
        switch_node = next(n for n in ext.nodes if n.kind is NodeKind.SWITCH)
        loop_node = next(n for n in ext.nodes if n.kind is NodeKind.LOOP)
        assert "len(items) > 10" in switch_node.description
        assert "item" in loop_node.description and "items" in loop_node.description

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
        """Paths are the shape the *engine* produces — ordinals, not names.

        This test used to build its entry with ``path="fetch"``, which no run
        has ever written, and it passed while the overlay matched nothing at
        all in production: a completed run rendered with every node pending.
        A fixture that cannot occur proves nothing about the code that has to
        handle the fixtures that do.
        """
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="fetch", kind=NodeKind.EFFECT, label="fetch"),
                WGIRNode(id="process", kind=NodeKind.PURE, label="process"),
            ],
        )
        journal = [
            JournalEntry(
                path="0",
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

    def test_a_journal_entry_naming_nothing_is_reported(self) -> None:
        """A silently empty overlay is indistinguishable from a run that did
        nothing, which is how the previous implementation stayed broken."""
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        trace = overlay_journal(
            graph,
            [
                JournalEntry(
                    path="0",
                    name="not_in_the_graph",
                    kind=EntryKind.STEP,
                    status=EntryStatus.COMPLETED,
                )
            ],
        )
        assert trace.unmatched_entries == ["not_in_the_graph"]


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


# ---------------------------------------------------------------------------
# Edge condition labels (if/else, loops)
# ---------------------------------------------------------------------------


class TestEdgeConditionLabels:
    def test_if_else_labels_true_and_false(self) -> None:
        source = '''
async def my_workflow(ctx, input):
    if input > 10:
        await ctx.step(big_handler, input)
    else:
        await ctx.step(small_handler, input)
'''
        ext = extract_from_source(source, flow_id="test")
        switch_id = next(n.id for n in ext.nodes if n.kind is NodeKind.SWITCH)
        by_target = {e.target: e for e in ext.edges if e.source == switch_id}
        assert by_target["big_handler"].label == "True"
        assert by_target["small_handler"].label == "False"
        assert by_target["big_handler"].condition == "input > 10"
        assert by_target["small_handler"].condition == "not (input > 10)"

    def test_if_else_branches_reconverge_after_the_block(self) -> None:
        """A statement after `if/else` is reachable from *either* branch --
        both must show up as a predecessor, not just whichever branch the
        AST visitor happened to finish last."""
        source = '''
async def my_workflow(ctx, input):
    if input > 10:
        await ctx.step(big_handler, input)
    else:
        await ctx.step(small_handler, input)
    await ctx.step(finish, input)
'''
        ext = extract_from_source(source, flow_id="test")
        finish_id = next(n.id for n in ext.nodes if n.label == "finish")
        preds = {e.source for e in ext.edges if e.target == finish_id}
        assert preds == {"big_handler", "small_handler"}

    def test_if_without_else_reconnects_from_switch(self) -> None:
        """No `else` means the false outcome skips straight from the switch
        to whatever comes next, not through the true branch."""
        source = '''
async def my_workflow(ctx, input):
    if input > 10:
        await ctx.step(big_handler, input)
    await ctx.step(finish, input)
'''
        ext = extract_from_source(source, flow_id="test")
        switch_id = next(n.id for n in ext.nodes if n.kind is NodeKind.SWITCH)
        finish_id = next(n.id for n in ext.nodes if n.label == "finish")
        preds = {e.source for e in ext.edges if e.target == finish_id}
        assert preds == {"big_handler", switch_id}

    def test_if_without_else_where_true_branch_returns_forks_not_chains(self) -> None:
        """`if x: return A` followed by `return B` (no `else`) must produce a
        switch with two branch edges to two independent `return` nodes -- not
        a linear chain from the first `return` into the second, which is
        what an unreachable "return -> return" edge would otherwise draw."""
        source = '''
async def my_workflow(ctx, error_type):
    if error_type:
        return "failed"
    return "succeeded"
'''
        ext = extract_from_source(source, flow_id="test")
        return_nodes = [n for n in ext.nodes if n.kind is NodeKind.RETURN]
        assert len(return_nodes) == 2
        return_ids = {n.id for n in return_nodes}

        # No edge should run from one return node to the other.
        assert not any(
            e.source in return_ids and e.target in return_ids for e in ext.edges
        )

        switch_id = next(n.id for n in ext.nodes if n.kind is NodeKind.SWITCH)
        by_target = {e.target: e for e in ext.edges if e.source == switch_id}
        assert set(by_target) == return_ids
        assert {e.label for e in by_target.values()} == {"True", "False"}

    def test_if_else_both_branches_return_leaves_dead_code_unwired(self) -> None:
        """When both branches of an `if`/`else` return, nothing after the
        `if` is reachable -- it must not be wired to either branch tail."""
        source = '''
async def my_workflow(ctx, x):
    if x:
        return "a"
    else:
        return "b"
    await ctx.step(unreachable, x)
'''
        ext = extract_from_source(source, flow_id="test")
        unreachable_id = next(n.id for n in ext.nodes if n.label == "unreachable")
        preds = {e.source for e in ext.edges if e.target == unreachable_id}
        assert preds == set()

    def test_for_loop_back_edge_and_done_label(self) -> None:
        source = '''
async def my_workflow(ctx, items):
    for item in items:
        await ctx.step(process, item)
    await ctx.step(finish, items)
'''
        ext = extract_from_source(source, flow_id="test")
        loop_id = next(n.id for n in ext.nodes if n.kind is NodeKind.LOOP)
        back_edge = next(e for e in ext.edges if e.target == loop_id)
        assert back_edge.source == "process"
        assert back_edge.label == "loop"

        exit_edge = next(e for e in ext.edges if e.source == loop_id and e.target != "process")
        assert exit_edge.target == "finish"
        assert exit_edge.label == "done"


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------


class TestLayoutEngine:
    def test_empty_graph(self) -> None:
        graph = WGIRGraph(flow_id="test", nodes=[], edges=[])
        assert compute_layout(graph) == {}

    def test_single_node_at_origin(self) -> None:
        graph = WGIRGraph(
            flow_id="test", nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")]
        )
        positions = compute_layout(graph)
        assert positions == {"a": (0.0, 0.0)}

    def test_linear_chain_advances_one_column_per_node(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id=n, kind=NodeKind.EFFECT, label=n)
                for n in ("a", "b", "c", "d", "e")
            ],
            edges=[
                WGIREdge(source=s, target=t, kind=EdgeKind.CONTROL)
                for s, t in [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")]
            ],
        )
        positions = compute_layout(graph)
        xs = [positions[n][0] for n in ("a", "b", "c", "d", "e")]
        assert xs == sorted(xs)
        assert len(set(xs)) == 5
        # A linear chain has no siblings, so every node stays on one row.
        assert len({positions[n][1] for n in ("a", "b", "c", "d", "e")}) == 1

    def test_branching_produces_two_rows_in_the_same_column(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="switch", kind=NodeKind.SWITCH, label="if"),
                WGIRNode(id="big", kind=NodeKind.EFFECT, label="big"),
                WGIRNode(id="small", kind=NodeKind.EFFECT, label="small"),
            ],
            edges=[
                WGIREdge(source="switch", target="big", kind=EdgeKind.CONTROL, label="True"),
                WGIREdge(source="switch", target="small", kind=EdgeKind.CONTROL, label="False"),
            ],
        )
        positions = compute_layout(graph)
        assert positions["big"][0] == positions["small"][0]
        assert positions["big"][0] > positions["switch"][0]
        assert positions["big"][1] != positions["small"][1]

    def test_parallel_gather_places_branches_side_by_side(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="start", kind=NodeKind.EFFECT, label="start"),
                WGIRNode(id="fetch_a", kind=NodeKind.EFFECT, label="fetch_a"),
                WGIRNode(id="fetch_b", kind=NodeKind.EFFECT, label="fetch_b"),
                WGIRNode(id="join", kind=NodeKind.PARALLEL, label="gather"),
            ],
            edges=[
                WGIREdge(source="start", target="fetch_a", kind=EdgeKind.CONTROL),
                WGIREdge(source="start", target="fetch_b", kind=EdgeKind.CONTROL),
                WGIREdge(source="fetch_a", target="join", kind=EdgeKind.CONTROL),
                WGIREdge(source="fetch_b", target="join", kind=EdgeKind.CONTROL),
            ],
        )
        positions = compute_layout(graph)
        # Both parallel branches land in the same column, distinct rows,
        # and converge into a later column.
        assert positions["fetch_a"][0] == positions["fetch_b"][0]
        assert positions["fetch_a"][1] != positions["fetch_b"][1]
        assert positions["join"][0] > positions["fetch_a"][0]

    def test_cyclic_loop_terminates_and_lays_out_forward(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="loop", kind=NodeKind.LOOP, label="for"),
                WGIRNode(id="process", kind=NodeKind.EFFECT, label="process"),
                WGIRNode(id="finish", kind=NodeKind.EFFECT, label="finish"),
            ],
            edges=[
                WGIREdge(source="loop", target="process", kind=EdgeKind.CONTROL),
                WGIREdge(source="process", target="loop", kind=EdgeKind.CONTROL, label="loop"),
                WGIREdge(source="loop", target="finish", kind=EdgeKind.CONTROL, label="done"),
            ],
        )
        positions = compute_layout(graph)  # must not hang
        assert positions["process"][0] > positions["loop"][0]
        assert positions["finish"][0] > positions["loop"][0]

    def test_self_loop_does_not_hang(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.LOOP, label="a")],
            edges=[WGIREdge(source="a", target="a", kind=EdgeKind.CONTROL)],
        )
        positions = compute_layout(graph)
        assert positions == {"a": (0.0, 0.0)}

    def test_tb_direction_swaps_axes(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="b", kind=NodeKind.EFFECT, label="b"),
            ],
            edges=[WGIREdge(source="a", target="b", kind=EdgeKind.CONTROL)],
        )
        lr = compute_layout(graph, direction="LR")
        tb = compute_layout(graph, direction="TB")
        assert lr["b"][0] > lr["a"][0]
        assert tb["b"][1] > tb["a"][1]

    def test_disconnected_nodes_do_not_crash(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="a", kind=NodeKind.EFFECT, label="a"),
                WGIRNode(id="orphan", kind=NodeKind.EFFECT, label="orphan"),
            ],
            edges=[],
        )
        positions = compute_layout(graph)
        assert set(positions) == {"a", "orphan"}


# ---------------------------------------------------------------------------
# Enhanced React Flow export
# ---------------------------------------------------------------------------


class TestEnhancedReactFlowExport:
    def test_display_name_conversion(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="prepare_joke", kind=NodeKind.EFFECT, label="prepare_joke")],
        )
        payload = to_react_flow(graph)
        assert payload["nodes"][0]["data"]["displayName"] == "Prepare Joke"

    def test_display_name_leaves_dotted_tool_ids_alone(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="n", kind=NodeKind.TOOL, label="jira.search_issues")],
        )
        payload = to_react_flow(graph)
        assert payload["nodes"][0]["data"]["displayName"] == "jira.search_issues"

    def test_new_node_fields_present(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(
                    id="fetch",
                    kind=NodeKind.EFFECT,
                    label="fetch",
                    description="Fetches data",
                    retry_policy={"max_attempts": 3},
                    timeout="30s",
                    tools=["jira.search_issues"],
                    children=["child_a"],
                ),
            ],
        )
        data = to_react_flow(graph)["nodes"][0]["data"]
        assert data["retryPolicy"] == {"max_attempts": 3}
        assert data["timeout"] == "30s"
        assert data["tools"] == ["jira.search_issues"]
        assert data["children"] == ["child_a"]
        assert data["hasDescription"] is True

    def test_edge_condition_and_label_in_export(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="switch", kind=NodeKind.SWITCH, label="if"),
                WGIRNode(id="big", kind=NodeKind.EFFECT, label="big"),
            ],
            edges=[
                WGIREdge(
                    source="switch",
                    target="big",
                    kind=EdgeKind.CONTROL,
                    label="True",
                    condition="input > 10",
                ),
            ],
        )
        edge = to_react_flow(graph)["edges"][0]
        assert edge["label"] == "True"
        assert edge["data"]["condition"] == "input > 10"

    def test_layout_metadata_present(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        payload = to_react_flow(graph)
        assert payload["layout"] == {"direction": "LR", "computed": True}

    def test_layout_metadata_reports_supplied_positions(self) -> None:
        graph = WGIRGraph(
            flow_id="test",
            nodes=[WGIRNode(id="a", kind=NodeKind.EFFECT, label="a")],
        )
        payload = to_react_flow(graph, positions={"a": (5.0, 5.0)})
        assert payload["layout"]["computed"] is False

    def test_positions_use_layout_engine_not_naive_fallback(self) -> None:
        """A branch should land on two distinct rows -- the old
        `_fallback_position` stacked every sibling `row * 110px` in arrival
        order regardless of shape, so this only holds with real layering."""
        graph = WGIRGraph(
            flow_id="test",
            nodes=[
                WGIRNode(id="switch", kind=NodeKind.SWITCH, label="if"),
                WGIRNode(id="big", kind=NodeKind.EFFECT, label="big"),
                WGIRNode(id="small", kind=NodeKind.EFFECT, label="small"),
            ],
            edges=[
                WGIREdge(source="switch", target="big", kind=EdgeKind.CONTROL),
                WGIREdge(source="switch", target="small", kind=EdgeKind.CONTROL),
            ],
        )
        nodes = {n["id"]: n["position"] for n in to_react_flow(graph)["nodes"]}
        assert nodes["big"]["x"] == nodes["small"]["x"]
        assert nodes["big"]["y"] != nodes["small"]["y"]
