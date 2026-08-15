"""GraphPatch — constrained canvas editing operations.

A ``GraphPatch`` describes a single atomic edit to a workflow's graph.
The 6 operations are designed to be safe — they cannot produce invalid code
by construction.  Complex edits outside these ops fall back to
"edit in IDE" or the coding agent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from loom.graph.wgir import WGIRGraph


class PatchOp(StrEnum):
    """The six constrained editing operations."""

    SET_LAYOUT = "set_layout"
    """Move a node on the canvas (display only, no code change)."""
    SET_PARAM = "set_param"
    """Change a literal parameter value in a step call."""
    INSERT_NODE = "insert_node"
    """Add a new step + await call at a position."""
    REMOVE_NODE = "remove_node"
    """Delete a step call.  Blocked if output is referenced downstream."""
    REWIRE = "rewire"
    """Change which step's output feeds into another step's input."""
    SET_POLICY = "set_policy"
    """Change retry/timeout/concurrency policy on a step."""


class GraphPatch(BaseModel):
    """A single atomic edit operation on the graph."""

    op: PatchOp
    target: str
    """Node ID or ``'layout'``."""
    params: dict[str, Any] = Field(default_factory=dict)


class PatchValidationError(Exception):
    """A patch cannot be applied safely."""


class PatchResult(BaseModel):
    """Result of applying a patch."""

    applied: bool
    op: PatchOp
    target: str
    detail: str = ""


class PatchApplier:
    """Validate and describe patch operations against a WGIR graph.

    The actual code modification (rewriting source files) requires an
    AST transform library and is deferred to the coding agent or IDE.
    This class validates that the patch is structurally sound.
    """

    def validate(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        """Validate a patch against the current graph.

        Returns a ``PatchResult`` describing whether the patch is valid.
        Raises ``PatchValidationError`` for unsafe operations.
        """
        match patch.op:
            case PatchOp.SET_LAYOUT:
                return self._validate_layout(patch, graph)
            case PatchOp.SET_PARAM:
                return self._validate_set_param(patch, graph)
            case PatchOp.INSERT_NODE:
                return self._validate_insert(patch, graph)
            case PatchOp.REMOVE_NODE:
                return self._validate_remove(patch, graph)
            case PatchOp.REWIRE:
                return self._validate_rewire(patch, graph)
            case PatchOp.SET_POLICY:
                return self._validate_set_policy(patch, graph)

    def _validate_layout(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        """Layout changes are always valid (display-only)."""
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail="Layout is display-only",
        )

    def _validate_set_param(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        node = graph.find_node(patch.target)
        if node is None:
            msg = f"Node '{patch.target}' not found"
            raise PatchValidationError(msg)
        if node.source is None:
            msg = f"Node '{patch.target}' has no source range"
            raise PatchValidationError(msg)
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail=f"Set param on {node.label}",
        )

    def _validate_insert(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        after = patch.params.get("after")
        if after and graph.find_node(after) is None:
            msg = f"Insert position node '{after}' not found"
            raise PatchValidationError(msg)
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail=f"Insert node after {after or 'start'}",
        )

    def _validate_remove(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        node = graph.find_node(patch.target)
        if node is None:
            msg = f"Node '{patch.target}' not found"
            raise PatchValidationError(msg)
        # Check if any downstream nodes reference this node's output
        data_deps = [
            e for e in graph.edges
            if e.source == patch.target and e.kind.value == "data"
        ]
        if data_deps:
            targets = [e.target for e in data_deps]
            msg = (
                f"Cannot remove '{patch.target}' — "
                f"output referenced by: {targets}"
            )
            raise PatchValidationError(msg)
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail=f"Remove {node.label} (no data dependents)",
        )

    def _validate_rewire(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        from_node = patch.params.get("from")
        to_node = patch.params.get("to")
        if from_node and graph.find_node(from_node) is None:
            msg = f"Source node '{from_node}' not found"
            raise PatchValidationError(msg)
        if to_node and graph.find_node(to_node) is None:
            msg = f"Target node '{to_node}' not found"
            raise PatchValidationError(msg)
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail=f"Rewire {from_node} → {to_node}",
        )

    def _validate_set_policy(
        self, patch: GraphPatch, graph: WGIRGraph
    ) -> PatchResult:
        node = graph.find_node(patch.target)
        if node is None:
            msg = f"Node '{patch.target}' not found"
            raise PatchValidationError(msg)
        return PatchResult(
            applied=True,
            op=patch.op,
            target=patch.target,
            detail=f"Set policy on {node.label}",
        )
