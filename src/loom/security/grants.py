"""Grant derivation — static analysis of workflow source for required permissions.

A ``GrantSet`` declares what a workflow needs: which toolsets, agents,
resources, sub-flows, egress hosts, and budget limits.  ``derive_grants()``
uses AST analysis to extract this from source code.
"""

from __future__ import annotations

import ast
from typing import Any

from pydantic import BaseModel, Field

#: Effects a toolset entry may pin, matching :class:`EffectClass` values.
_EFFECTS = frozenset({"read", "write", "destructive"})


class GrantIssue(BaseModel):
    """One grant entry that names nothing the runtime can see."""

    entry: str
    dimension: str
    """Which list it came from: ``toolsets``, ``agents``, …"""
    reason: str
    suggestions: list[str] = Field(default_factory=list)

    @classmethod
    def of(
        cls, dimension: str, entry: str, known: list[str], *, part: str | None = None
    ) -> GrantIssue:
        """Build an issue with the closest known names attached."""
        from loom.nodes.base import near_matches

        wanted = part or entry
        return cls(
            entry=entry,
            dimension=dimension,
            reason=f"no {dimension[:-1]} named {wanted!r} is registered",
            suggestions=near_matches(wanted, known),
        )

    def __str__(self) -> str:
        hint = f" — did you mean {', '.join(self.suggestions)}?" if self.suggestions else ""
        return f"grant {self.entry!r}: {self.reason}{hint}"


