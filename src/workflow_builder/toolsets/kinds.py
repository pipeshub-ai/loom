"""Toolset kind classification and reserved namespaces.

Distinguishes between application integrations, MCP servers, knowledge
bases, agent memory, and reusable skills.
"""

from __future__ import annotations

from enum import StrEnum


class ToolsetKind(StrEnum):
    """The kind of toolset an integration represents."""

    APP = "app"
    MCP = "mcp"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    SKILL = "skill"


RESERVED_PREFIXES: dict[str, ToolsetKind] = {
    "mcp": ToolsetKind.MCP,
    "knowledge": ToolsetKind.KNOWLEDGE,
    "memory": ToolsetKind.MEMORY,
    "skill": ToolsetKind.SKILL,
}


def classify_toolset(toolset_id: str) -> ToolsetKind:
    """Infer the toolset kind from its id prefix.

    IDs that start with a reserved prefix (e.g. ``knowledge.rag``) are
    classified accordingly.  All other IDs default to ``APP``.
    """
    for prefix, kind in RESERVED_PREFIXES.items():
        if toolset_id.startswith(prefix + ".") or toolset_id == prefix:
            return kind
    return ToolsetKind.APP


def validate_namespace(toolset_id: str, kind: ToolsetKind) -> bool:
    """Check that a toolset id is consistent with its declared kind.

    Returns ``True`` when the id's prefix matches the kind.  For ``APP``
    kind, the id must *not* start with any reserved prefix.
    """
    inferred = classify_toolset(toolset_id)
    if kind == ToolsetKind.APP:
        return inferred == ToolsetKind.APP
    return inferred == kind
