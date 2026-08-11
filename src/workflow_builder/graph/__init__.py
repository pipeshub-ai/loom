"""Workflow Graph Intermediate Representation (WGIR).

Deterministic graph extraction from decorated workflow code.
Provides the foundation for visualization, narration, canvas editing,
and run trace overlay.
"""

from __future__ import annotations

from workflow_builder.graph.wgir import (
    EdgeKind,
    NodeKind,
    SourceRange,
    WGIREdge,
    WGIRGraph,
    WGIRNode,
)

__all__ = [
    "EdgeKind",
    "NodeKind",
    "SourceRange",
    "WGIREdge",
    "WGIRGraph",
    "WGIRNode",
]