class GrantSet(BaseModel):
    """Declares the permissions a workflow requires.

    Used by the gateway (Phase 5) for enforcement.  In embedded mode,
    grants are informational — they document what the workflow does.
    """

    toolsets: list[str] = Field(default_factory=list)
    """e.g. ``["jira.issues:write", "slack.chat:write"]``"""
    agents: list[str] = Field(default_factory=list)
    """e.g. ``["support-triage"]``"""
    resources: list[str] = Field(default_factory=list)
    """e.g. ``["pg:read"]``"""
    subflows: list[str] = Field(default_factory=list)
    """Referenced sub-workflow names."""
    nodes: list[str] = Field(default_factory=list)
    """Catalogued nodes this workflow may call, e.g. ``["io.http_request"]``.

    Its own dimension, and it had to become one. A node call journals as a step
    whose target is ``<category>.<id>``, which the broker's bridged-toolset
    branch read as a *toolset* called ``control`` — a toolset no manifest
    declares and no grant can name. So a workflow that narrowed itself to
    ``toolsets=["jira.issues:read"]`` — the very workflow a careful author
    writes — had every ``ctx.node()`` call refused, ``control.switch``
    included, with an error telling it to grant a toolset that does not exist.

    An entry is a node id, or a category to permit all of it
    (``"human"`` permits ``human.approval`` and ``human.choice``).
    """
    egress: list[str] = Field(default_factory=list)
    """e.g. ``["api.atlassian.com"]``"""
    budget: dict[str, Any] = Field(default_factory=dict)
    """e.g. ``{"usd_per_run": 0.50}``"""
    strict: bool = False
    """Close every dimension, not just the ones with entries.

    Off by default so an existing ``GrantSet`` that only ever declared
    ``toolsets`` keeps behaving exactly as before. When ``True``,
    :class:`~loom.runtime.effects.GuardedBroker` denies ``agent``
    and ``child`` calls too when ``agents``/``subflows`` is empty, instead of
    treating "nothing declared" as "nothing to check" for that dimension.
    An identity-derived grant (e.g. from an OAuth scope) should set this —
    a token that grants ``jira:read`` must not leave every ``ctx.agent()``
    call unchecked simply because the token said nothing about agents.
    """

    @property
    def is_empty(self) -> bool:
        """True when this grant set narrows nothing at all.

        ``strict`` alone makes this ``False`` even with every list empty —
        a strict grant with nothing declared means "deny everything", the
        opposite of unrestricted, and must not be short-circuited to
        "permit everything" by ``Authority.is_unrestricted`` or
        :class:`~loom.runtime.effects.GuardedBroker`.
        """
        if self.strict:
            return False
        return not any([
            self.toolsets,
            self.agents,
            self.resources,
            self.subflows,
            self.nodes,
            self.egress,
            self.budget,
        ])

    def allows_node(self, node_id: str) -> bool:
        """Whether this grant set permits calling *node_id*.

        Entries are a full id (``"io.http_request"``) or a category
        (``"io"``). Categories are the useful granularity: a workflow either
        may reach out over HTTP or it may not, and enumerating every ``io.*``
        node to say so would go stale the moment one is added.
        """
        category, _, _ = node_id.partition(".")
        return any(entry in (node_id, category) for entry in self.nodes)

    def allows_operation(self, toolset_id: str, op_id: str, effect: str) -> bool:
        """Whether this grant set permits one operation on one toolset.

        Entries are ``<toolset>[.<group>][:<effect>]``, matched most-specific
        first::

            "jira"                 every jira operation
            "jira:read"            jira reads only
            "jira.issues"          the issues group
            "jira.issues:write"    writes within the issues group

        An empty ``toolsets`` list grants nothing. That is deliberate: a grant
        set is opt-in, and a caller that has declared *some* toolsets but not
        this one should be denied rather than waved through.
        """
        group = op_id.split(".")[0] if "." in op_id else ""
        for entry in self.toolsets:
            scope, _, granted_effect = entry.partition(":")
            if granted_effect and granted_effect != effect:
                continue
            if scope == toolset_id:
                return True
            if group and scope == f"{toolset_id}.{group}":
                return True
        return False

    def validate_against(
        self,
        *,
        toolsets: Any = None,
        agents: Any = None,
    ) -> list[GrantIssue]:
        """Check that every entry names something that exists.

        An unrecognized entry is worse than a wrong one, because it fails
        silently in the safe direction: ``allows_operation`` matches nothing,
        so a workflow declaring ``jira.issues:writ`` gets an empty toolset and
        the failure surfaces much later as "the agent could not find a tool".
        An operator reading ``grants=[...]`` would reasonably believe the
        workflow is restricted to what it lists; a typo means it is restricted
        to nothing, which looks identical from the outside until it doesn't.

        Returns issues rather than raising, because the right response differs
        by caller: registration raises, ``loom check`` prints, and the coding
        agent repairs.

        Args:
            toolsets: A registry answering ``list_toolsets()``, and optionally
                ``get(id)`` for group checking. Only manifest metadata is read
                — no toolset is imported to validate a string.
            agents: An optional collection of known agent names.
        """
        issues: list[GrantIssue] = []
        if toolsets is not None:
            issues += self._toolset_issues(toolsets)
        if agents is not None:
            known = sorted(agents)
            for name in self.agents:
                if name not in known:
                    issues.append(GrantIssue.of("agents", name, known))
        return issues

    def _toolset_issues(self, registry: Any) -> list[GrantIssue]:
        known = list(registry.list_toolsets())
        if not known:
            # Nothing registered is not the same as nothing valid. Toolsets load
            # lazily and via entry points, so a registry that is empty at this
            # moment says nothing about the entries — and flagging all of them
            # would fail every workflow whose grants are declared before its
            # integrations are registered.
            return []
        issues: list[GrantIssue] = []
        for entry in self.toolsets:
            scope, effect = _parse_toolset_entry(entry)
            toolset_id, _, group = scope.partition(".")

            if toolset_id not in known:
                issues.append(GrantIssue.of("toolsets", entry, known, part=toolset_id))
                continue
            if effect and effect not in _EFFECTS:
                issues.append(
                    GrantIssue(
                        entry=entry,
                        dimension="toolsets",
                        reason=f"unknown effect {effect!r}",
                        suggestions=sorted(_EFFECTS),
                    )
                )
                continue
            if group:
                manifest = registry.get(toolset_id)
                groups = sorted(getattr(manifest, "groups", {}) or {})
                if groups and group not in groups:
                    issues.append(
                        GrantIssue(
                            entry=entry,
                            dimension="toolsets",
                            reason=f"toolset {toolset_id!r} has no group {group!r}",
                            suggestions=[f"{toolset_id}.{g}" for g in groups[:3]],
                        )
                    )
        return issues

    def merge(self, other: GrantSet) -> GrantSet:
        """Merge two grant sets (union) — widens what is permitted.

        Use this to combine what *several declared workflows* need. Do not
        use it to combine a declared grant with an identity-derived one
        (a token's scopes, a delegated caller's authority) — that direction
        must narrow, which is what :meth:`intersect` is for. ``strict`` is
        ORed rather than dropped, so merging a strict grant with a lenient
        one does not silently reopen the dimensions the strict side closed.
        """
        return GrantSet(
            toolsets=_dedup(self.toolsets + other.toolsets),
            agents=_dedup(self.agents + other.agents),
            resources=_dedup(self.resources + other.resources),
            subflows=_dedup(self.subflows + other.subflows),
            egress=_dedup(self.egress + other.egress),
            budget={**self.budget, **other.budget},
            strict=self.strict or other.strict,
        )

    def intersect(self, other: GrantSet) -> GrantSet:
        """What both grant sets permit — narrows, never widens.

        This is the direction identity must travel: mapping a token's scopes
        onto a workflow's declared grant must never grant *more* than the
        workflow declared, no matter how broad the token's scopes are.
        ``declared.intersect(scopes_to_grant(token.scopes, declared))``
        (see :mod:`loom.identity.scopes`) is the intended call
        shape, and the result is provably a subset of both operands:

        - ``toolsets`` entries are kept only when one side's entry *covers*
          the other's (same scope, or a broader scope with a compatible
          effect) — the narrower of the pair survives. Two entries that
          govern different toolsets contribute nothing, so a caller with
          no matching entry gets no toolset access at all rather than a
          leftover from one side.
        - ``agents``/``resources``/``subflows``/``egress`` intersect as
          plain sets — an exact match on both sides.
        - ``budget`` keeps a key from either side (absence means
          unconstrained, i.e. the more permissive value), taking the
          numeric minimum when both sides declare the same key.
        - ``strict`` is ORed: intersecting with a strict grant must not
          make the result less strict than either input.
        """
        return GrantSet(
            toolsets=_intersect_toolsets(self.toolsets, other.toolsets),
            agents=_intersect_plain(self.agents, other.agents),
            resources=_intersect_plain(self.resources, other.resources),
            subflows=_intersect_plain(self.subflows, other.subflows),
            egress=_intersect_plain(self.egress, other.egress),
            budget=_intersect_budget(self.budget, other.budget),
            strict=self.strict or other.strict,
        )


