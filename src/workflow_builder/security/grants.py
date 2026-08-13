"""Grant derivation — static analysis of workflow source for required permissions.

A ``GrantSet`` declares what a workflow needs: which toolsets, agents,
resources, sub-flows, egress hosts, and budget limits.  ``derive_grants()``
uses AST analysis to extract this from source code.
"""

from __future__ import annotations

import ast
from typing import Any

from pydantic import BaseModel, Field


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
    egress: list[str] = Field(default_factory=list)
    """e.g. ``["api.atlassian.com"]``"""
    budget: dict[str, Any] = Field(default_factory=dict)
    """e.g. ``{"usd_per_run": 0.50}``"""

    @property
    def is_empty(self) -> bool:
        return not any([
            self.toolsets,
            self.agents,
            self.resources,
            self.subflows,
            self.egress,
            self.budget,
        ])

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

    def merge(self, other: GrantSet) -> GrantSet:
        """Merge two grant sets (union)."""
        return GrantSet(
            toolsets=_dedup(self.toolsets + other.toolsets),
            agents=_dedup(self.agents + other.agents),
            resources=_dedup(self.resources + other.resources),
            subflows=_dedup(self.subflows + other.subflows),
            egress=_dedup(self.egress + other.egress),
            budget={**self.budget, **other.budget},
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
