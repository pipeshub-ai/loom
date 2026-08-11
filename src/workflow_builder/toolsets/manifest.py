"""Toolset manifest — the declarative description of an integration.

A ``ToolsetManifest`` captures every operation an external service exposes,
grouped by resource (e.g. ``leads``, ``contacts``).  The manifest is the
source of truth for the three-tier disclosure catalog, code generation,
certification, and grant derivation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EffectClass(StrEnum):
    """Classifies the side-effect of an operation."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class OperationSpec(BaseModel):
    """A single operation within a toolset group."""

    id: str
    """Dot-separated identifier, e.g. ``leads.upsert``."""
    summary: str
    """One-line description (~40 tokens)."""
    description: str = ""
    """Full documentation."""
    effect: EffectClass = EffectClass.READ
    input_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the input payload (if no Pydantic model)."""
    output_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the output payload."""
    scopes: list[str] = Field(default_factory=list)
    """Required OAuth / API scopes."""
    pagination: bool = False
    """Whether this op returns ``Page[T]``."""
    rate_limit_group: str = ""
    """Shared rate-limit key across ops that share a quota."""
    idempotent: bool = False
    """Safe to retry without side-effects?"""


class ToolsetManifest(BaseModel):
    """Declarative description of an external integration."""

    id: str
    """Unique identifier, e.g. ``salesforce``."""
    version: str
    """Semantic version of this manifest."""
    summary: str
    """Tier 1 index card — short description for search results."""
    description: str = ""
    """Full description."""
    groups: dict[str, list[OperationSpec]] = Field(default_factory=dict)
    """Resource groups: ``group_name → [OperationSpec, ...]``."""
    auth: dict[str, Any] = Field(default_factory=dict)
    """Authentication configuration (type, fields, etc.)."""
    base_url: str = ""
    """Base URL for the API."""
    rate_limits: dict[str, Any] = Field(default_factory=dict)
    """Rate limit configuration per group or global."""
    egress_hosts: list[str] = Field(default_factory=list)
    """Declared egress hosts for sandbox enforcement."""
    fakes_module: str = ""
    """Python module path containing fake implementations for testing."""

    def all_operations(self) -> list[OperationSpec]:
        """Return all operations across all groups."""
        return [op for ops in self.groups.values() for op in ops]

    def find_operation(self, op_id: str) -> OperationSpec | None:
        """Find an operation by its dotted id."""
        for ops in self.groups.values():
            for op in ops:
                if op.id == op_id:
                    return op
        return None
