"""Phase 13.4 — what the coding agent is told, and what checks its output.

Two stages, and each is asserted **in both directions**. The second half is the
one usually skipped and the one that matters more: a stage that fires on
correct code is worse than no stage, because the repair loop acts on
``report.errors`` and will rewrite working code to silence it.

Same discipline as ``tests/test_effect_gates_can_fail.py`` — hand the check the
defect it exists for, and then hand it something correct and require silence.
"""

from __future__ import annotations

import pytest

from loom.agents.checks import CheckContext
from loom.agents.stages import (
    _SELECTOR_PATTERNS,
    BrowserEffectStage,
    SelectorStage,
    default_stages,
)

CORRECT = '''
from loom import Context, workflow


@workflow(name="reserve")
async def reserve(ctx: Context, booking: dict) -> str:
    page = await ctx.node("browser.navigate",
                          {"url": booking["url"], "scope": "durable"})
    await ctx.node("browser.act", {
        "session": page.session,
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": booking["email"],
        "effect": "read"})
    await ctx.node("human.approval", {"subject": "booking"})
    await ctx.node("browser.act", {
        "session": page.session,
        "method": "click",
        "target": {"role": "button", "name": "Confirm booking"},
        "effect": "write"})
    await ctx.node("browser.close", {"session": page.session})
    return "booked"
'''

NO_BROWSER = '''
from loom import Context, step, workflow


@step
async def render(rows: list) -> str:
    return "\\n".join(f"- {r}" for r in rows)


@workflow(name="report")
async def report(ctx: Context, rows: list) -> str:
    return await ctx.step(render, rows)
'''


async def run_stage(stage, code: str, **kwargs):
    return await stage.run(code, CheckContext(**kwargs))


class TestSelectorStage:
    """E8, first half — and the silence half is asserted below it."""

    @pytest.mark.parametrize(
        "selector",
        [
            "div.card > button.primary",
            "//button[@id='submit']",
            "(//div[@class='row'])[2]",
            "[data-testid=confirm]",
            "li:nth-child(3)",
            "#main .row",
            "ul > li",
        ],
    )
    async def test_it_flags_a_selector(self, selector: str) -> None:
        code = f'''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {{"url": "https://x.test"}})
    await ctx.node("browser.act", {{"method": "click",
        "target": {{"role": "button", "css": {selector!r}}}, "effect": "read"}})
    return "ok"
'''
        result = await run_stage(SelectorStage(), code)
        assert result.issues, f"{selector!r} went unreported"
        assert "silently wrong later" in result.issues[0].message

    @pytest.mark.parametrize(
        "text",
        [
            "Confirm booking",
            "Search restaurants, cuisines, etc.",
            "the button that confirms the booking",
            "https://example.com/a/b",
            "someone@example.com",
            "Party size",
            "showing 200 of 312",
            "#urgent #billing",
            "Mr. Smith and Ms. Jones",
            "name > 5",
        ],
    )
    def test_it_stays_silent_on_ordinary_strings(self, text: str) -> None:
        """The half that decides whether anyone keeps the stage switched on.

        A check that fires on prose gets disabled, and then catches nothing at
        all — the reasoning the redaction denylist already follows.
        """
        import re

        assert not any(re.search(pattern, text)
                       for pattern, _ in _SELECTOR_PATTERNS), (
            f"{text!r} is ordinary text and would be reported as a selector")

    async def test_correct_code_is_clean(self) -> None:
        result = await run_stage(SelectorStage(), CORRECT)
        assert result.issues == []

    async def test_it_skips_a_workflow_with_no_browser_call(self) -> None:
        """A CSS string in a workflow that renders an email is not this mistake."""
        result = await run_stage(SelectorStage(), NO_BROWSER)
        assert result.skipped

    async def test_a_selector_the_spec_supplied_is_not_a_guess(self) -> None:
        code = '''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {"url": "https://x.test"})
    await ctx.node("browser.act", {"method": "click",
        "target": {"role": "button", "css": "div.card > button.primary"},
        "effect": "read"})
    return "ok"
'''
        result = await run_stage(
            SelectorStage(), code,
            spec="click the element at div.card > button.primary")
        assert result.issues == [], (
            "an address the caller supplied is knowledge, not a guess")


