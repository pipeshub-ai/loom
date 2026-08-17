"""React Flow export — WGIR as a node-based canvas.

`React Flow <https://reactflow.dev>`_ expects ``{ id, type, data, position }``
nodes and ``{ id, source, target }`` edges, which WGIR maps onto almost exactly.
This module does the translation and nothing else: no styling opinions beyond
a stable node type per kind, so the canvas stays free to render however it
likes.

Positions are computed by :func:`~loom.graph.layout.compute_layout` — a
layered (Sugiyama-style) auto-layout — unless the caller supplies its own via
``positions`` (typically accumulated from ``SET_LAYOUT`` patches), in which
case a node without an entry there still falls back to the computed layout
rather than the origin, so a partially-customized canvas never stacks nodes
on top of each other.
"""

from __future__ import annotations

from typing import Any

from loom.graph.layout import compute_layout
from loom.graph.wgir import EdgeKind, NodeKind, WGIRGraph

#: Edges that represent control flow rather than data flow, so a canvas can
#: dash them without knowing what each kind means.
_CONTROL_EDGES = frozenset({EdgeKind.CONTROL, EdgeKind.ERROR})


def to_react_flow(
    graph: WGIRGraph,
    *,
    positions: dict[str, tuple[float, float]] | None = None,
    trace: Any | None = None,
    direction: str = "LR",
) -> dict[str, Any]:
    """Convert a WGIR graph into a React Flow ``{nodes, edges}`` payload.

    Parameters
    ----------
    positions:
        Node coordinates, typically accumulated from ``SET_LAYOUT`` patches.
        Nodes without an entry get the computed layered-layout position.
    trace:
        Optional :class:`~loom.graph.trace.RunTrace`. When given,
        each node carries the status it reached in that run, so the same canvas
        renders both the static shape and a live execution.
    direction:
        Layout direction passed to :func:`~loom.graph.layout.compute_layout`
        when a node has no explicit position. ``"LR"`` (default) or ``"TB"``.
    """
    coords = positions or {}
    computed = compute_layout(graph, direction=direction)
    statuses = _statuses(trace)

    nodes = []
    for node in graph.nodes:
        x, y = coords.get(node.id, computed.get(node.id, (0.0, 0.0)))
        nodes.append(
            {
                "id": node.id,
                "type": _node_type(node.kind),
                "position": {"x": x, "y": y},
                "data": {
                    "label": node.label,
                    "displayName": _display_name(node.label),
                    "kind": node.kind.value,
                    "description": node.description,
                    "hasDescription": bool(node.description),
                    "inputType": node.input_type,
                    "outputType": node.output_type,
                    "stepClass": node.step_class,
                    "source": node.source.model_dump() if node.source else None,
                    "status": statuses.get(node.id),
                    "retryPolicy": node.retry_policy,
                    "timeout": node.timeout,
                    "tools": list(node.tools),
                    "children": list(node.children),
                },
            }
        )

    edges = [
        {
            "id": f"{edge.source}->{edge.target}:{edge.kind.value}",
            "source": edge.source,
            "target": edge.target,
            "label": edge.label or edge.condition or None,
            "animated": edge.kind in _CONTROL_EDGES,
            "data": {
                "kind": edge.kind.value,
                "condition": edge.condition,
                "variable": edge.variable,
            },
        }
        for edge in graph.edges
    ]

    return {
        "flowId": graph.flow_id,
        "extractionHash": graph.extraction_hash,
        "sourceFile": graph.source_file,
        "layout": {"direction": direction, "computed": positions is None},
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


def _display_name(label: str) -> str:
    """``fetch_open_tickets`` -> ``"Fetch Open Tickets"``.

    A dotted bridged-tool label (``jira.search_issues``) is left alone —
    title-casing would mangle the operation id a canvas needs to show
    verbatim, and the dot itself is already the signal a renderer uses to
    tell a tool call apart from a plain step (see
    ``bridges/tool_steps.py`` on the PipesHub side).
    """
    if "." in label or not label:
        return label
    return label.replace("_", " ").replace("-", " ").title()


def _statuses(trace: Any | None) -> dict[str, str]:
    """Node id → status from a RunTrace, or empty when no trace was given."""
    if trace is None:
        return {}
    return {
        node_id: node_trace.status
        for node_id, node_trace in getattr(trace, "node_traces", {}).items()
    }
