"""Skeleton-first narration — verified, deterministic flow descriptions.

The explainer receives a WGIR *skeleton* (nodes and edges extracted from
code) and produces a human-readable *narration*.  The model narrates nodes
it was handed — it cannot invent steps or hide destructive actions.

Completeness is verified: every node must appear in the narration.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from loom.graph.wgir import WGIRGraph


class Narration(BaseModel):
    """The output of the explainer — a narrated description of a workflow."""

    summary: str = ""
    """One-paragraph overview."""
    node_descriptions: dict[str, str] = Field(default_factory=dict)
    """``node_id → prose`` for each node."""
    edge_descriptions: dict[str, str] = Field(default_factory=dict)
    """``source:target → prose`` for notable edges."""
    capability_manifest: list[str] = Field(default_factory=list)
    """Systems written to (e.g. "Writes to Salesforce leads")."""
    full_text: str = ""
    """Compiled ``description.md`` text."""


class Explainer(Protocol):
    """Generate human-readable narration from a WGIR skeleton."""

    async def narrate(self, graph: WGIRGraph) -> Narration:
        """Produce a narration for the given graph."""
        ...


class SkeletonExplainer:
    """Default explainer that generates descriptions from metadata only.

    Does not use an LLM — produces deterministic output from docstrings
    and node labels.  Suitable for testing and as a fallback when no
    model provider is available.
    """

    async def narrate(self, graph: WGIRGraph) -> Narration:
        """Generate a narration using node metadata only."""
        node_descs: dict[str, str] = {}
        capabilities: list[str] = []

        for node in graph.nodes:
            desc = node.description or f"{node.kind.value} step: {node.label}"
            node_descs[node.id] = desc
            if node.kind.value in ("effect", "agent", "emit"):
                capabilities.append(
                    f"{node.kind.value}: {node.label}"
                )

        summary = self._build_summary(graph)
        full_text = self._build_full_text(graph, node_descs, summary)

        return Narration(
            summary=summary,
            node_descriptions=node_descs,
            capability_manifest=capabilities,
            full_text=full_text,
        )

    @staticmethod
    def _build_summary(graph: WGIRGraph) -> str:
        """Build a one-line summary from the graph."""
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)
        kinds = {n.kind.value for n in graph.nodes}
        return (
            f"Workflow '{graph.flow_id}' with {node_count} nodes "
            f"and {edge_count} edges. "
            f"Node types: {', '.join(sorted(kinds))}."
        )

    @staticmethod
    def _build_full_text(
        graph: WGIRGraph,
        node_descs: dict[str, str],
        summary: str,
    ) -> str:
        """Build the full description.md content."""
        lines = [
            f"# {graph.flow_id}",
            "",
            summary,
            "",
            "## Steps",
            "",
        ]
        for node in graph.nodes:
            desc = node_descs.get(node.id, "")
            lines.append(f"- **{node.label}** ({node.kind.value}): {desc}")
        return "\n".join(lines)


def verify_completeness(
    narration: Narration, graph: WGIRGraph
) -> list[str]:
    """Verify every node in the graph appears in the narration.

    Returns a list of missing node IDs (empty if complete).
    """
    graph_ids = {n.id for n in graph.nodes}
    narrated_ids = set(narration.node_descriptions.keys())
    return sorted(graph_ids - narrated_ids)
