"""Time-travel — scrub a run timeline to any journal sequence number.

Given a WGIR graph and a journal, ``snapshot_at(seq)`` returns the graph
state at journal entry ``seq``: which nodes were completed, pending,
or failed at that moment.
"""

from __future__ import annotations

from loom.graph.trace import _match_to_node
from loom.graph.wgir import WGIRGraph
from loom.runtime.journal import JournalEntry


class TimeTraveler:
    """Scrub the run timeline to any journal position."""

    def __init__(
        self, graph: WGIRGraph, journal: list[JournalEntry]
    ) -> None:
        self._graph = graph
        self._journal = journal
        self._max_seq = len(journal) - 1 if journal else 0

    @property
    def max_seq(self) -> int:
        """Maximum valid sequence number."""
        return self._max_seq

    def snapshot_at(self, seq: int) -> WGIRGraph:
        """Return graph state at journal sequence *seq*.

        Nodes that have been visited by sequence *seq* carry their
        latest status in ``metadata["status"]``.  Unvisited nodes
        have ``metadata["status"] = "pending"``.
        """
        snapshot = self._graph.model_copy(deep=True)
        node_ids = snapshot.node_ids()

        # Collect entries up to seq
        active_entries = [
            e for i, e in enumerate(self._journal) if i <= seq
        ]

        # Build latest status per node
        node_status: dict[str, str] = {}
        node_seq: dict[str, int] = {}
        for i, entry in enumerate(active_entries):
            nid = _match_to_node(entry.path, node_ids)
            if nid is not None:
                node_status[nid] = entry.status.value
                node_seq[nid] = i

        # Apply to snapshot
        for node in snapshot.nodes:
            if node.id in node_status:
                node.metadata["status"] = node_status[node.id]
                node.metadata["seq"] = node_seq[node.id]
            else:
                node.metadata["status"] = "pending"

        return snapshot