def _dedup(items: list[str]) -> list[str]:
    """Deduplicate while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _intersect_plain(a: list[str], b: list[str]) -> list[str]:
    """Exact-match set intersection, order taken from `a`."""
    held = set(b)
    return _dedup([item for item in a if item in held])


def _parse_toolset_entry(entry: str) -> tuple[str, str]:
    """Split a ``<toolset>[.<group>][:<effect>]`` entry into (scope, effect).

    ``effect == ""`` means "any effect" — an unqualified entry like ``"jira"``.
    """
    scope, _, effect = entry.partition(":")
    return scope, effect


def _covers(broad: tuple[str, str], narrow: tuple[str, str]) -> bool:
    """Whether everything `narrow` permits is also permitted by `broad`."""
    broad_scope, broad_effect = broad
    narrow_scope, narrow_effect = narrow
    scope_ok = narrow_scope == broad_scope or narrow_scope.startswith(f"{broad_scope}.")
    effect_ok = broad_effect == "" or broad_effect == narrow_effect
    return scope_ok and effect_ok


def _intersect_toolsets(a: list[str], b: list[str]) -> list[str]:
    """Keep an entry only where the other side grants at least as much.

    For each pair of entries, one across `a` and one across `b`: if they are
    identical, keep it; if one covers the other, keep the narrower one.
    Entries with no counterpart on the other side (including everything, when
    either list is empty) contribute nothing — the other side never declared
    that access, so it cannot survive an intersection.
    """
    result: list[str] = []
    parsed_a = [(entry, _parse_toolset_entry(entry)) for entry in a]
    parsed_b = [(entry, _parse_toolset_entry(entry)) for entry in b]
    for entry_a, scope_a in parsed_a:
        for entry_b, scope_b in parsed_b:
            if scope_a == scope_b:
                result.append(entry_a)
            elif _covers(scope_a, scope_b):
                result.append(entry_b)
            elif _covers(scope_b, scope_a):
                result.append(entry_a)
    return _dedup(result)


def _intersect_budget(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Narrower budget: min of shared numeric keys, else whichever side has it."""
    result = dict(a)
    for key, value in b.items():
        current = result.get(key)
        if key in result and isinstance(current, int | float) and isinstance(value, int | float):
            result[key] = min(current, value)
        elif key not in result:
            result[key] = value
    return result


def derive_grants(source: str) -> GrantSet:
    """Derive required grants from workflow source code via AST analysis.

    Scans for:
    - ``ctx.agent(...)`` calls → agent names
    - ``ctx.child(...)`` calls → sub-workflow names
    - Import statements referencing toolset packages
    - String literals matching toolset patterns (``toolset.group``)

    This is a best-effort heuristic — it cannot capture dynamic tool
    usage or runtime-computed names.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return GrantSet()

    visitor = _GrantVisitor()
    visitor.visit(tree)
    return visitor.grants


class _GrantVisitor(ast.NodeVisitor):
    """AST visitor that extracts grant-relevant patterns."""

    def __init__(self) -> None:
        self.grants = GrantSet()

    def visit_Call(self, node: ast.Call) -> None:
        # Detect ctx.agent(..., agent_name) or ctx.child(workflow_name, ...)
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "agent" and node.args:
                name = self._extract_name(node.args[0])
                if name:
                    self.grants.agents.append(name)
            elif attr == "child" and node.args:
                name = self._extract_name(node.args[0])
                if name:
                    self.grants.subflows.append(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Detect toolset imports like: from loom_toolset_jira import ...
        if node.module and node.module.startswith("loom_toolset_"):
            toolset_id = node.module.replace("loom_toolset_", "", 1)
            self.grants.toolsets.append(toolset_id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith("loom_toolset_"):
                toolset_id = alias.name.replace("loom_toolset_", "", 1)
                self.grants.toolsets.append(toolset_id)
        self.generic_visit(node)

    @staticmethod
    def _extract_name(node: ast.expr) -> str | None:
        """Try to extract a string name from an AST node."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return node.id
        return None
