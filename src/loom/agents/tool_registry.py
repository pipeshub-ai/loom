"""Three-layer lazy tool system.

Layer 1 — Registration: only metadata (ToolsetManifest) is stored.
Layer 2 — Discovery: agents browse via manifest (search/show/stub).
Layer 3 — Materialization: Tool objects created on demand via resolver.

Usage::

    from loom.agents.tool_registry import Toolset, ToolsetRegistry

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

from loom.core.exceptions import (
    ConfigurationError,
    GrantDenied,
    RegistryError,
)
from loom.core.serde import json_schema_for
from loom.security.grants import GrantSet
from loom.toolsets.catalog import ToolsetCatalog
from loom.toolsets.kinds import ToolsetKind
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.pagination import paginates


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
        """Materialize a single tool. Triggers import on first call.

        The tool is stamped with where it came from — toolset, operation, and
        effect class — because a resolved tool is an ordinary callable that can
        be held, passed on, and invoked long after whatever narrowed the list
        that produced it. Without that provenance, an effect broker asked
        whether a call is permitted has only a display name to go on, and
        ``search`` from one toolset is indistinguishable from ``search`` in
        another.

        Best-effort: a resolver may return a framework-native tool that has no
        metadata to stamp, and a tool that cannot be labelled is still a tool.
        """
        tool = self._resolver(op_id)
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            spec = next(
                (op for op in self.manifest.all_operations() if op.id == op_id), None
            )
            metadata.setdefault("toolset", self.manifest.id)
            metadata.setdefault("operation", op_id)
            if spec is not None:
                metadata.setdefault("effect", spec.effect)
        return tool

    def resolve_all(self) -> list[Any]:
        """Materialize all tools in this toolset."""
        return [self.resolve(op.id) for op in self.manifest.all_operations()]

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_steps(
        cls,
        toolset_id: str,
        steps: list[Any],
        *,
        kind: ToolsetKind = ToolsetKind.APP,
        provider: str = "",
        effects: dict[str, EffectClass] | None = None,
    ) -> Toolset:
        """Build a Toolset from a list of @step-decorated functions.

        The manifest is auto-generated from function signatures and
        docstrings. No manual schema writing needed.

        Parameters
        ----------
        toolset_id:
            Identifier for this toolset (e.g. ``"jira"``).
        steps:
            List of ``@step``-decorated async functions.
        kind, provider:
            Identity of the toolset — see :class:`ToolsetManifest`.
        effects:
            Per-operation :class:`EffectClass` overrides, keyed by tool name.
            Anything not named here is guessed from the name, which is a decent
            default for CRUD-shaped APIs and wrong for plenty of others — say so
            explicitly for operations that are billed, rate-limited, or
            destructive in a way their name does not advertise.
        """
        from loom.agents.tools import coerce_tool

        declared = effects or {}
        tool_map: dict[str, Any] = {}
        ops: list[OperationSpec] = []

        for step_fn in steps:
            t = coerce_tool(step_fn)
            op_id = t.name
            tool_map[op_id] = t
            effect = declared.get(op_id) or _guess_effect(t.name)

            ops.append(OperationSpec(
                id=op_id,
                summary=t.description.split(".")[0] if t.description else op_id,
                description=t.description,
                effect=effect,
                # The name a workflow body will actually write. Left empty, this
                # manifest can neither say how to import itself nor answer what
                # `ctx.step(op, ...)` is — the operation id names a capability,
                # and only a function name is something anyone can call.
                function=op_id,
                input_schema=t.parameters,
                output_schema=_output_schema(step_fn),
                idempotent=(effect == EffectClass.READ),
                # Derived from the return type, never declared — see
                # ``paginates``. One rule, checked mechanically, so a thousand
                # toolsets stay honest with nobody maintaining a list.
                pagination=paginates(step_fn),
            ))

        return cls._build(toolset_id, ops, tool_map, kind=kind, provider=provider)

    @classmethod
    def from_callables(
        cls,
        toolset_id: str,
        callables: list[Any],
        summary: str = "",
        *,
        kind: ToolsetKind = ToolsetKind.APP,
        provider: str = "",
        effects: dict[str, EffectClass] | None = None,
    ) -> Toolset:
        """Build a Toolset from plain callables or LangChain tools.

        Each callable is introspected for name, description, and
        parameter schema. Works with any callable including LangChain
        ``@tool``-decorated functions.

        ``effects`` declares each operation's :class:`EffectClass`; anything
        unnamed is guessed from the operation name.
        """
        from loom.agents.tools import build_parameter_schema

        declared = effects or {}
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
            effect = declared.get(name) or _guess_effect(name)
            ops.append(OperationSpec(
                id=name,
                function=name,
                summary=desc_oneline[:100],
                description=desc,
                input_schema=schema,
                output_schema=_output_schema(fn),
                effect=effect,
                idempotent=(effect == EffectClass.READ),
                # Derived, never declared: returning Results *is* the
                # declaration, so a toolset author writes it once and a
                # thousand of them stay honest without anyone maintaining a
                # parallel list.
                pagination=paginates(fn),
            ))

        return cls._build(
            toolset_id, ops, tool_map, kind=kind, provider=provider, summary=summary
        )

    @classmethod
    def _build(
        cls,
        toolset_id: str,
        ops: list[OperationSpec],
        tool_map: dict[str, Any],
        *,
        kind: ToolsetKind,
        provider: str,
        summary: str = "",
    ) -> Toolset:
        manifest = ToolsetManifest(
            id=toolset_id,
            version="1.0.0",
            kind=kind,
            provider=provider,
            summary=summary or f"{toolset_id} toolset ({len(ops)} tools)",
            groups={toolset_id: ops},
        )

        def resolver(op_id: str) -> Any:
            if op_id not in tool_map:
                known = ", ".join(sorted(tool_map)) or "none"
                msg = (
                    f"Unknown operation '{op_id}' in toolset "
                    f"'{toolset_id}' (known: {known})"
                )
                raise KeyError(msg)
            return tool_map[op_id]

        return cls(manifest=manifest, _resolver=resolver)

    # Convenience alias
    from_langchain = from_callables


class ToolsetRegistry(ToolsetCatalog):
    """The one place toolsets live — for discovery *and* for execution.

    Extends :class:`ToolsetCatalog` so a single object answers both questions
    that used to need two: "what integrations exist?" (search / show / stub,
    used by the coding agent) and "give me the callable tools" (``resolve_*``,
    used by ``ctx.agent()``). Keeping these apart is how a coding agent ends up
    generating correct code against a toolset the runtime cannot actually call.

    A registry may delegate to a *parent* — ``Runtime.toolsets`` chains to the
    process-global registry, so anything registered via ``register_toolset()``
    or a ``loom_toolset`` entry point is visible to every Runtime without
    Runtimes leaking registrations into each other.
    """

    def __init__(
        self,
        parent: ToolsetCatalog | None = None,
        *,
        allow_builtin_fallback: bool = True,
    ) -> None:
        super().__init__()
        self._toolsets: dict[str, Toolset] = {}
        self._parent = parent
        # Default True: a bare ``Runtime()`` / generated ``python flow.py``
        # still resolves ``toolsets=["jira"]`` to Loom's shipped toolsets.
        # Hosts that must never touch process-env credentials (multi-tenant
        # Query) pass ``False`` so an unknown id stays unknown.
        self._allow_builtin_fallback = allow_builtin_fallback

    # -- registration ---------------------------------------------------------

    def register(self, toolset: Toolset | ToolsetManifest) -> None:
        """Register a toolset or a bare manifest. Stores metadata only.

        A bare ``ToolsetManifest`` is discoverable but not callable — useful for
        describing an integration whose tools are generated elsewhere. Asking to
        resolve one raises rather than returning an empty tool list, so the gap
        surfaces at wiring time instead of as an agent that mysteriously has no
        tools.
        """
        if isinstance(toolset, ToolsetManifest):
            super().register(toolset)
            return

        manifest = toolset.manifest
        existing = self._toolsets.get(manifest.id)
        if existing is not None and existing.manifest.qualified_id != manifest.qualified_id:
            raise ConfigurationError(
                f"toolset id '{manifest.id}' is already registered by "
                f"{existing.manifest.qualified_id!r}; registering "
                f"{manifest.qualified_id!r} under the same id would hide it. "
                "Give one of them a distinct id, kind, or provider."
            )
        super().register(manifest)
        self._toolsets[manifest.id] = toolset

    def unregister(self, toolset_id: str) -> None:
        super().unregister(toolset_id)
        self._toolsets.pop(toolset_id, None)

    # -- lookup ---------------------------------------------------------------

    def get(self, toolset_id: str) -> ToolsetManifest | None:  # type: ignore[override]
        found = super().get(toolset_id)
        if found is None and self._parent is not None:
            return self._parent.get(toolset_id)
        return found

    def effect_of(self, function: str) -> EffectClass | None:
        """As :meth:`ToolsetCatalog.effect_of`, continuing into the parent.

        Without the chain a step from an entry-point toolset — the ordinary way
        a first-party integration arrives — would resolve to nothing here, and
        its reads would go on reaching the broker classified as writes.
        """
        found = super().effect_of(function)
        if found is None and self._parent is not None:
            return self._parent.effect_of(function)
        return found

    def get_toolset(self, toolset_id: str) -> Toolset | None:
        """The executable toolset for *toolset_id*, or ``None``.

        Checked in order: registered here, registered on the parent, then
        (when :attr:`_allow_builtin_fallback` is true) the toolsets LOOM
        ships. That last step is what lets a generated workflow run
        standalone — ``python generated_workflow.py`` registers nothing,
        and ``toolsets=["jira"]`` used to fail with "no executable toolset
        'jira' is registered (known: none)".

        A fallback, not a registration: the built-ins stay out of
        ``list_toolsets`` and so out of :meth:`resolve_tools`'s no-ids sweep.
        Asking for one by name gets it; asking for "everything" does not
        quietly acquire four integrations and their destructive operations.
        Hosts that pass ``allow_builtin_fallback=False`` skip the last step
        so an unregistered id cannot resolve to a process-env credentialed
        builtin.
        """
        found = self._toolsets.get(toolset_id)
        if found is not None:
            return found
        if isinstance(self._parent, ToolsetRegistry):
            inherited = self._parent.get_toolset(toolset_id)
            if inherited is not None:
                return inherited
        if not self._allow_builtin_fallback:
            return None

        from loom.toolsets.registry import builtin_toolset

        return builtin_toolset(toolset_id)

    def list_toolsets(self) -> list[str]:
        """Every discoverable toolset id, this registry's and its parent's."""
        ids = list(self._manifests)
        if self._parent is not None:
            ids += [i for i in self._parent.toolset_ids if i not in self._manifests]
        return ids

    @property
    def toolset_ids(self) -> list[str]:  # type: ignore[override]
        return self.list_toolsets()

    def search(self, query: str, *, limit: int = 10) -> list[Any]:
        """Search this registry and its parent, nearest match first."""
        cards = super().search(query, limit=limit)
        if self._parent is not None:
            seen = {c.toolset_id for c in cards}
            cards += [
                c
                for c in self._parent.search(query, limit=limit)
                if c.toolset_id not in seen
            ]
        return cards[:limit]

    def show(self, toolset_id: str, group: str | None = None) -> Any:
        if toolset_id not in self._manifests and self._parent is not None:
            return self._parent.show(toolset_id, group)
        return super().show(toolset_id, group)

    def stub(self, op_path: str) -> Any:
        dot = op_path.find(".")
        head = op_path[:dot] if dot != -1 else op_path
        if head not in self._manifests and self._parent is not None:
            return self._parent.stub(op_path)
        return super().stub(op_path)

    # -- materialization ------------------------------------------------------

    def resolve_tools(
        self,
        toolset_ids: list[str] | None = None,
        *,
        effects: set[EffectClass] | None = None,
        grants: GrantSet | None = None,
    ) -> list[Any]:
        """Materialize Tool objects. Triggers imports.

        Parameters
        ----------
        toolset_ids:
            If None, resolves every executable toolset. If a list, resolves only
            those — an unknown id raises rather than being skipped, because a
            typo that silently yields fewer tools is very hard to notice from the
            other side of an agent call.
        effects:
            Optional filter on :class:`EffectClass`. Pass ``{EffectClass.READ}``
            to hand an agent a strictly read-only toolset.
        grants:
            Optional :class:`GrantSet`. Operations outside it are withheld, and
            naming a toolset that the grants deny outright raises
            :class:`GrantDenied` — asking for a toolset you may not use is a
            configuration error, not something to silently narrow.
        """
        if toolset_ids is None:
            targets = [
                ts
                for tid in self.list_toolsets()
                if (ts := self.get_toolset(tid)) is not None
            ]
        else:
            targets = []
            for tid in toolset_ids:
                ts = self.get_toolset(tid)
                if ts is None:
                    known = ", ".join(sorted(self.list_toolsets())) or "none"
                    raise RegistryError(
                        f"no executable toolset '{tid}' is registered (known: {known})"
                    )
                targets.append(ts)

        tools: list[Any] = []
        for ts in targets:
            ops = ts.manifest.all_operations()
            if effects is not None:
                ops = [op for op in ops if op.effect in effects]
            if grants is not None:
                permitted = [
                    op
                    for op in ops
                    if grants.allows_operation(ts.manifest.id, op.id, op.effect.value)
                ]
                if not permitted and toolset_ids is not None:
                    raise GrantDenied(
                        f"workflow grants do not permit any operation on toolset "
                        f"'{ts.manifest.id}'",
                        grant=", ".join(grants.toolsets) or "none",
                        required=ts.manifest.id,
                    )
                ops = permitted
            tools.extend(ts.resolve(op.id) for op in ops)
        return tools

    def resolve_one(self, toolset_id: str, op_id: str) -> Any:
        """Resolve a single tool by toolset and operation ID."""
        ts = self.get_toolset(toolset_id)
        if ts is None:
            msg = f"Toolset '{toolset_id}' is not registered as executable"
            raise RegistryError(msg)
        return ts.resolve(op_id)

    # -- documentation --------------------------------------------------------

    def describe(
        self, toolset_ids: list[str] | None = None, *, detail: str = "full"
    ) -> str:
        """Auto-generate tool documentation from manifests.

        No hand-written docs — built from ToolsetManifest metadata (operation
        names, summaries, input schemas). Each heading carries the toolset's
        qualified id so a model can distinguish two toolsets wrapping the same
        service.

        ``detail="index"`` emits only the index card for each toolset — what it
        is, its groups, how to import it — and leaves the operations to be
        fetched on demand. That is the point of the three-tier catalog: a prompt
        carrying every operation of every registered integration costs tens of
        thousands of tokens before the model has read the spec, and grows with
        each integration installed rather than with the task at hand.
        """
        ids = toolset_ids if toolset_ids is not None else self.list_toolsets()
        manifests = [m for tid in ids if (m := self.get(tid)) is not None]
        if not manifests:
            return ""

        lines = [
            "## Available toolsets",
            "",
            "A toolset that shows an Import line is callable from workflow code:",
            "import those names and call them with ctx.step(). Use exactly the",
            "import shown — the operation id (e.g. 'messages.search') names the",
            "capability and is not importable.",
            "",
            "A toolset with no Import line is reachable only through ctx.agent();",
            "name its operations in the prompt rather than writing calls to them.",
            "",
            *PAGING_HOWTO,
        ]
        for m in manifests:
            lines.append(f"### {m.id} [{m.qualified_id}] -- {m.summary}")

            imports = m.import_line()
            if imports:
                lines.append(f"  Import: {imports}")
                lines.append("  Call with: await ctx.step(<function>, ...)")
            elif not self.get_toolset(m.id):
                lines.append("  (metadata only — reachable via ctx.agent(), not importable)")

            # Name the resolvers explicitly. Filtering on a human's words is the
            # common way a query returns nothing without failing, and the fix is
            # only obvious if the caller knows which operation does the joining.
            for kind, op in sorted(m.resolvers().items()):
                lines.append(
                    f"  Resolve a {kind} with {op.function or op.id} before "
                    f"filtering on one — the API matches ids, not display names."
                )

            # Say which reads page. The flag was already on the manifests and
            # went nowhere near the model, so a generated workflow capped at
            # whatever number looked reasonable and reported the count as a
            # total. Naming them is what makes ".complete" a question anyone
            # thinks to ask.
            paging = sorted(op.function or op.id for op in m.paginated())
            if paging:
                # A named example, not a rule to infer from. A small model
                # copies; asking it to derive the call from a sentence is where
                # "show all the stories" became max_results=100 and a count
                # reported as a total.
                lines.append(f"  Paged: {', '.join(paging)}")

            if detail == "index":
                # Names, not signatures. A name is what a caller needs to know
                # the capability exists and to name it in an agent prompt; the
                # parameters and schemas are a tool call away and are the part
                # that would make this grow without bound.
                #
                # A bridge toolset (e.g. PipesHub) routes every operation
                # through ONE shared function, so `op.function` is identical
                # for all of them. Showing "pipeshub_tool" repeated N times
                # tells the model nothing; fall back to `op.id` (e.g.
                # "gmail.send_email") which names the actual capability.
                all_ops = list(m.all_operations())
                functions = [op.function for op in all_ops if op.function]
                use_ids = len(set(functions)) < len(functions)
                names = ", ".join(
                    op.id if use_ids else (op.function or op.id)
                    for op in all_ops
                )
                lines.append(f"  Operations: {names}")
                lines.append(
                    f'  For signatures and schemas: show_toolset("{m.id}") '
                    f'then get_tool_contract("{m.id}.<op_id>"). '
                    f'For examples: get_tool_docs("{m.id}").'
                )
                lines.append("")
                continue

            # For a bridge toolset where all operations share one function,
            # use the operation id — the function name repeated is not useful.
            all_ops = list(m.all_operations())
            use_ids = len(set(op.function for op in all_ops if op.function)) < len(all_ops)
            for op in all_ops:
                label = op.id if use_ids else (op.function or op.id)
                sig = _schema_to_signature(label, op.input_schema)
                suffix = f"  [{op.effect.value}]"
                lines.append(f"  - {sig}{suffix}")
                if op.summary:
                    lines.append(f"    {op.summary}")
            lines.append("")
        return "\n".join(lines)


_WRITE_WORDS = ("create", "update", "add", "transition", "assign", "upsert", "index", "send")
_DESTRUCTIVE_WORDS = ("delete", "remove", "drop", "purge", "revoke")


def _guess_effect(name: str) -> EffectClass:
    """Best-effort effect classification from an operation name.

    A fallback, not an authority — pass ``effects={...}`` to the factory when
    the name does not tell the truth about what the call does.
    """
    lowered = name.lower()
    if any(word in lowered for word in _DESTRUCTIVE_WORDS):
        return EffectClass.DESTRUCTIVE
    if any(word in lowered for word in _WRITE_WORDS):
        return EffectClass.WRITE
    return EffectClass.READ


def _output_schema(fn: Any) -> dict[str, Any]:
    """Derive a JSON Schema for the return type, so the agent knows the shape."""

    target = getattr(fn, "fn", fn)
    annotation = getattr(target, "__annotations__", {}).get("return")
    if annotation is None:
        return {}
    try:
        return json_schema_for(annotation)
    except Exception:
        return {}


#: How to use a paged read. Printed once for the whole catalog, not once per
#: toolset: the pattern is identical everywhere, and a per-toolset copy would
#: make the index grow with the number of integrations installed rather than
#: with the task — which is the cost the three-tier catalog exists to avoid.
PAGING_HOWTO = [
    "A read marked 'paged' returns at most its max_results/limit, following",
    "the API's pages to fill it, and the result knows whether it saw",
    "everything — .complete survives being returned from a step.",
    "  Bounded set — one call, then say what it covers:",
    "      found = await ctx.step(<paged read>, ..., max_results=200)",
    '      header = f"showing {found.summary()}"   # "200 of 312"',
    "  Unbounded set (a mailbox, a log): raising max_results is wrong — one",
    "  call for 50,000 rows is one journal entry, so a crash refetches all of",
    "  them. No shipped read takes a cursor argument, so bound the window",
    "  instead of the row count — a date range, a label, a status — and run",
    "  the workflow on a schedule over successive windows:",
    "      since = await ctx.state.get('since') or default_start",
    "      found = await ctx.step(<paged read>, query_for(since), max_results=500)",
    "      await ctx.state.set('since', new_watermark)",
    "  Filter at the service, never after: pass the predicate to the query",
    "  (JQL, q=, $filter, SOQL) rather than fetching everything and keeping",
    "  the rows you wanted. A comprehension over a paged result is also a",
    "  plain list, so it silently drops .complete — use .filtered(...) when",
    "  the predicate genuinely cannot be expressed server-side.",
    "",
]


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
