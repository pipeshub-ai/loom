"""Discovery scope and execution scope are different questions.

`search_toolsets("jira")` answered nothing on a `loom author` run while
`toolsets=["jira"]` in the file it wrote resolved perfectly. The two are served
by one object, and only the resolving half had ever been given the built-in
tier — so a model could write correct code against an integration it was unable
to find, and the validator then rejected the import as an integration this
environment does not have.

Three things are pinned here, and the first is the one that must never break:

* **the scope boundary** — a registry that lets an unscoped `ctx.agent()`
  acquire `jira_delete_issue` has undone the reason the built-ins were left
  unregistered in the first place;
* **that chaining is structural** — `search_operations` and `profile_of` were
  added to `ToolsetCatalog` after `ToolsetRegistry`'s per-method parent
  overrides were written, got no override, and silently stopped at tier 0;
* **that shadowing is total** — a host registering its own `jira` must not have
  a missing operation answered out of LOOM's.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from loom.agents.tool_registry import Toolset, ToolsetRegistry
from loom.runtime.engine import Runtime
from loom.toolsets.catalog import ToolsetCatalog
from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.registry import (
    BuiltinToolsetCatalog,
    builtin_catalog,
    get_catalog,
    register_available_toolsets,
    register_toolset,
)


def _manifest(toolset_id: str = "acme", *, function: str = "acme_search") -> ToolsetManifest:
    return ToolsetManifest(
        id=toolset_id,
        version="1.0.0",
        summary=f"{toolset_id} — searching widgets and gizmos.",
        tools_module=f"tests.fake.{toolset_id}",
        groups={
            "widgets": [
                OperationSpec(
                    id="widgets.search",
                    summary="Search widgets.",
                    description="Search the widget catalogue.",
                    effect=EffectClass.READ,
                    function=function,
                )
            ]
        },
    )


def _toolset(manifest: ToolsetManifest) -> Toolset:
    return Toolset(manifest=manifest, _resolver=lambda op_id: f"tool:{manifest.id}.{op_id}")


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


class TestTheScopeBoundary:
    """What may be *named* is not what a sweep acquires.

    Stated three ways because a future change that collapses the two scopes
    fails the first of these, which is the cheap one.
    """

    def test_an_unscoped_agent_on_a_bare_runtime_still_gets_nothing(self) -> None:
        # The reason the built-ins were never registered: `resolve_tools(None)`
        # sweeps everything registered, so seeding them hands
        # `jira_delete_issue` to `ctx.agent("summarise this")`.
        assert Runtime().toolsets.resolve_tools() == []

    def test_naming_a_toolset_still_resolves_it(self) -> None:
        tools = Runtime().toolsets.resolve_tools(["jira"])
        assert tools, "toolsets=['jira'] must resolve on a process that registered nothing"

    def test_the_coding_agent_can_find_what_that_workflow_would_call(self) -> None:
        registry = Runtime().toolsets
        assert [card.toolset_id for card in registry.search("jira")] == ["jira"]
        assert "jira" in registry.catalogue_ids()
        assert registry.get("jira") is not None

    def test_execution_scope_stays_empty_while_the_catalogue_is_full(self) -> None:
        registry = Runtime().toolsets
        assert registry.list_toolsets() == []
        assert len(registry.catalogue_ids()) > 20

    def test_registering_moves_a_toolset_into_execution_scope(self) -> None:
        registry = ToolsetRegistry()
        registry.register(_toolset(_manifest()))
        assert registry.list_toolsets() == ["acme"]
        assert registry.resolve_tools() == ["tool:acme.widgets.search"]

    def test_the_builtin_tier_can_be_switched_off_entirely(self) -> None:
        # What a multi-tenant host passes so an unknown id cannot resolve to a
        # process-env credentialed builtin. It must close discovery too, or the
        # coding agent writes against toolsets that host will refuse to run.
        registry = ToolsetRegistry(allow_builtin_fallback=False)
        assert registry.get("jira") is None
        assert registry.search("jira") == []
        assert registry.catalogue_ids() == []
        with pytest.raises(KeyError):
            registry.show("jira")


# ---------------------------------------------------------------------------
# Chaining, structurally
# ---------------------------------------------------------------------------


#: Every read on :class:`ToolsetCatalog`, and how to prove it reaches a parent.
#:
#: A table rather than a stack of individual tests, because the defect it exists
#: for is a method that nobody remembered to chain — and a test somebody has to
#: remember to write has the same failure mode as the override it replaces.
#: ``test_every_catalogue_read_is_covered`` fails when a read is added to
#: ``ToolsetCatalog`` and not added here.
CHAINED_READS: dict[str, Any] = {
    "get": lambda r: r.get("acme") is not None,
    "manifest_of": lambda r: getattr(r.manifest_of("acme_search"), "id", "") == "acme",
    "search": lambda r: "acme" in [c.toolset_id for c in r.search("widgets")],
    "search_operations": lambda r: "widgets.search"
    in [m.op_id for m in r.search_operations("search widgets")],
    "show": lambda r: len(r.show("acme").ops) == 1,
    "stub": lambda r: r.stub("acme.widgets.search").op_id == "widgets.search",
    "effect_of": lambda r: r.effect_of("acme_search") is EffectClass.READ,
    "profile_of": lambda r: r.profile_of("acme_search") is not None,
    "toolset_ids": lambda r: "acme" in r.toolset_ids,
}

#: Members that are not chained reads, and why.
#:
#: ``register``/``unregister``/``invalidate`` write to one catalogue's own store
#: — a registration that reached a parent would be a registry leaking into its
#: neighbours. ``list_toolsets`` *is* chained to the parent, but its meaning is
#: execution scope; the table above covers it through ``toolset_ids``.
NOT_A_CHAINED_READ = {"register", "unregister", "invalidate", "list_toolsets"}


class TestChainingIsStructural:
    @pytest.fixture
    def chained(self) -> ToolsetRegistry:
        """An empty registry over a parent holding one toolset."""
        parent = ToolsetRegistry()
        parent.register(_toolset(_manifest()))
        return ToolsetRegistry(parent=parent)

    @pytest.mark.parametrize("name", sorted(CHAINED_READS))
    def test_read_reaches_the_parent(self, name: str, chained: ToolsetRegistry) -> None:
        assert CHAINED_READS[name](chained), f"{name}() stopped at tier 0"

    def test_a_nearer_tier_ranks_first(self, chained: ToolsetRegistry) -> None:
        """Tier order, not a merged ranking — scores from two tiers are not
        comparable, and a nearer registration should win."""
        matches = chained.search_operations("search widgets")
        assert matches[0].toolset_id == "acme"

    def test_every_catalogue_read_is_covered(self) -> None:
        """A read added to the base class must be added to the table above.

        `search_operations` and `profile_of` were both added to
        `ToolsetCatalog` after the parent-delegation overrides were written,
        and neither got one. `profile_of` is the one that mattered: it is read
        on every `ctx.step` via `runtime/context.py::_declared_effect`, which
        prefers it over `effect_of` and falls back only when the attribute is
        *absent* — so the chained `effect_of` was unreachable from a Runtime
        and every globally-registered toolset dispatched with no effect class,
        reaching the broker as an unclassified write.
        """
        public = {
            name
            for name, member in inspect.getmembers(ToolsetCatalog)
            if not name.startswith("_")
            and (inspect.isfunction(member) or isinstance(member, property))
        }
        uncovered = public - set(CHAINED_READS) - NOT_A_CHAINED_READ
        assert not uncovered, (
            f"{sorted(uncovered)} was added to ToolsetCatalog without a chaining "
            "test. Add it to CHAINED_READS, or to NOT_A_CHAINED_READ with a reason."
        )

    def test_profile_of_reaches_the_process_global_catalogue(self) -> None:
        # The live half of the defect, in the shape it actually occurred:
        # `loom mcp` seeds the global catalogue, and every Runtime in that
        # process then dispatched toolset steps unclassified.
        register_available_toolsets()
        profile = Runtime().toolsets.profile_of("jira_search_issues")
        assert profile is not None
        assert profile.effect is EffectClass.READ
        assert profile.open_world is True

    def test_effect_of_and_profile_of_stay_out_of_the_builtin_tier(self) -> None:
        """Execution scope, deliberately, unlike every other read.

        These decide what a run is *allowed to do*. Widening them to the
        built-in tier would reclassify steps in deployments that registered no
        toolset at all — a policy change, not a discovery one.
        """
        registry = Runtime().toolsets
        assert registry.get("jira") is not None  # discoverable
        assert registry.effect_of("jira_search_issues") is None
        assert registry.profile_of("jira_search_issues") is None


# ---------------------------------------------------------------------------
# The built-in tier
# ---------------------------------------------------------------------------


class TestTheBuiltinTier:
    def test_a_local_registration_shadows_a_shipped_toolset(self) -> None:
        registry = ToolsetRegistry()
        registry.register(_toolset(_manifest("jira", function="mine_search")))

        shadowing = registry.get("jira")
        assert shadowing is not None
        assert shadowing.summary.startswith("jira — searching")
        assert [op.id for op in registry.show("jira").ops] == ["widgets.search"]
        assert [c.toolset_id for c in registry.search("jira")] == ["jira"]

    def test_shadowing_is_total_rather_than_per_operation(self) -> None:
        """A missing operation must not be answered out of LOOM's Jira.

        The reason the raising reads resolve the *toolset* first and then ask
        that tier, instead of trying each tier until one does not raise: a host
        that deliberately registered a narrower `jira` would otherwise find
        `jira.issues.delete` working anyway.
        """
        registry = ToolsetRegistry()
        registry.register(_toolset(_manifest("jira", function="mine_search")))
        with pytest.raises(KeyError):
            registry.stub("jira.issues.delete")

    def test_the_tier_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            builtin_catalog().register(_manifest())
        with pytest.raises(TypeError):
            builtin_catalog().unregister("jira")

    def test_it_does_not_import_manifests_until_something_reads_it(self) -> None:
        # A Runtime holds one of these permanently. Loading on construction
        # would import 27 manifest modules for a process that never browses a
        # catalogue at all.
        fresh = BuiltinToolsetCatalog()
        assert fresh._loaded is False
        assert fresh.get("jira") is not None
        assert fresh._loaded is True

    def test_it_carries_manifests_and_never_reaches_a_tools_module(self) -> None:
        # Layer 1 stays Layer 1: no httpx, no vendor SDK, no credentials.
        catalog = BuiltinToolsetCatalog()
        manifest = catalog.get("jira")
        assert manifest is not None
        assert manifest.tools_module == "loom.toolsets.jira.tools"


class TestRegisterAvailableToolsets:
    def test_it_still_seeds_and_stays_idempotent(self) -> None:
        first = register_available_toolsets()
        second = register_available_toolsets()
        assert len(first) > 20
        assert first == second
        # Registered, not merely discoverable: this is what puts them in
        # execution scope for a surface whose job is to publish integrations.
        assert get_catalog().get_toolset("jira") is not None
        assert "jira" in get_catalog().list_toolsets()

    def test_it_does_not_overwrite_something_already_registered(self) -> None:
        register_toolset(_toolset(_manifest("jira", function="mine_search")))
        register_available_toolsets()
        mine = get_catalog().get("jira")
        assert mine is not None
        assert mine.summary.startswith("jira — searching")


# ---------------------------------------------------------------------------
# What the prompt carries
# ---------------------------------------------------------------------------


class TestThePromptBlock:
    """The roster is one line per toolset, and it is never silent.

    An index card names every operation id — ~1,700 characters per toolset — so
    the 27 LOOM ships render to roughly 8,400 tokens, on a method whose own
    docstring promises "~40 tokens each". That is a cost paid on every turn of
    every job to say something `search_operations` answers on demand and better.
    """

    def test_it_is_exactly_one_line_per_toolset(self) -> None:
        # Line-count equality, not a character budget: a budget is a ceiling
        # somebody raises, and a count is a property of the design.
        registry = ToolsetRegistry(allow_builtin_fallback=False)
        registry.register(_toolset(_manifest("one")))
        before = registry.prompt_block().splitlines()

        registry.register(_toolset(_manifest("two")))
        after = registry.prompt_block().splitlines()

        assert len(after) - len(before) == 1

    def test_the_shipped_catalogue_fits_a_prompt(self) -> None:
        registry = Runtime().toolsets
        roster = registry.prompt_block()
        # The same toolsets, rendered the way the prompt used to want them.
        # `describe()` is execution scope, so it must be asked explicitly —
        # comparing against its default would compare against "".
        index = registry.describe(registry.catalogue_ids(), detail="index")

        assert len(roster) // 4 < 1_500, "the roster stopped being O(one line)"
        assert len(roster) * 4 < len(index), "the roster is not buying anything"

    def test_every_shipped_toolset_is_named(self) -> None:
        registry = Runtime().toolsets
        roster = registry.prompt_block()
        for toolset_id in registry.catalogue_ids():
            assert f"  {toolset_id} " in roster or f"  {toolset_id:<18}" in roster

    def test_an_empty_catalogue_says_none_rather_than_nothing(self) -> None:
        """`DEFAULT_SYSTEM_PROMPT` says "Only the toolsets listed above exist".

        With `""` that sentence pointed at nothing at all, which is how a model
        asked for a Jira workflow spent thirty turns searching for a list it
        had been told was there.
        """
        empty = ToolsetRegistry(allow_builtin_fallback=False).prompt_block()
        assert empty != ""
        assert "None" in empty
        # `describe()` still answers "" — it documents what is registered, and
        # a host asking for its own docs is not the prompt.
        assert ToolsetRegistry(allow_builtin_fallback=False).describe() == ""

    def test_the_block_reads_catalogue_scope(self) -> None:
        # The bug in one line: this read `list_toolsets()`, so a bare Runtime
        # contributed no toolset block to the system prompt whatever.
        registry = Runtime().toolsets
        assert "jira" in registry.prompt_block()
        assert registry.describe() == "", "describe() stays execution scope"


class TestTheValidatorAllowlist:
    """A correct import must not be reported as a missing integration."""

    def test_a_shipped_toolset_import_is_accepted_on_a_bare_runtime(self) -> None:
        from loom.agents.coding_agent import _catalogue, _toolset_modules
        from loom.agents.validator import CodeValidator

        registry = Runtime().toolsets
        code = (
            "from loom import Context, workflow\n"
            "from loom.toolsets.jira.tools import jira_search_issues\n\n"
            "@workflow(name='overdue')\n"
            "async def overdue(ctx: Context, project: str) -> list:\n"
            "    return await ctx.step(jira_search_issues, project)\n"
        )
        validator = CodeValidator(
            available_toolsets=set(_catalogue(registry)),
            toolset_modules=_toolset_modules(registry),
        )
        assert [i for i in validator.validate(code) if i.category == "toolset"] == []

    def test_an_invented_toolset_is_still_refused(self) -> None:
        from loom.agents.coding_agent import _catalogue, _toolset_modules
        from loom.agents.validator import CodeValidator

        registry = Runtime().toolsets
        code = (
            "from loom import Context, workflow\n"
            "from loom.toolsets.pipedrive.tools import pipedrive_search\n\n"
            "@workflow(name='x')\n"
            "async def x(ctx: Context) -> None:\n"
            "    await ctx.step(pipedrive_search)\n"
        )
        validator = CodeValidator(
            available_toolsets=set(_catalogue(registry)),
            toolset_modules=_toolset_modules(registry),
        )
        issues = [i for i in validator.validate(code) if i.category == "toolset"]
        assert issues, "widening the allowlist must not disable the check"

    def test_a_registry_without_the_split_still_works(self) -> None:
        """A host may pass any object with a registry's shape.

        One written before `catalogue_ids()` existed has only `list_toolsets()`,
        and must keep the behaviour that shipped rather than being refused.
        """
        from loom.agents.coding_agent import _catalogue

        class Older:
            def list_toolsets(self) -> list[str]:
                return ["legacy"]

        assert _catalogue(Older()) == ["legacy"]
