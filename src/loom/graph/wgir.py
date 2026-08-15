"""WGIR — Workflow Graph Intermediate Representation.

The WGIR is the canonical graph schema extracted from workflow source code.
It is deterministic: the same code always produces the same graph.

Three extraction passes populate the graph:
1. **Registry pass** — decorator metadata (exact identity, types)
2. **AST pass** — control flow and ``ctx.*`` calls (structural)
3. **Symbolic pass** — runtime reachability (dynamic)

The merged graph is committed as ``graph.json`` alongside the source.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeKind(StrEnum):
    """The 18 kinds of nodes that can appear in a WGIR graph."""

    TRIGGER = "trigger"
    PURE = "pure"
    EFFECT = "effect"
    TOOL = "tool"
    AGENT = "agent"
    AGENT_SESSION = "agent_session"
    MAP = "map"
    SWITCH = "switch"
    LOOP = "loop"
    PARALLEL = "parallel"
    RACE = "race"
    WAIT = "wait"
    HUMAN = "human"
    SUBFLOW = "subflow"
    EMIT = "emit"
    COMPENSATE = "compensate"
    ARTIFACT = "artifact"
    RETURN = "return"


class EdgeKind(StrEnum):
    """Edge types in a WGIR graph."""

    DATA = "data"
    CONTROL = "control"
    ERROR = "error"
    COMPENSATION = "compensation"
    EVENT = "event"


class SourceRange(BaseModel):
    """Points back to the exact location in source code."""

    file: str
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0


class WGIRNode(BaseModel):
    """A single node in the workflow graph."""

    id: str
    """Stable step identity."""
    kind: NodeKind
    label: str
    """Human-readable name."""
    description: str = ""
    """From docstring or narration."""
    input_type: str | None = None
    output_type: str | None = None
    source: SourceRange | None = None
    step_class: str | None = None
    """``'pure'`` | ``'effect'`` | ``'agent'``"""
    retry_policy: dict[str, Any] | None = None
    timeout: str | None = None
    tools: list[str] = Field(default_factory=list)
    """Tool names (for agent nodes)."""
    children: list[str] = Field(default_factory=list)
    """Child node IDs (for container nodes like map, parallel)."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WGIREdge(BaseModel):
    """A directed edge between two nodes."""

    source: str
    """Source node ID."""
    target: str
    """Target node ID."""
    kind: EdgeKind
    label: str = ""
    condition: str | None = None
    """For conditional edges (switch branches)."""
    variable: str | None = None
    """Data dependency variable name."""


class WGIRGraph(BaseModel):
    """The complete workflow graph."""

    flow_id: str
    version: int = 1
    nodes: list[WGIRNode] = Field(default_factory=list)
    edges: list[WGIREdge] = Field(default_factory=list)
    triggers: list[dict[str, Any]] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    grants: list[str] = Field(default_factory=list)
    source_file: str = ""
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    extraction_hash: str = ""
    """Hash of the graph content for change detection."""

    def compute_hash(self) -> str:
        """Compute a deterministic hash of the graph's structural content."""
        payload = {
            "flow_id": self.flow_id,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind.value,
                    "label": n.label,
                    "input_type": n.input_type,
                    "output_type": n.output_type,
                    "step_class": n.step_class,
                    "tools": n.tools,
                    "children": n.children,
                }
                for n in sorted(self.nodes, key=lambda n: n.id)
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "kind": e.kind.value,
                }
                for e in sorted(
                    self.edges, key=lambda e: (e.source, e.target)
                )
            ],
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def finalize(self) -> WGIRGraph:
        """Set the extraction hash and return self."""
        self.extraction_hash = self.compute_hash()
        return self

    def find_node(self, node_id: str) -> WGIRNode | None:
        """Look up a node by ID."""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def node_ids(self) -> set[str]:
        """Return all node IDs."""
        return {n.id for n in self.nodes}

    def successors(self, node_id: str) -> list[str]:
        """Return IDs of nodes that this node has edges to."""
        return [e.target for e in self.edges if e.source == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        """Return IDs of nodes that have edges to this node."""
        return [e.source for e in self.edges if e.target == node_id]
