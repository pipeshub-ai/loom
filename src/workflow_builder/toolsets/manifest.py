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

from workflow_builder.toolsets.kinds import ToolsetKind


class EffectClass(StrEnum):
    """Classifies the side-effect of an operation."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class OperationSpec(BaseModel):
    """A single operation within a toolset group."""

    id: str
    """Dot-separated identifier, e.g. ``leads.upsert``."""
    function: str = ""
    """Name of the ``@step`` function implementing this operation.

    An operation id names a capability; it is not something anyone can write in
    Python. Without this the generated documentation shows only ``leads.upsert``
    and a model asked to write code invents an import to match it. Set together
    with :attr:`ToolsetManifest.tools_module` to make the operation callable
    from generated code rather than merely describable."""
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
    """Whether this operation returns a page of a larger result set.

    Set it on every read that can return more rows than one request carries.
    The toolset client follows the pages to fill the caller's limit and hands
    back a :class:`~workflow_builder.toolsets.pagination.Results`, which knows
    whether it saw everything — but a caller only asks that question if it
    knows there was a question, so this is what puts "may be capped" in front
    of whoever is writing the call.

    A read that returns one object, or a naturally bounded list, leaves it
    ``False``. Declaring pagination that does not happen is as misleading as
    omitting it."""
    rate_limit_group: str = ""
    """Shared rate-limit key across ops that share a quota."""
    idempotent: bool = False
    """Safe to retry without side-effects?"""
    resolves: str = ""
    """The kind of entity this turns a human's words into a stable identifier for.

    ``"user"`` on an operation that takes a name and returns an account id.
    Filtering on a person, a project, or a board by the name someone typed is
    the single most common way a query returns zero rows and no error — the API
    matches on identifiers, the human said a display name, and nothing joins
    them. Marking the operation lets a caller be told to resolve first, without
    knowing anything about this particular service."""


class ToolsetManifest(BaseModel):
    """Declarative description of an external integration."""

    id: str
    """Unique identifier, e.g. ``salesforce``."""
    version: str
    """Semantic version of this manifest."""
    kind: ToolsetKind = ToolsetKind.APP
    """What sort of toolset this is — an app integration, an MCP server, a
    knowledge base, agent memory, or a skill. Lets an agent tell a first-party
    Jira toolset from an MCP-sourced one that exposes similar operations."""
    provider: str = ""
    """Who supplies these tools — e.g. ``loom``, ``mcp:atlassian``, ``acme-corp``.
    Together with ``kind`` and ``id`` this is what makes a toolset addressable
    when two of them describe the same underlying service."""
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
    tools_module: str = ""
    """Importable module exposing this toolset's operations as ``@step`` functions.

    ``workflow_builder.toolsets.google.gmail.tools``, for example. This is what
    turns a manifest from a description into something generated code can call:
    documentation built from a manifest without it lists operation ids that
    exist in no namespace, and a model writing code against that guesses an
    import — plausibly, confidently, and wrongly."""

    def resolvers(self) -> dict[str, OperationSpec]:
        """Entity kind → the operation that resolves it."""
        return {
            op.resolves: op for op in self.all_operations() if op.resolves
        }

    def paginated(self) -> list[OperationSpec]:
        """Operations whose result may be a page of something larger.

        The counterpart to :meth:`resolvers`, and there for the same reason:
        the manifest knows something the caller needs and cannot infer from a
        signature. ``max_results: int`` looks identical whether it caps a
        complete answer or truncates a partial one.
        """
        return [op for op in self.all_operations() if op.pagination]

    def import_line(self) -> str:
        """The import a workflow needs to call this toolset, or ``""``.

        Empty when the manifest declares no ``tools_module`` or no operation
        names a function, because a half-specified import is worse than none.
        """
        names = sorted({op.function for op in self.all_operations() if op.function})
        if not self.tools_module or not names:
            return ""
        return f"from {self.tools_module} import {', '.join(names)}"

    @property
    def qualified_id(self) -> str:
        """Fully-qualified identity: ``<kind>:<provider>:<id>``.

        Two toolsets may both call themselves ``jira``; only this tells them
        apart. Registries key on it so a second registration augments the
        catalog instead of silently replacing the first.
        """
        return f"{self.kind.value}:{self.provider or 'local'}:{self.id}"

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
