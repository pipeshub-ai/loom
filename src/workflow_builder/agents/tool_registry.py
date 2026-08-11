"""Three-layer lazy tool system.

Layer 1 — Registration: only metadata (ToolsetManifest) is stored.
Layer 2 — Discovery: agents browse via manifest (search/show/stub).
Layer 3 — Materialization: Tool objects created on demand via resolver.

Usage::

    from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry

    registry = ToolsetRegistry()
    registry.register(Toolset.from_steps("jira", [jira_search, jira_create]))
    registry.register(Toolset.from_langchain("web", [search_tool, fetch]))

    # Discover (no imports triggered)
    registry.describe()          # auto-generated tool docs
    registry.list_toolsets()     # ["jira", "web"]

    # Resolve (triggers imports)
    tools = registry.resolve_tools()                # all tools
    tools = registry.resolve_tools(["jira"])         # just jira
    tool  = registry.resolve_one("jira", "search")  # single tool
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)


@dataclass
class Toolset:
    """A group of tools with lazy loading.

    Bundles a ``ToolsetManifest`` (metadata, no imports) with a
    resolver callable that materializes actual ``Tool`` objects
    on demand.
    """

    manifest: ToolsetManifest
    _resolver: Callable[[str], Any] = field(repr=False)
    """Maps an operation ID (e.g. ``"issues.search"``) to a Tool."""

    def resolve(self, op_id: str) -> Any:
        """Materialize a single tool. Triggers import on first call."""
        return self._resolver(op_id)

    def resolve_all(self) -> list[Any]:
        """Materialize all tools in this toolset."""
        return [self.resolve(op.id) for op in self.manifest.all_operations()]

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_steps(cls, toolset_id: str, steps: list[Any]) -> Toolset:
        """Build a Toolset from a list of @step-decorated functions.

        The manifest is auto-generated from function signatures and
        docstrings. No manual schema writing needed.

        Parameters
        ----------
        toolset_id:
            Identifier for this toolset (e.g. ``"jira"``).
        steps:
            List of ``@step``-decorated async functions.
        """
        from workflow_builder.agents.tools import coerce_tool

        # Build manifest from step signatures
        tool_map: dict[str, Any] = {}
        ops: list[OperationSpec] = []

        for step_fn in steps:
            t = coerce_tool(step_fn)
            op_id = t.name
            tool_map[op_id] = t

            # Determine effect from metadata or name
            effect = EffectClass.READ
            name_lower = t.name.lower()
            if any(w in name_lower for w in ("create", "update", "add", "transition", "assign")):
                effect = EffectClass.WRITE
            elif any(w in name_lower for w in ("delete", "remove")):
                effect = EffectClass.DESTRUCTIVE

            ops.append(OperationSpec(
                id=op_id,
                summary=t.description.split(".")[0] if t.description else op_id,
                description=t.description,
                effect=effect,
                input_schema=t.parameters,
                idempotent=(effect == EffectClass.READ),
            ))

        manifest = ToolsetManifest(
            id=toolset_id,
            version="1.0.0",
            summary=f"{toolset_id} toolset ({len(ops)} tools)",
            groups={toolset_id: ops},
        )

        def resolver(op_id: str) -> Any:
            if op_id not in tool_map:
                msg = f"Unknown operation '{op_id}' in toolset '{toolset_id}'"
                raise KeyError(msg)
            return tool_map[op_id]

        return cls(manifest=manifest, _resolver=resolver)

    @classmethod
    def from_callables(
        cls,
        toolset_id: str,
        callables: list[Any],
        summary: str = "",
    ) -> Toolset:
        """Build a Toolset from plain callables or LangChain tools.

        Each callable is introspected for name, description, and
        parameter schema. Works with any callable including LangChain
        ``@tool``-decorated functions.
        """
        from workflow_builder.agents.tools import build_parameter_schema

        tool_map: dict[str, Any] = {}
        ops: list[OperationSpec] = []

        for fn in callables:
            name = getattr(fn, "name", None) or getattr(fn, "__name__", "tool")
            desc = getattr(fn, "description", None) or (inspect.getdoc(fn) or "")
            desc_oneline = desc.split("\n")[0] if desc else name

            # Try to get schema
            schema: dict[str, Any] = {}
            if hasattr(fn, "args_schema") and fn.args_schema is not None:
                schema = fn.args_schema.model_json_schema()
            elif hasattr(fn, "parameters"):
                schema = fn.parameters if isinstance(fn.parameters, dict) else {}
            else:
                try:
                    schema, _ = build_parameter_schema(fn)
                except (TypeError, ValueError):
                    schema = {"type": "object", "properties": {}}

            tool_map[name] = fn
            ops.append(OperationSpec(
                id=name,
                summary=desc_oneline[:100],
                description=desc,
                input_schema=schema,
                effect=EffectClass.READ,
                idempotent=True,
            ))

        manifest = ToolsetManifest(
            id=toolset_id,
            version="1.0.0",
            summary=summary or f"{toolset_id} ({len(ops)} tools)",
            groups={toolset_id: ops},
        )

        def resolver(op_id: str) -> Any:
            if op_id not in tool_map:
                msg = f"Unknown operation '{op_id}' in toolset '{toolset_id}'"
                raise KeyError(msg)
            return tool_map[op_id]

        return cls(manifest=manifest, _resolver=resolver)

    # Convenience alias
    from_langchain = from_callables


class ToolsetRegistry:
    """Stores toolsets and resolves tools lazily.

    Lives on ``Runtime`` as ``rt.toolsets``.
    """

    def __init__(self) -> None:
        self._toolsets: dict[str, Toolset] = {}

    def register(self, toolset: Toolset) -> None:
        """Register a toolset. Only stores metadata — no imports."""
        self._toolsets[toolset.manifest.id] = toolset

    def get(self, toolset_id: str) -> Toolset | None:
        return self._toolsets.get(toolset_id)

    def list_toolsets(self) -> list[str]:
        return list(self._toolsets)

    def resolve_tools(
        self, toolset_ids: list[str] | None = None
    ) -> list[Any]:
        """Resolve Tool objects. Triggers imports.

        Parameters
        ----------
        toolset_ids:
            If None, resolves ALL registered toolsets.
            If a list, resolves only the named toolsets.
        """
        targets = (
            self._toolsets.values()
            if toolset_ids is None
            else [
                self._toolsets[tid]
                for tid in toolset_ids
                if tid in self._toolsets
            ]
        )
        tools: list[Any] = []
        for ts in targets:
            tools.extend(ts.resolve_all())
        return tools

    def resolve_one(self, toolset_id: str, op_id: str) -> Any:
        """Resolve a single tool by toolset and operation ID."""
        ts = self._toolsets.get(toolset_id)
        if ts is None:
            msg = f"Toolset '{toolset_id}' not registered"
            raise KeyError(msg)
        return ts.resolve(op_id)

    def describe(
        self, toolset_ids: list[str] | None = None
    ) -> str:
        """Auto-generate tool documentation from manifests.

        No hand-written docs — built from ToolsetManifest metadata
        (operation names, summaries, input schemas).
        """
        targets = (
            list(self._toolsets.values())
            if toolset_ids is None
            else [
                self._toolsets[tid]
                for tid in toolset_ids
                if tid in self._toolsets
            ]
        )
        if not targets:
            return ""

        lines = [
            "## Available tools for ctx.agent()",
            "",
            "The agent has access to the following tools.",
            "Reference tool names explicitly in your prompt",
            "so the agent knows which ones to use.",
            "",
        ]
        for ts in targets:
            m = ts.manifest
            lines.append(f"### {m.id} -- {m.summary}")
            for op in m.all_operations():
                sig = _schema_to_signature(op.id, op.input_schema)
                lines.append(f"  - {sig}")
                if op.summary:
                    lines.append(f"    {op.summary}")
            lines.append("")
        return "\n".join(lines)


def _schema_to_signature(name: str, schema: dict[str, Any]) -> str:
    """Convert a JSON Schema to a human-readable function signature."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    parts: list[str] = []
    for pname, pschema in props.items():
        ptype = pschema.get("type", "any")
        default = pschema.get("default")
        if pname in required or default is None:
            parts.append(f"{pname}: {ptype}")
        else:
            parts.append(f"{pname}: {ptype} = {default!r}")
    return f"{name}({', '.join(parts)})"
