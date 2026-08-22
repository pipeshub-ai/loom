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
import logging
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

logger = logging.getLogger(__name__)


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
                # The other three facets travel with it, because a tool call
                # inside an agent loop reaches the same broker a `ctx.step`
                # does — and an agent deciding to send an email is the case
                # `block_irreversible` exists for. Without these the dial sees
                # every agent tool call as irreversible and open-world, which
                # makes it refuse everything rather than the right things.
                metadata.setdefault("open_world", spec.open_world)
                metadata.setdefault("reversible", spec.reversible)
                metadata.setdefault("access_control", spec.access_control)
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
            effect = declared.get(op_id) or _resolve_guess(t.name)

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
            # Tested rather than annotated. `getattr` returns Any, and
            # unannotated that Any spread into the OperationSpec id, the
            # function name and the tool_map key — so nothing downstream was
            # checked at all. A callable whose `name` is not a string gets
            # the fallback rather than a str() of whatever it was.
            raw_name = getattr(fn, "name", None) or getattr(fn, "__name__", None)
            name: str = raw_name if isinstance(raw_name, str) else "tool"
            raw_desc = getattr(fn, "description", None) or inspect.getdoc(fn)
            # Tested rather than annotated: `description` comes off an
            # arbitrary callable, so a non-string one should become no
            # description rather than a str() of whatever it was.
            desc: str = raw_desc if isinstance(raw_desc, str) else ""
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
            effect = declared.get(name) or _resolve_guess(name)
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

    # -- the two scopes -------------------------------------------------------
    #
    # Discovery and execution are different questions, and the difference is
    # the whole of why `search_toolsets("jira")` answered nothing on a
    # `loom author` run while `toolsets=["jira"]` in the file it wrote resolved
    # perfectly. `get_toolset` below has drawn this line since the built-in
    # fallback was added — *asking for one by name gets it; asking for
    # "everything" does not* — and nothing ever drew it for the catalogue.
    #
    #   execution scope   what a no-ids sweep may reach: what somebody registered
    #   catalogue scope   what may be *named*: registrations, then the built-ins
    #
    # `resolve_tools(None)` reads the first and is unchanged. Everything a model
    # browses reads the second.

    def _execution_tiers(self) -> tuple[Any, ...]:
        """Tiers a no-ids sweep may reach. Tier 0 is this registry's own.

        ``super()`` rather than ``self``: these tiers are what the reads below
        consult, so tier 0 must be the *inherited* implementation or every
        lookup would re-enter itself.
        """
        tiers: list[Any] = [super()]
        if self._parent is not None:
            tiers.append(self._parent)
        return tuple(tiers)

    def _catalogue_tiers(self) -> tuple[Any, ...]:
        """Execution tiers, then the toolsets LOOM ships.

        The built-in tier is **last**, so a host registering its own ``jira``
        shadows it, and it is constructed unloaded — a Runtime that never
        browses a catalogue never imports the 27 manifest modules.
        """
        tiers = list(self._execution_tiers())
        if self._allow_builtin_fallback:
            from loom.toolsets.registry import builtin_catalog

            tiers.append(builtin_catalog())
        return tuple(tiers)

    # -- chaining -------------------------------------------------------------
    #
    # Three helpers, because the alternative is an override per method — and an
    # override per method is exactly how `search_operations` and `profile_of`
    # came to stop at tier 0. Both were added to `ToolsetCatalog` after these
    # overrides were written and neither got one. `profile_of` is read on every
    # `ctx.step` (`runtime/context.py::_declared_effect`), so a toolset
    # registered on the process-global catalogue dispatched with no effect
    # class at all and reached the broker as an unclassified write — defeating,
    # one layer up, the lookup that exists to prevent exactly that. A method
    # added to the base class from here on chains by construction.

    def _first(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """The first catalogue tier with a non-``None`` answer."""
        for tier in self._catalogue_tiers():
            found = getattr(tier, method)(*args, **kwargs)
            if found is not None:
                return found
        return None

    def _owner(self, toolset_id: str) -> Any:
        """The first catalogue tier holding *toolset_id*, else the last tier.

        Reads that raise resolve the *toolset* first and then ask that tier,
        rather than trying each in turn: falling through on a missing operation
        would answer ``jira.issues.delete`` out of LOOM's Jira after a host had
        deliberately registered a narrower one under the same id. Shadowing has
        to be total to mean anything. Falling back to the last tier keeps the
        "not found" error identical to the one a lone catalogue raises.
        """
        tiers = self._catalogue_tiers()
        for tier in tiers:
            if tier.get(toolset_id) is not None:
                return tier
        return tiers[-1]

    def _merged(
        self, method: str, identity: Callable[[Any], Any], limit: int, *args: Any, **kwargs: Any
    ) -> list[Any]:
        """Every tier's matches, nearest tier first, de-duplicated by *identity*.

        Tier order rather than a merged ranking, because scores from two tiers
        are not comparable and because a locally registered toolset should win
        over the one LOOM ships under the same id. This is what the old
        two-tier ``search`` did; it is now what every merging read does.
        """
        out: list[Any] = []
        seen: set[Any] = set()
        for tier in self._catalogue_tiers():
            for item in getattr(tier, method)(*args, limit=limit, **kwargs):
                key = identity(item)
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        return out[:limit]

    # -- lookup ---------------------------------------------------------------

    def get(self, toolset_id: str) -> ToolsetManifest | None:
        manifest: ToolsetManifest | None = self._first("get", toolset_id)
        return manifest

    def effect_of(self, function: str) -> EffectClass | None:
        """As :meth:`ToolsetCatalog.effect_of`, continuing into the parent.

        Without the chain a step from an entry-point toolset — the ordinary way
        a first-party integration arrives — would resolve to nothing here, and
        its reads would go on reaching the broker classified as writes.

        **Execution scope, deliberately, where every read above is catalogue
        scope.** This one is the broker's per-dispatch lookup and its answer
        decides what a run is *allowed to do*, so widening it to the built-in
        tier would reclassify steps in deployments that registered no toolset
        at all. That is a policy change rather than a discovery one, and it
        belongs in its own change with its own tests.
        """
        for tier in self._execution_tiers():
            found = tier.effect_of(function)
            if found is not None:
                effect: EffectClass = found
                return effect
        return None

    def profile_of(self, function: str) -> Any:
        """As :meth:`ToolsetCatalog.profile_of`, continuing into the parent.

        Execution scope, for the reason :meth:`effect_of` gives — and this is
        the one that was actually being read. ``_declared_effect`` prefers
        ``profile_of`` and falls back to ``effect_of`` only when the attribute
        is *absent*, which it never is: so the chained ``effect_of`` above was
        unreachable from a Runtime, and every globally-registered toolset
        dispatched with no classification whatever.
        """
        for tier in self._execution_tiers():
            found = tier.profile_of(function)
            if found is not None:
                return found
        return None

    def manifest_of(self, function: str) -> ToolsetManifest | None:
        """Which toolset declares this ``@step``, across every catalogue tier.

        **Catalogue** scope, unlike :meth:`effect_of` beside it, and the
        difference is what each answer is *for*. That one is the broker's
        per-dispatch classification and decides what a run is allowed to do, so
        widening it changes policy. This one only decides what a *message* says
        when a call is about to fail anyway — so a bare Runtime running a
        generated workflow gets "jira is not connected" instead of "JIRA_URL is
        required", which is the case that produced the complaint.
        """
        found: ToolsetManifest | None = self._first("manifest_of", function)
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
        """Every **registered** toolset id, this registry's and its parent's.

        Execution scope: this is what :meth:`resolve_tools` sweeps when given
        no ids, so a toolset appearing here is one an unscoped
        ``ctx.agent("summarise this")`` acquires. The toolsets LOOM ships are
        deliberately absent — :meth:`catalogue_ids` is the other question.
        """
        ids = list(self._manifests)
        if self._parent is not None:
            ids += [i for i in self._parent.toolset_ids if i not in self._manifests]
        return ids

    def catalogue_ids(self) -> list[str]:
        """Every toolset that may be *named*: registered first, then built-in.

        What generated code may import, what the coding agent may browse, and
        what ``CodeValidator`` should accept. None of those is the same question
        as what a no-ids sweep acquires, and answering them with
        ``list_toolsets()`` is what made a bare ``Runtime`` reject
        ``from loom.toolsets.jira.tools import ...`` as an integration this
        environment does not have.
        """
        ids: list[str] = []
        seen: set[str] = set()
        for tier in self._catalogue_tiers():
            for toolset_id in tier.toolset_ids:
                if toolset_id in seen:
                    continue
                seen.add(toolset_id)
                ids.append(toolset_id)
        return ids

    @property
    def toolset_ids(self) -> list[str]:
        return self.list_toolsets()

    def search(self, query: str, *, limit: int = 10) -> list[Any]:
        """Search every catalogue tier, nearest tier first."""
        return self._merged("search", lambda card: card.toolset_id, limit, query)

    def search_operations(
        self, query: str, *, limit: int = 10, toolset_id: str | None = None
    ) -> list[Any]:
        """Search operations across every catalogue tier, nearest tier first."""
        return self._merged(
            "search_operations",
            lambda match: (match.toolset_id, match.op_id),
            limit,
            query,
            toolset_id=toolset_id,
        )

    def show(self, toolset_id: str, group: str | None = None) -> Any:
        return self._owner(toolset_id).show(toolset_id, group)

    def stub(self, op_path: str) -> Any:
        dot = op_path.find(".")
        if dot == -1:
            # A malformed path is the catalogue's own ValueError, and asking
            # every tier for it would replace a precise complaint with the last
            # tier's identical one.
            return super().stub(op_path)
        return self._owner(op_path[:dot]).stub(op_path)

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

    def prompt_block(self) -> str:
        """What a coding agent's system prompt should say exists. **One line
        per toolset**, over the whole catalogue.

        The counterpart of ``NodeRegistry.prompt_block()``, and separate from
        :meth:`describe` for two reasons that agree. Scope: this reads
        :meth:`catalogue_ids`, because a model may write against any toolset it
        can *name*, while ``describe()`` documents what is registered. And
        cost: an index card names every operation id — ~1,700 characters per
        toolset — so the 27 LOOM ships render to ~8,400 tokens against ~1,000
        for a roster. The difference that matters is the growth rate, ~430
        tokens per integration installed against ~20, paid on every turn of
        every job, to say something ``search_operations`` answers on demand and
        more precisely.

        Never empty, unlike ``describe()``: ``DEFAULT_SYSTEM_PROMPT`` goes on
        to say *"Only the toolsets listed above exist"*, and with nothing above
        it that sentence pointed at nothing at all — which is how a model asked
        for a Jira workflow spent thirty turns searching for a list it had been
        told was there. "None" is an answer; silence is not.
        """
        manifests = [m for tid in self.catalogue_ids() if (m := self.get(tid)) is not None]
        if not manifests:
            return (
                "## Available toolsets\n\n"
                "None. This process has no integrations registered, so there is "
                "nothing to import from loom.toolsets. Say what the task needs "
                "and that it is not available here, rather than writing code "
                "against an integration that does not exist."
            )
        return "\n".join(self._roster(manifests))

    def _roster(self, manifests: list[ToolsetManifest]) -> list[str]:
        """One line per toolset: what it is, and whether code can call it.

        **Exactly one line each**, so the block's cost is the number of
        integrations installed and nothing else.
        ``tests/test_toolset_discovery.py::TestThePromptBlock`` asserts that as
        line-count equality rather than a character budget — a budget is a
        ceiling somebody raises, and a count is a property. Same discipline
        ``NodeRegistry.prompt_block`` is pinned with.
        """
        lines = [
            "## Available toolsets",
            "",
            f"{len(manifests)} integrations are installed:",
            "",
        ]
        for m in manifests:
            # The module, because it is the one thing a model cannot derive and
            # will otherwise build out of the id: `google_calendar` lives at
            # `loom.toolsets.google.calendar`, and an import assembled from the
            # id resolves as a plausible path and fails at run time. Operation
            # *names* are deliberately not here — those are what show_toolset
            # and get_tool_contract are for, and they are what makes an index
            # card grow with the size of an integration rather than its
            # existence.
            #
            # A toolset with no module is reachable only through ctx.agent().
            # Saying which costs a few characters and is the difference between
            # generated code that imports and code that does not exist.
            module = m.tools_module or ""
            reach = f"  [{module}]" if module else "  [ctx.agent() only — not importable]"
            lines.append(f"  {m.id:<18}{m.summary}{reach}")
        lines += [
            "",
            "The name in brackets is the module to import from — use it exactly;",
            "one built from the toolset id resolves and then fails at run time.",
            "",
            "search_toolsets(query) and search_operations(query) find the right",
            "one. show_toolset(id) lists its operations; get_tool_contract(",
            'id + "." + op_id) gives the exact call to write, and get_tool_docs(id)',
            "has worked examples where they exist.",
            "",
            *PAGING_HOWTO,
        ]
        return lines

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

        **Execution scope**, and deliberately not the catalogue: this documents
        the toolsets somebody registered, which is what a host asking for its
        own docs means. What the *system prompt* should say exists is a
        different question with a different answer — see :meth:`prompt_block`.
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
                # the capability exists and to name it in a tool call; the
                # parameters and schemas are a tool call away and are the part
                # that would make this grow without bound.
                #
                # **Operation ids, never function names.** This line listed
                # `op.function`, so the block read:
                #
                #     Import: from loom.toolsets.jira.tools import ...,
                #             jira_list_projects, ...
                #     Operations: ..., jira_list_projects, ...
                #     ... then get_tool_contract("jira.<op_id>")
                #
                # A model asked to name an operation did exactly what the line
                # labelled "Operations" said and called
                # `call_read_operation("jira.jira_list_projects")`, which is not
                # an operation id and does not exist. It cost a turn to be told
                # so, on the first call of the run, every run — and the block
                # said it while also explaining, two lines above, that an
                # operation id looks like `messages.search`.
                #
                # The two lines are complementary now rather than duplicates:
                # Import gives the names generated *code* calls, Operations
                # gives the ids a *tool call* names. That also removes the
                # bridge-toolset special case — a bridge routes every operation
                # through one shared function, so `op.function` was useless
                # there and `op.id` was already the fallback.
                lines.append(
                    "  Operations (ids, for get_tool_contract / "
                    "call_read_operation): "
                    + ", ".join(op.id for op in m.all_operations())
                )
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


#: Words that mean the call destroys or withdraws something.
#:
#: Grown from the operations LOOM ships: the original list knew
#: ``delete/remove/drop/purge/revoke`` and missed ``archive``, ``trash``,
#: ``unshare`` and ``end`` — seven destructive operations that a read-only
#: grant would therefore have handed to an agent.
#:
#: ``clear`` joined them when the Sheets toolset arrived, and it is the same
#: shape: emptying a range is irreversible through the API, the word carries no
#: hint of that, and ``sheets_clear_range`` read as an ordinary write. That is
#: what ``tests/test_effect_guess.py`` is for — every new toolset scores the
#: heuristic against operations classified by hand, and a miss is a hard
#: failure rather than a tolerance, because under-classifying costs a deletion.
_DESTRUCTIVE_WORDS = (
    "delete", "remove", "drop", "purge", "revoke", "destroy", "wipe", "erase",
    "archive", "trash", "unshare", "unassign", "unpublish", "expire",
    "terminate", "cancel_subscription", "end_active", "close_account",
    "clear",
)

#: Words that mean the call changes something.
#:
#: Also grown from the corpus. ``upload``, ``reply``, ``move``, ``share``,
#: ``invite``, ``copy``, ``rename``, ``forward`` and a dozen more were all
#: reading as harmless.
_WRITE_WORDS = (
    "create", "update", "add", "transition", "assign", "upsert", "index",
    "send", "post", "put", "patch", "write", "set", "edit", "modify", "batch",
    "upload", "reply", "forward", "move", "copy", "rename", "share", "invite",
    "restore", "untrash", "unarchive", "respond", "append", "complete",
    "close", "cancel", "join", "leave", "schedule", "publish", "submit",
    "approve", "reject", "merge", "import", "sync", "start", "stop", "resume",
)


#: Words that positively indicate a read. Required rather than assumed: an
#: operation is only classified READ when its name says so.
_READ_WORDS = (
    "get", "list", "search", "find", "read", "fetch", "query", "describe",
    "show", "view", "download", "export", "lookup", "whoami", "check",
    "status", "count", "peek", "resolve", "recent", "changes", "history",
)


def _guess_effect(name: str) -> EffectClass | None:
    """Best-effort effect classification from an operation name, or ``None``.

    ``None`` means *the name carries no signal*, which is different from
    "this is a read". The distinction is the whole point: this used to return
    ``READ`` for anything it did not recognise, and READ is the one class
    exempt from every write and destructive control — so an unrecognised verb
    was not flagged, it was granted. Scored against the 320 operations LOOM
    ships, that fallback under-classified 14% of them, always toward more
    permitted.

    Callers resolve ``None`` to ``WRITE`` and say so in the log. A fallback
    that guesses cautiously costs an approval; one that guesses permissively
    costs a deletion.

    Still a fallback and not an authority: pass ``effects={...}`` to the
    factory whenever the name does not tell the truth about the call.
    """
    lowered = name.lower()
    if any(word in lowered for word in _DESTRUCTIVE_WORDS):
        return EffectClass.DESTRUCTIVE
    if any(word in lowered for word in _WRITE_WORDS):
        return EffectClass.WRITE
    if any(word in lowered for word in _READ_WORDS):
        return EffectClass.READ
    return None


def _resolve_guess(name: str) -> EffectClass:
    """The guess, or ``WRITE`` when the name says nothing — and a log line.

    The log matters more than it looks. A toolset built from callables whose
    names carry no verb gets every operation classified ``WRITE``, which is
    safe and also useless: ``resolve_tools(effects={READ})`` will hand an agent
    nothing. Saying which operation could not be classified is what turns that
    from a mystery into a one-line fix — ``effects={"scrape": ...}``.
    """
    guessed = _guess_effect(name)
    if guessed is not None:
        return guessed
    logger.info(
        "no effect class could be guessed from the name %r; defaulting to "
        "WRITE. Pass effects={%r: EffectClass...} to classify it.",
        name, name,
    )
    return EffectClass.WRITE



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
