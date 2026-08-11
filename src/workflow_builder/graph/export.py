"""Graph export — Mermaid flowchart generation from WGIR.

Produces Mermaid-syntax flowcharts suitable for PR review comments,
documentation, and rendering in any Mermaid-compatible viewer.
"""

from __future__ import annotations

from workflow_builder.graph.wgir import EdgeKind, NodeKind, WGIRGraph

# Node shapes by kind
_NODE_SHAPES: dict[NodeKind, tuple[str, str]] = {
    NodeKind.TRIGGER: ("([", "])"),  # stadium
    NodeKind.PURE: ("[/", "/]"),  # parallelogram
    NodeKind.EFFECT: ("[", "]"),  # rectangle
    NodeKind.TOOL: ("[", "]"),
    NodeKind.AGENT: ("[[", "]]"),  # subroutine
    NodeKind.AGENT_SESSION: ("[[", "]]"),
    NodeKind.MAP: ("{{", "}}"),  # hexagon
    NodeKind.SWITCH: ("{", "}"),  # rhombus
    NodeKind.LOOP: ("([", "])"),  # stadium
    NodeKind.PARALLEL: ("{{", "}}"),
    NodeKind.RACE: ("{{", "}}"),
    NodeKind.WAIT: ("((", "))"),  # circle
    NodeKind.HUMAN: ("((", "))"),
    NodeKind.SUBFLOW: ("[[", "]]"),
    NodeKind.EMIT: (">", "]"),  # asymmetric
    NodeKind.COMPENSATE: ("[", "]"),
    NodeKind.ARTIFACT: ("[(", ")]"),  # cylinder
    NodeKind.RETURN: ("([", "])"),
}

# Edge arrows by kind
_EDGE_ARROWS: dict[EdgeKind, str] = {
    EdgeKind.DATA: "-->",
    EdgeKind.CONTROL: "-->",
    EdgeKind.ERROR: "-.->",
    EdgeKind.COMPENSATION: "-.->",
    EdgeKind.EVENT: "==>",
}


def _sanitize_label(text: str) -> str:
    """Escape characters that break Mermaid syntax."""
    return text.replace('"', "'").replace("\n", " ")


def to_mermaid(
    graph: WGIRGraph,
    *,
    direction: str = "TB",
    title: str | None = None,
) -> str:
    """Export a WGIR graph as a Mermaid flowchart.

    Args:
        graph: The WGIR graph to export.
        direction: Flow direction — ``TB``, ``LR``, ``BT``, or ``RL``.
        title: Optional title comment at the top.
    """
    lines: list[str] = []
    if title:
        lines.append("---")
        lines.append(f"title: {title}")
        lines.append("---")
    lines.append(f"flowchart {direction}")

    # Nodes
    for node in graph.nodes:
        left, right = _NODE_SHAPES.get(node.kind, ("[", "]"))
        label = _sanitize_label(node.label)
        lines.append(f"    {node.id}{left}\"{label}\"{right}")

    # Edges
    for edge in graph.edges:
        arrow = _EDGE_ARROWS.get(edge.kind, "-->")
        if edge.label:
            label = _sanitize_label(edge.label)
            lines.append(f"    {edge.source} {arrow}|{label}| {edge.target}")
        else:
            lines.append(f"    {edge.source} {arrow} {edge.target}")

    return "\n".join(lines)
