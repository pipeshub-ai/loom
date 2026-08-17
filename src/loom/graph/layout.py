"""Sugiyama-style layered layout for WGIR graphs.

``to_react_flow`` used to place nodes with a single longest-path depth pass
(one column per depth, siblings stacked in arrival order) — cheap, but every
branch and every sibling landed in reading order rather than a shape a human
would draw, and a retry loop's back-edge inflated the depth of everything
downstream of it because the naive pass had no notion of "this edge closes a
cycle, don't count it".

This module replaces that with the classic four-phase layered approach:

1. **Cycle breaking** — DFS over the graph flags edges that close a cycle
   (a loop's back-edge, chiefly) so every later phase can treat the graph as
   a DAG without lying about what the graph actually is.
2. **Layer assignment** — longest-path from each source over the remaining
   forward edges, via Kahn's algorithm. Every node's layer is one more than
   the deepest predecessor that can reach it.
3. **Crossing minimization** — a two-sweep barycenter heuristic: order each
   layer by the mean position of its predecessors (top-down sweep), then by
   the mean position of its successors (bottom-up sweep). Two sweeps is the
   standard cheap approximation; workflow graphs are small enough that more
   sweeps buy little.
4. **Coordinate assignment** — nodes are spaced evenly within their layer and
   centered against the tallest layer, so a short branch does not hug the
   top of the canvas while a long one runs off the bottom.

No third-party dependency (no ``networkx``, no ``dagre``) — the whole thing
is pure-Python graph traversal, intentionally small enough to read in one
sitting and cheap enough to run on every code generation.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.graph.wgir import WGIREdge, WGIRGraph

__all__ = ["compute_layout"]

_WHITE, _GRAY, _BLACK = 0, 1, 2

#: Barycenter sweeps to run (one down + one up counts as two). Diminishing
#: returns past this for graphs the size a single workflow file produces.
_ORDERING_PASSES = 2


def compute_layout(
    graph: WGIRGraph,
    *,
    direction: str = "LR",
    node_width: float = 240,
    node_height: float = 80,
    rank_sep: float = 80,
    node_sep: float = 40,
) -> dict[str, tuple[float, float]]:
    """Compute a layered ``{node_id: (x, y)}`` layout for a WGIR graph.

    Parameters
    ----------
    direction:
        ``"LR"`` (left-to-right, the default — matches the pipeline reading
        order n8n and Langflow use) or ``"TB"`` (top-to-bottom). Any other
        value is treated as ``"LR"``.
    node_width, node_height:
        The canvas footprint of one node, used to space columns/rows apart.
    rank_sep:
        Gap between layers (columns in LR, rows in TB).
    node_sep:
        Gap between siblings within the same layer.

    Returns an empty dict for an empty graph. A single node lands at
    ``(0, 0)``. Cycles (a retry loop's back-edge) are detected and excluded
    from layering rather than causing runaway depth or an infinite loop.
    """
    node_ids = [n.id for n in graph.nodes]
    if not node_ids:
        return {}

    back_edges = _detect_back_edges(node_ids, graph.edges)
    layer_of = _assign_layers(node_ids, graph.edges, back_edges)
    layers = _order_layers(node_ids, graph.edges, layer_of, back_edges)
    positions = _assign_coordinates(layers, node_width, node_height, rank_sep, node_sep)

    if direction == "TB":
        return {nid: (y, x) for nid, (x, y) in positions.items()}
    return positions


def _detect_back_edges(
    node_ids: list[str], edges: list[WGIREdge]
) -> set[tuple[str, str]]:
    """Edges that close a cycle, found via iterative DFS with the standard
    white/gray/black coloring: an edge to a node still on the stack (gray)
    closes a cycle back to an ancestor, including a node's own self-loop.

    Iterative rather than recursive so a long linear workflow cannot hit
    Python's recursion limit.
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        if e.source in adjacency and e.target in adjacency:
            adjacency[e.source].append(e.target)

    color = dict.fromkeys(node_ids, _WHITE)
    back_edges: set[tuple[str, str]] = set()

    for start in node_ids:
        if color[start] != _WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = _GRAY
        while stack:
            node, idx = stack[-1]
            neighbors = adjacency[node]
            if idx >= len(neighbors):
                color[node] = _BLACK
                stack.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = neighbors[idx]
            if color[nxt] == _WHITE:
                color[nxt] = _GRAY
                stack.append((nxt, 0))
            elif color[nxt] == _GRAY:
                back_edges.add((node, nxt))
            # black neighbor: a forward/cross edge, not a back-edge.
    return back_edges


def _assign_layers(
    node_ids: list[str],
    edges: list[WGIREdge],
    back_edges: set[tuple[str, str]],
) -> dict[str, int]:
    """Longest-path layer per node over the forward (non-back-edge) DAG,
    via Kahn's algorithm — a node's layer is always one past its deepest
    predecessor, so no edge ever points backward across layers.
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    indegree = dict.fromkeys(node_ids, 0)
    for e in edges:
        if (e.source, e.target) in back_edges:
            continue
        if e.source not in adjacency or e.target not in indegree:
            continue
        adjacency[e.source].append(e.target)
        indegree[e.target] += 1

    layer = dict.fromkeys(node_ids, 0)
    queue: deque[str] = deque(nid for nid in node_ids if indegree[nid] == 0)
    while queue:
        node = queue.popleft()
        for nxt in adjacency[node]:
            layer[nxt] = max(layer[nxt], layer[node] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return layer


def _order_layers(
    node_ids: list[str],
    edges: list[WGIREdge],
    layer_of: dict[str, int],
    back_edges: set[tuple[str, str]],
) -> dict[int, list[str]]:
    """Order nodes within each layer to reduce edge crossings.

    Barycenter heuristic: sort a layer by the mean sibling-position of each
    node's neighbors in the adjacent layer, alternating a top-down sweep
    (against predecessors) with a bottom-up sweep (against successors) so
    both directions of influence get a chance to settle.
    """
    layers: dict[int, list[str]] = {}
    for nid in node_ids:
        layers.setdefault(layer_of[nid], []).append(nid)
    if not layers:
        return layers
    max_layer = max(layers)

    preds: dict[str, list[str]] = {nid: [] for nid in node_ids}
    succs: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        if (e.source, e.target) in back_edges:
            continue
        if e.source not in succs or e.target not in preds:
            continue
        succs[e.source].append(e.target)
        preds[e.target].append(e.source)

    position: dict[str, int] = {}

    def refresh_positions() -> None:
        for nodes in layers.values():
            for idx, nid in enumerate(nodes):
                position[nid] = idx

    def barycenter(nid: str, neighbors: list[str]) -> float:
        if not neighbors:
            # No pull from this direction — keep the node's current spot
            # rather than collapsing every unconnected node to the top.
            return position[nid]
        return sum(position[n] for n in neighbors) / len(neighbors)

    refresh_positions()
    for _ in range(_ORDERING_PASSES):
        for idx in range(1, max_layer + 1):
            layers[idx].sort(key=lambda nid: barycenter(nid, preds[nid]))
            refresh_positions()
        for idx in range(max_layer - 1, -1, -1):
            layers[idx].sort(key=lambda nid: barycenter(nid, succs[nid]))
            refresh_positions()

    return layers


def _assign_coordinates(
    layers: dict[int, list[str]],
    node_width: float,
    node_height: float,
    rank_sep: float,
    node_sep: float,
) -> dict[str, tuple[float, float]]:
    """Place each layer as a column, nodes stacked with even spacing and
    centered against the tallest layer so a two-node branch does not hug
    the canvas edge next to a five-node one.
    """
    positions: dict[str, tuple[float, float]] = {}
    if not layers:
        return positions

    layer_height = {
        idx: len(nodes) * node_height + max(0, len(nodes) - 1) * node_sep
        for idx, nodes in layers.items()
    }
    max_height = max(layer_height.values())

    for idx in sorted(layers):
        nodes = layers[idx]
        x = idx * (node_width + rank_sep)
        y_offset = (max_height - layer_height[idx]) / 2
        for row, nid in enumerate(nodes):
            y = y_offset + row * (node_height + node_sep)
            positions[nid] = (x, y)

    return positions
