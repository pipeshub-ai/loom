"""Looking up LOOM's own API.

Every construct the coding agent is told to use had a tool behind it except the
SDK itself. Told to use `@pure`, the agent searched the *toolset* catalogue for
it — `search_operations("pure decorator loom import")`, `get_tool_docs("slack")`
— because those were the only tools it had, spent three to five turns per run
doing it, and then wrote a workflow with no `@step` at all, which the validator
flagged. Told to do something, unable to look it up, then reported for not
doing it.

The property under test throughout: every line is rendered from the object, so
it cannot disagree with the installed version.
"""

from __future__ import annotations

import json

import pytest

from loom.agents.coding_tools import build_coding_tools
from loom.agents.sdk_reference import (
    context_methods,
    sdk_contract,
    sdk_symbols,
)


class TestItAnswersWhatSentTheAgentSearching:
    def test_pure_renders_its_form(self) -> None:
        contract = sdk_contract("pure")

        assert contract.kind == "decorator"
        assert contract.import_line == "from loom import pure"
        # The substantive part: a signature describes a call, and the agent's
        # next action is to write one.
        assert "@pure" in contract.usage
        assert "async def" in contract.usage

    def test_a_leading_at_is_accepted(self) -> None:
        """It is how the prompt writes it and how anybody would type it."""
        assert sdk_contract("@effect").symbol == "effect"

    def test_a_context_method_resolves_with_or_without_the_prefix(self) -> None:
        assert sdk_contract("ctx.now").usage.startswith("ctx.now(")
        assert sdk_contract("now").symbol == "ctx.now"

    def test_await_comes_from_the_code(self) -> None:
        """Not from a list of which methods are async — one more thing that
        would need maintaining beside the thing it describes."""
        assert sdk_contract("ctx.gather").usage.startswith("await ")
        assert not sdk_contract("ctx.now").usage.startswith("await ")


class TestItIsDerivedNotWrittenDown:
    """`node_contract` renders from the node's own models for this reason: a
    second copy of a call's shape drifts while the code cannot."""

    def test_every_published_symbol_renders(self) -> None:
        for symbol in sdk_symbols():
            assert sdk_contract(symbol).usage

    def test_every_context_method_renders(self) -> None:
        for method in context_methods():
            assert sdk_contract(f"ctx.{method}").usage

    def test_the_surface_is_loom_s_own_all(self) -> None:
        """A list kept here would be one to update when the package changes."""
        import loom

        assert set(sdk_symbols()) == set(loom.__all__)

    def test_options_carry_real_defaults(self) -> None:
        assert any(o.startswith("max_attempts=") for o in sdk_contract("step").options
                   ) or "retry=None" in sdk_contract("step").options


class TestAWorkflowBodyIsAsync:
    """`@workflow` takes a `WorkflowFn`, which says nothing on its face and
    resolves to `Callable[..., Awaitable[Any]]`. Read literally it renders as
    `def`, and a caller who copies that gets a coroutine nobody awaits."""

    def test_the_alias_is_followed(self) -> None:
        assert "async def" in sdk_contract("workflow").usage

    def test_so_are_the_direct_annotations(self) -> None:
        for name in ("pure", "step", "effect", "node"):
            assert "async def" in sdk_contract(name).usage


class TestAnUnknownNameSuggests:
    """The behaviour `CodeValidator` already has for an import: a misspelling
    is the common case and the correction is cheap to offer."""

    def test_a_near_miss_is_named(self) -> None:
        with pytest.raises(KeyError, match="Retry"):
            sdk_contract("Retryy")

    def test_something_unrelated_still_refuses_cleanly(self) -> None:
        with pytest.raises(KeyError, match="no LOOM symbol"):
            sdk_contract("jira_search_issues")


class TestTheToolIsAlwaysThere:
    """Unlike probes or exploration, this needs no registry, no browser and no
    network — it reads the installed package. There is no configuration under
    which it should be absent."""

    def test_it_is_offered_with_no_registry(self) -> None:
        assert "sdk_contract" in {t.name for t in build_coding_tools()}

    async def test_it_returns_json_a_model_can_use(self) -> None:
        tool = {t.name: t for t in build_coding_tools()}["sdk_contract"]
        payload = json.loads(await tool.fn("@pure"))

        assert payload["import"] == "from loom import pure"
        assert "@pure" in payload["usage"]

    async def test_an_error_is_a_payload_not_a_raise(self) -> None:
        """A raise aborts the model's turn; the agent needs to read this and
        try another name."""
        tool = {t.name: t for t in build_coding_tools()}["sdk_contract"]
        payload = json.loads(await tool.fn("Retryy"))

        assert "Retry" in payload["error"]