class TestBrowserEffectStage:
    async def test_it_flags_an_undeclared_effect(self) -> None:
        code = '''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {"url": "https://x.test"})
    await ctx.node("browser.act", {"method": "click",
        "target": {"role": "button", "name": "Go"}})
    return "ok"
'''
        result = await run_stage(BrowserEffectStage(), code)
        assert any("declare no `effect`" in i.message for i in result.issues)
        assert all(i.severity == "warning" for i in result.issues), (
            "unclassified code is correct, merely unclassified")

    async def test_a_write_with_nobody_asked_is_an_error(self) -> None:
        """Non-blocking, but an error on purpose.

        The repair loop reads ``report.errors``; a warning here is a finding
        nobody sees. Safe to escalate because unchanged code ends the repair —
        a model that judges it wrong says so by leaving the file alone.
        """
        code = '''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {"url": "https://x.test"})
    await ctx.node("browser.act", {"method": "click",
        "target": {"role": "button", "name": "Buy"}, "effect": "write"})
    return "ok"
'''
        result = await run_stage(BrowserEffectStage(), code)
        errors = [i for i in result.issues if i.severity == "error"]
        assert errors, "an unapproved browser write went unreported"
        assert "never asks a person" in errors[0].message

    @pytest.mark.parametrize("asking", [
        'await ctx.node("human.approval", {"subject": "buy"})',
        'await ctx.wait_for_approval("buy")',
        'await ctx.node("human.choice", {"subject": "buy", "options": ["a"]})',
    ])
    async def test_any_way_of_asking_counts(self, asking: str) -> None:
        code = f'''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {{"url": "https://x.test"}})
    {asking}
    await ctx.node("browser.act", {{"method": "click",
        "target": {{"role": "button", "name": "Buy"}}, "effect": "write"}})
    return "ok"
'''
        result = await run_stage(BrowserEffectStage(), code)
        assert [i for i in result.issues if i.severity == "error"] == []

    async def test_a_read_only_flow_needs_no_approval(self) -> None:
        code = '''
from loom import Context, workflow

@workflow(name="w")
async def w(ctx: Context, _i) -> str:
    await ctx.node("browser.navigate", {"url": "https://x.test"})
    text = await ctx.node("browser.extract", {})
    return text.text
'''
        result = await run_stage(BrowserEffectStage(), code)
        assert result.skipped, "no browser.act to classify"

    async def test_correct_code_is_clean(self) -> None:
        result = await run_stage(BrowserEffectStage(), CORRECT)
        assert result.issues == []

    async def test_it_skips_a_workflow_with_no_browser_call(self) -> None:
        result = await run_stage(BrowserEffectStage(), NO_BROWSER)
        assert result.skipped


class TestTheyAreInThePipeline:
    def test_both_stages_run_by_default(self) -> None:
        names = [s.name for s in default_stages(smoke=False)]
        assert "selectors" in names
        assert "browser-effect" in names

    def test_they_sit_after_the_cheap_structural_checks(self) -> None:
        """Cheapest-first is the pipeline's whole ordering rule."""
        stages = default_stages(smoke=False)
        by_name = {s.name: s.cost for s in stages}
        assert by_name["selectors"] > by_name["static"]
        assert by_name["browser-effect"] > by_name["compile"]


class TestSmokeRunsABrowserWorkflowOffline:
    def test_a_generated_browser_flow_runs_with_no_network(self) -> None:
        """E9. Without this the sandbox can only reach a connection error.

        And the cheapest repair a model can find for an error it cannot fix is
        to delete the browser work — shipping a workflow that passes every
        check having removed what the spec asked for. Exactly why
        ``AutoRespondChannel`` exists.
        """
        from loom.agents.smoke import smoke_run

        code = '''
from loom import Context, workflow


@workflow(name="read_a_page")
async def read_a_page(ctx: Context, url: str) -> str:
    page = await ctx.node("browser.navigate", {"url": url})
    await ctx.node("browser.act", {
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": "someone@example.com",
        "effect": "read"})
    found = await ctx.node("browser.extract", {})
    return f"{page.title}: {found.text[:40]}"
'''
        result = smoke_run(code, workflow_input="https://example.test/page")
        assert result.ok, result.error

    def test_the_same_flow_fails_with_no_provider_configured(self) -> None:
        """The other half of E9 — the fake must be doing something.

        A smoke pass proves nothing if the code would have passed anyway.
        """
        import asyncio

        from loom import Context, Runtime, workflow
        from loom.stores.memory import MemoryStore

        @workflow(name="no_provider_flow")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": "https://example.test"})
            return "ok"

        result = asyncio.run(Runtime(store=MemoryStore()).run(flow, None))
        assert result.status.value == "failed"
        assert result.error and "browser" in result.error.message.lower()


class TestThePromptSaysTheRules:
    """The stages catch what the prompt failed to prevent.

    Asserted as content rather than left to a reader, because a rule the model
    is never told is one the repair loop teaches it the expensive way.
    """

    @pytest.mark.parametrize("phrase", [
        "Two matches raise",
        "ordinal",
        "matches nothing, silently",
        "cannot be inferred from the target",
        'scope="durable"',
        "human.approval",
        "browser.close",
    ])
    def test_the_browser_block_covers(self, phrase: str) -> None:
        """Only what nothing else tells the agent.

        The block is deliberately 681 characters — see
        ``test_the_prompt_stays_lean`` for what was cut and why. A worked
        example is *not* here on purpose: ``node_contract`` renders the call
        from the node's own models, so a copy in the prompt is a second source
        that can drift while the tool cannot.
        """
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert phrase in " ".join(DEFAULT_SYSTEM_PROMPT.split())

    def test_the_discover_step_names_the_browser_as_an_exit(self) -> None:
        """Step 1 was wrong without it.

        It named plain Python as the *only* answer when no toolset matches,
        which is no longer true — and saying "use a browser when nothing else
        covers it" in the browser section too would be the same sentence twice.
        """
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        # Whitespace-normalised: the prompt is a wrapped block, so an exact
        # match asserts where the lines happen to break rather than what it
        # says — and breaks the moment anyone rewraps the paragraph.
        flat = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "plain Python in a `@step`, or drive the page" in flat
