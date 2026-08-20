"""Run trace overlay — map journal entries onto the WGIR graph.

Answers "why did my run do that" by overlaying execution data (status,
duration, attempt count) onto the static graph structure.

**The matching problem, and why the obvious key does not work.** A journal
entry is identified by its ``path`` — ``"0"``, ``"1"``, ``"3.1"`` — allocated
by a counter as the body runs. A graph node is identified by a slug derived
from the call — ``fetch``, ``fetch_1``, ``jira.create_issue``. The two
namespaces have never overlapped, so matching a path against a node id found
nothing, ever: a completed run rendered with every node ``pending``, and the
canvas, ``loom watch`` and the time-travel scrubber all inherited it. The test
that covered this hand-built a journal entry with ``path="fetch"`` — a shape
the engine has never produced — and passed.

**What is matched instead** is the entry's *name*, which is the same string the
extractor put in the node's label, with lexical order as the tie-breaker when
one name appears more than once. Every entry lands on a node, and the totals
are conserved: the entries for a label are distributed across the nodes
carrying it, and the last of them absorbs the remainder.

**What this deliberately does not claim.** When one label appears at several
call sites *and* one of them is inside a loop, which entry belongs to which
site is not recoverable — a journal entry records what ran, not which line
issued it, and a loop turns one line into many entries. So per-site attribution
in that case is approximate, and :attr:`NodeTrace.entries` is the honest
number: how many entries landed here, not how many iterations this site ran.
Making it exact needs the engine to journal a source identity alongside the
path, which is a change to the record format rather than to this file.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from loom.graph.wgir import NodeKind, WGIRGraph
from loom.runtime.journal import EntryKind, JournalEntry

__all__ = ["NodeTrace", "RunTrace", "overlay_journal"]

#: Node kinds that express control flow rather than a durable operation. They
#: are never journaled — there is nothing to record about "there is a loop
#: here" — so reporting them ``pending`` after a successful run says the run
#: did not reach them, which is false. They get their own status instead.
STRUCTURAL_KINDS: frozenset[NodeKind] = frozenset({
    NodeKind.TRIGGER,
    NodeKind.LOOP,
    NodeKind.SWITCH,
    NodeKind.PARALLEL,
    NodeKind.RACE,
    NodeKind.RETURN,
})

#: Entry kinds that never draw. ``side_effect`` is ``ctx.now()`` / ``uuid4()``
#: / ``random()`` — journaled so they replay identically, deliberately absent
#: from the graph because a node per clock read would bury the flow. An entry
#: of this kind that matches nothing is not a defect, so it is not counted as
#: one.
UNDRAWN_ENTRY_KINDS: frozenset[EntryKind] = frozenset({
    EntryKind.SIDE_EFFECT,
    EntryKind.MODEL_CALL,
    EntryKind.TOOL_CALL,
})

_NODE_PREFIX = "node:"
"""``ctx.node("human.approval")`` journals as ``node:human.approval`` and draws
as ``human.approval``. One prefix, stripped in one place."""


class NodeTrace(BaseModel):
    """Execution trace data for a single node."""

    node_id: str
    status: str = "pending"
    """``completed``, ``failed``, ``exhausted``, ``suspended``, ``pending``, or
    ``structural`` — the last meaning the node expresses control flow and has
    no execution of its own to report."""
    attempts: int = 0
    duration_ms: float | None = None
    output_preview: str = ""
    error: str = ""
    entries: int = 0
    """How many journal entries matched this node. Greater than one for a node
    inside a loop, which is the case an ordinal-based match cannot express."""
    last_entry_index: int | None = None
    """Position in the journal of the most recent entry that matched this node.

    Recorded during the single overlay pass so the time-travel scrubber can be
    built on the same matcher rather than re-deriving it — the previous
    scrubber had its own copy of the matching rule, and inherited its defect.
    """


class RunTrace(BaseModel):
    """Complete execution trace overlaid on a WGIR graph."""

    run_id: str
    flow_id: str
    node_traces: dict[str, NodeTrace] = Field(default_factory=dict)
    """``node_id → NodeTrace``"""
    total_duration_ms: float | None = None
    final_status: str = ""

    unmatched_entries: list[str] = Field(default_factory=list)
    """Names of journal entries that matched no node in the graph.

    Surfaced rather than dropped, because a silently empty overlay is
    indistinguishable from a run that did nothing — which is exactly how the
    previous implementation stayed broken. A non-empty list here means the
    graph and the journal disagree about what this workflow contains, and that
    is a defect in the extractor or a stale committed graph.
    """

    @property
    def matched(self) -> int:
        """Nodes that at least one journal entry landed on."""
        return sum(1 for t in self.node_traces.values() if t.entries)


class _NodeIndex:
    """Resolves a journal entry to the graph node that produced it.

    Holds the tie-breaking state, which is the whole of the problem: a label
    appearing three times in a body is three nodes, and the entries have to be
    handed out in lexical order. Its own class so that ordering rule is stated
    and tested once, rather than living inside a loop in ``overlay_journal``.
    """

    def __init__(self, graph: WGIRGraph) -> None:
        self._by_label: dict[str, list[str]] = defaultdict(list)
        self._cursor: dict[str, int] = defaultdict(int)
        for node in graph.nodes:
            if node.kind in STRUCTURAL_KINDS:
                continue
            self._by_label[node.label].append(node.id)

    def resolve(self, entry: JournalEntry) -> str | None:
        """The node id for *entry*, or ``None`` when the graph has no such node.

        Repeated matches on an exhausted label return the *last* node with that
        label rather than nothing: a step inside a loop is one node that ran
        many times, and reporting only its first iteration would understate the
        run.
        """
        label = self.label_for(entry)
        candidates = self._by_label.get(label)
        if not candidates:
            return None
        index = self._cursor[label]
        if index < len(candidates):
            self._cursor[label] = index + 1
            return candidates[index]
        return candidates[-1]

    @staticmethod
    def label_for(entry: JournalEntry) -> str:
        """The graph label an entry's name corresponds to."""
        name = entry.name
        if name.startswith(_NODE_PREFIX):
            return name[len(_NODE_PREFIX) :]
        return name


