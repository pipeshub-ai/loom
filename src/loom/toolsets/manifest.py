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

from loom.toolsets.kinds import ToolsetKind


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
    effect: EffectClass = EffectClass.WRITE
    """What this operation does to the world. **Declare it on every operation,
    including the reads** — the default is a backstop, not a classification.

    It defaults to ``WRITE`` because the alternative fails open. ``READ`` is the
    one class exempt from every write and destructive control, so defaulting to
    it means an operation nobody classified is *granted* rather than flagged: a
    forgotten ``delete`` is reachable by an agent resolved with
    ``resolve_tools(effects={EffectClass.READ})``. Defaulting to ``WRITE`` makes
    the failure mode a refused call instead, which is recoverable by reading the
    error. ``EffectCall.effect`` in ``runtime/effects.py`` has always defaulted
    this way; this makes the manifest agree with the broker.

    All 320 operations LOOM ships declare this explicitly, so the default is
    reached only by a toolset that has not been classified yet — which is
    exactly the case that should not be trusted with a read-only grant.
    """
    input_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the input payload (if no Pydantic model)."""
    output_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the output payload."""
    reversible: bool = False
    """Can this operation's effect be undone, restoring the prior state?

    **Not** "is there an opposite operation". Deleting an issue you created
    does not undo the create — the key is consumed, the comments are gone — so
    ``issues.create`` is not reversible by ``issues.delete``. Only a genuine
    restore counts: trash/untrash, share/unshare.

    This is the axis ``EffectClass`` cannot express, and the one that matters
    most when a model is choosing. Ranked by damage, ``gmail_trash_message`` is
    DESTRUCTIVE and ``gmail_send_message`` is WRITE — but trashing is
    recoverable for thirty days and nothing unsends an email. A policy that
    blocks DESTRUCTIVE and permits WRITE therefore stops the recoverable
    operation and allows the irreversible one.

    Declared, never derived. Whether an inverse genuinely restores the prior
    state is a judgement about the service, and :func:`~loom.toolsets.certify`
    checks only that the id resolves (CERT-14)."""

    undone_by: str = ""
    """The operation id that reverses this one, when one exists.

    Richer than the boolean and checkable — and eventually actionable: this is
    what ``ctx.compensate()`` would register to unwind a failed saga, which is
    machinery LOOM already has, wired to a declaration it already needs."""

    access_control: bool = False
    """Does this change *who can reach data*, rather than the data itself?

    ``share`` / ``unshare`` / ``invite`` / ``remove_permission``. AWS promotes
    the same idea to a top-level access level (``Permissions management``)
    rather than a flavour of Write, and for an agent it is the highest-
    consequence category available: **sharing a folder exfiltrates without
    writing anything to it**, and reads as an ordinary additive write.

    Declared, never derived. A scope-based derivation was measured against all
    320 shipped operations and matched **zero** — Google covers permissions
    with the broad scope, so ``drive_share_file`` declares exactly what an
    ordinary write declares, and the Microsoft toolsets declare no scopes at
    all. A name-based one is the F3 mistake in a second place."""

    effect_by: dict[str, dict[str, EffectClass]] = Field(default_factory=dict)
    """Argument-dependent effect: ``{"method": {"GET": READ, "DELETE": DESTRUCTIVE}}``.

    :attr:`effect` is a property of the operation; for a few it is a property
    of the *call*. ``io.http_request`` is one node with one class, and
    ``method="GET"`` is a read while ``method="DELETE"`` destroys — and it is
    precisely the node a generated workflow reaches for when no toolset covers
    the API.

    Declarative rather than a callable, deliberately: grant validation and the
    catalog read manifest metadata without importing a toolset, and a callable
    would need the module.

    A matched rule wins in **either** direction — ``GET`` lowers the class and
    ``DELETE`` raises it, and both are the author's own declaration about their
    own operation. :attr:`effect` is the fallback, used whenever the argument
    was not passed or its value is not in the table, so an unrecognised method
    keeps the cautious class rather than falling to a read.
    """

    open_world: bool = True
    """Does this reach outside the deployment's trust boundary?

    ``True`` for anything that calls a remote service — which is every
    operation in a toolset, so it is only worth setting to ``False`` on a
    manifest wrapping computation the deployment already owns.

    What reads it is the read-to-write taint rule. That rule keys on *reading
    the world*, and it used to approximate that as ``EffectClass.READ``, which
    is not the same thing: filtering a list the run was handed is a READ and
    reads nothing. MCP names the same axis ``openWorldHint`` and gives the same
    example — a web search is open, a memory tool is not.
    """

    scopes: list[str] = Field(default_factory=list)
    """Required OAuth / API scopes."""
    pagination: bool = False
    """Whether this operation returns a page of a larger result set.

    Set it on every read that can return more rows than one request carries.
    The toolset client follows the pages to fill the caller's limit and hands
    back a :class:`~loom.toolsets.pagination.Results`, which knows
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

    ``loom.toolsets.google.gmail.tools``, for example. This is what
    turns a manifest from a description into something generated code can call:
    documentation built from a manifest without it lists operation ids that
    exist in no namespace, and a model writing code against that guesses an
    import — plausibly, confidently, and wrongly."""
    opaque_ids: dict[str, str] = Field(default_factory=dict)
    """Regex for an identifier only this service can issue → the entity kind
    whose resolver produces it.

    The counterpart to :meth:`resolvers`. That says *how* to turn a person's
    word into an id; this says what such an id looks like once it is in the
    code, so a generated workflow containing ``customfield_10042`` can be
    checked against whether anything was ever resolved to produce it. A
    fabricated id is the failure entity resolution exists to prevent, arriving
    one step later: it validates, it runs against fakes, and in production it
    either 400s or writes to whichever field happens to hold that number.

    Only patterns nobody would type from knowledge belong here — a Jira
    ``customfield_10016`` or a Slack ``C024BE91L``, not an integer id that is
    indistinguishable from any other number. **An absent pattern is not a claim
    that a toolset's ids are safe to guess**, only that no pattern describes
    them precisely enough to check, and a check that flags ordinary data is one
    people switch off."""

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
