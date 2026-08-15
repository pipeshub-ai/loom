"""Run trace overlay — map journal entries onto the WGIR graph.

Answers "why did my run do that" by overlaying actual execution data
(status, duration, attempt count) onto the static graph structure.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom.graph.wgir import WGIRGraph
from loom.runtime.journal import JournalEntry


class NodeTrace(BaseModel):
    """Execution trace data for a single node."""

    node_id: str
    status: str = "pending"
    """``completed``, ``failed``, ``skipped``, ``pending``"""
    attempts: int = 0
    duration_ms: float | None = None
    output_preview: str = ""
    error: str = ""


class RunTrace(BaseModel):
    """Complete execution trace overlaid on a WGIR graph."""

    run_id: str
    flow_id: str
    node_traces: dict[str, NodeTrace] = Field(default_factory=dict)
    """``node_id → NodeTrace``"""
    total_duration_ms: float | None = None
    final_status: str = ""


def overlay_journal(
    graph: WGIRGraph,
    journal: list[JournalEntry],
    *,
    run_id: str = "",
) -> RunTrace:
    """Overlay journal entries onto a WGIR graph.

    Each journal entry is matched to a graph node by its ``path``
    (which corresponds to the step name / node ID).  The overlay
    records the latest status, attempt count, and timing.
    """
    node_ids = graph.node_ids()
    traces: dict[str, NodeTrace] = {}

    for entry in journal:
        # Match journal path to node id
        node_id = _match_to_node(entry.path, node_ids)
        if node_id is None:
            continue

        if node_id not in traces:
            traces[node_id] = NodeTrace(node_id=node_id)

        trace = traces[node_id]
        trace.status = entry.status.value
        trace.attempts = max(trace.attempts, entry.attempts)

        if entry.output:
            preview = str(entry.output)
            trace.output_preview = (
                preview[:200] if len(preview) > 200 else preview
            )
        if entry.error:
            trace.error = str(entry.error)

    # Mark unvisited nodes as pending
    for node in graph.nodes:
        if node.id not in traces:
            traces[node.id] = NodeTrace(node_id=node.id, status="pending")

    return RunTrace(
        run_id=run_id,
        flow_id=graph.flow_id,
        node_traces=traces,
    )


def _match_to_node(path: str, node_ids: set[str]) -> str | None:
    """Match a journal path to a node ID.

    Journal paths may include attempt/sequence suffixes (e.g.
    ``step_name/0``).  We try the full path first, then strip
    suffixes.
    """
    if path in node_ids:
        return path
    # Try stripping trailing path segments
    parts = path.split("/")
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i])
        if candidate in node_ids:
            return candidate
    # Try just the first segment
    if parts[0] in node_ids:
        return parts[0]
    return None