def overlay_journal(
    graph: WGIRGraph,
    journal: list[JournalEntry],
    *,
    run_id: str = "",
) -> RunTrace:
    """Overlay journal entries onto a WGIR graph.

    Entries are consumed in journal order — which is execution order — and each
    is resolved to a node by name. A node that no entry reached keeps
    ``pending``; a node that expresses control flow is reported ``structural``,
    because "this loop did not run" is not a thing a loop can do.
    """
    index = _NodeIndex(graph)
    traces: dict[str, NodeTrace] = {}
    unmatched: list[str] = []

    for position, entry in enumerate(journal):
        node_id = index.resolve(entry)
        if node_id is None:
            if entry.kind not in UNDRAWN_ENTRY_KINDS:
                unmatched.append(entry.name)
            continue

        trace = traces.setdefault(node_id, NodeTrace(node_id=node_id))
        trace.status = entry.status.value
        trace.attempts = max(trace.attempts, entry.attempts)
        trace.entries += 1
        trace.last_entry_index = position
        trace.duration_ms = _duration_ms(entry) or trace.duration_ms

        if entry.output is not None:
            preview = str(entry.output)
            trace.output_preview = preview[:200]
        if entry.error:
            trace.error = str(entry.error)

    for node in graph.nodes:
        if node.id in traces:
            continue
        traces[node.id] = NodeTrace(
            node_id=node.id,
            status="structural" if node.kind in STRUCTURAL_KINDS else "pending",
        )

    return RunTrace(
        run_id=run_id,
        flow_id=graph.flow_id,
        node_traces=traces,
        unmatched_entries=unmatched,
    )


def _duration_ms(entry: JournalEntry) -> float | None:
    """Wall time for one entry, when both ends were recorded."""
    if entry.started_at is None or entry.finished_at is None:
        return None
    return (entry.finished_at - entry.started_at).total_seconds() * 1000.0
