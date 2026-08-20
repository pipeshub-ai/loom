"""Time-travel — scrub a run timeline to any journal sequence number.

Given a WGIR graph and a journal, ``snapshot_at(seq)`` returns the graph state
at journal entry ``seq``: which nodes had completed, which had failed, and
which had not been reached yet.

Built on :func:`~loom.graph.trace.overlay_journal` rather than on its own
matching pass. It had its own, and inherited the defect that made the overlay
never match anything — two copies of one broken rule instead of one. Slicing
the journal and overlaying the prefix is the same operation the live trace
performs, so the scrubber and the run view cannot disagree about what a node
was doing at a given moment.
"""

from __future__ import annotations

from loom.graph.trace import overlay_journal
from loom.graph.wgir import WGIRGraph
from loom.runtime.journal import JournalEntry

__all__ = ["TimeTraveler"]


class TimeTraveler:
    """Scrub the run timeline to any journal position."""

    def __init__(self, graph: WGIRGraph, journal: list[JournalEntry]) -> None:
        self._graph = graph
        self._journal = journal
        self._max_seq = len(journal) - 1 if journal else 0

    @property
    def max_seq(self) -> int:
        """Maximum valid sequence number."""
        return self._max_seq

    def snapshot_at(self, seq: int) -> WGIRGraph:
        """Return graph state at journal sequence *seq*.

        Nodes reached by sequence *seq* carry their latest status in
        ``metadata["status"]`` and the journal position that set it in
        ``metadata["seq"]``. Nodes not yet reached are ``pending``; nodes that
        express control flow are ``structural``, because a loop has no
        execution of its own to be pending about.
        """
        snapshot = self._graph.model_copy(deep=True)
        prefix = self._journal[: max(seq, -1) + 1]
        overlay = overlay_journal(snapshot, prefix)

        for node in snapshot.nodes:
            trace = overlay.node_traces.get(node.id)
            node.metadata["status"] = trace.status if trace else "pending"
            if trace is not None and trace.last_entry_index is not None:
                node.metadata["seq"] = trace.last_entry_index
        return snapshot
