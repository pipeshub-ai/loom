"""React Flow export — WGIR as a node-based canvas.

`React Flow <https://reactflow.dev>`_ expects ``{ id, type, data, position }``
nodes and ``{ id, source, target }`` edges, which WGIR maps onto almost exactly.
This module does the translation and nothing else: no layout engine, no
styling opinions beyond a stable node type per kind, so the canvas stays free to
render however it likes.

Positions are the one thing WGIR does not carry. When a node has none, a simple
layered fallback is computed from graph depth — enough to render something
sensible before a human has dragged anything, and overwritten the moment a
``SET_LAYOUT`` patch supplies real coordinates.
"""

from __future__ import annotations

from typing import Any

from workflow_builder.graph.wgir import EdgeKind, NodeKind, WGIRGraph

#: Horizontal and vertical spacing for the fallback layout, in canvas units.
_COLUMN_WIDTH = 260
_ROW_HEIGHT = 110

#: Edges that represent control flow rather than data flow, so a canvas can
#: dash them without knowing what each kind means.
_CONTROL_EDGES = frozenset({EdgeKind.CONTROL, EdgeKind.ERROR})


def to_react_flow(
    graph: WGIRGraph,
    *,
    positions: dict[str, tuple[float, float]] | None = None,
    trace: Any | None = None,
) -> dict[str, Any]:
    """Convert a WGIR graph into a React Flow ``{nodes, edges}`` payload.

    Parameters
    ----------
    positions:
        Node coordinates, typically accumulated from ``SET_LAYOUT`` patches.
        Nodes without an entry get the fallback layered position.
    trace:
        Optional :class:`~workflow_builder.graph.trace.RunTrace`. When given,
        each node carries the status it reached in that run, so the same canvas
        renders both the static shape and a live execution.
    """
    coords = positions or {}
    depths = _depths(graph)
    statuses = _statuses(trace)

    nodes = []
    for node in graph.nodes:
        x, y = coords.get(node.id, _fallback_position(node.id, depths))
        nodes.append(
            {
                "id": node.id,
                "type": _node_type(node.kind),
                "position": {"x": x, "y": y},
                "data": {
                    "label": node.label,
                    "kind": node.kind.value,
                    "description": node.description,
                    "inputType": node.input_type,
                    "outputType": node.output_type,
                    "stepClass": node.step_class,
                    "source": node.source.model_dump() if node.source else None,
                    "status": statuses.get(node.id),
                },
            }
        )

    edges = [
        {
            "id": f"{edge.source}->{edge.target}:{edge.kind.value}",
            "source": edge.source,
            "target": edge.target,
            "label": edge.label or None,
            "animated": edge.kind in _CONTROL_EDGES,
            "data": {"kind": edge.kind.value},
        }
        for edge in graph.edges
    ]

    return {
        "flowId": graph.flow_id,
        "extractionHash": graph.extraction_hash,
        "sourceFile": graph.source_file,
        "nodes": nodes,
        "edges": edges,
    }


def _node_type(kind: NodeKind) -> str:
    """Stable React Flow node type per WGIR kind.

    Triggers and returns get React Flow's built-in ``input``/``output`` types so
    a canvas with no custom components still renders handles on the right sides;
    everything else is ``default``, with the kind in ``data`` for custom
    renderers to switch on.
    """
    if kind is NodeKind.TRIGGER:
        return "input"
    if kind is NodeKind.RETURN:
        return "output"
    return "default"


def _depths(graph: WGIRGraph) -> dict[str, int]:
    """Longest-path depth per node, used only for the fallback layout.

    Iterates to a fixed point rather than doing a topological sort, so a cyclic
    graph (a retry loop, say) still terminates instead of raising.
    """
    depth = {node.id: 0 for node in graph.nodes}
    for _ in range(len(graph.nodes)):
        changed = False
        for edge in graph.edges:
            if edge.source not in depth or edge.target not in depth:
                continue
            candidate = depth[edge.source] + 1
            if candidate > depth[edge.target]:
                depth[edge.target] = candidate
                changed = True
        if not changed:
            break
    return depth


def _fallback_position(node_id: str, depths: dict[str, int]) -> tuple[float, float]:
    """Place a node in a column by depth, stacked by arrival order within it."""
    depth = depths.get(node_id, 0)
    siblings = sorted(other for other, d in depths.items() if d == depth)
    row = siblings.index(node_id) if node_id in siblings else 0
    return float(depth * _COLUMN_WIDTH), float(row * _ROW_HEIGHT)


def _statuses(trace: Any | None) -> dict[str, str]:
    """Node id → status from a RunTrace, or empty when no trace was given."""
    if trace is None:
        return {}
    return {
        node_id: node_trace.status
        for node_id, node_trace in getattr(trace, "node_traces", {}).items()
    }
