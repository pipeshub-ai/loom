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
from loom.agents.probes.base import ObservedPage
from loom.agents.stages import (
    _SELECTOR_PATTERNS,
    BrowserEffectStage,
    ObservedTargetsStage,
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
        # Not `scope="durable"`. The prompt used to prescribe it for a flow that
        # parks on a person, and no provider LOOM ships advertises `reattach` —
        # so the advice had no working implementation behind it and produced
        # code that died at the first `browser.navigate` of a real run. Two
        # step-scoped sessions either side of the approval is what does work.
        'two `scope="step"` sessions',
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


# ---------------------------------------------------------------------------
# Targets weighed against what was actually observed
# ---------------------------------------------------------------------------

INTERSTITIAL = ObservedPage(
    target="https://book.test/reserve",
    landed="https://book.test/reserve/message",
    names=("TopNav Sidenav Button", "Confirm and continue"),
)
FORM = ObservedPage(
    target="https://book.test/reserve",
    landed="https://book.test/reserve",
    names=("2 Guests", "Select a time", "Find availability"),
)

BOOKING = """
from loom import Context, workflow
from loom.nodes.browser import ActIn, NavigateIn, TargetIn

@workflow
async def book(ctx: Context, n: int) -> str:
    await ctx.node("browser.navigate", NavigateIn(url="https://book.test/reserve"))
    await ctx.node("browser.act", ActIn(
        target=TargetIn(role="button", name="2 Guests"), method="click", effect="read"))
    await ctx.node("browser.act", ActIn(
        target=TargetIn(role="button", name="Select a time"), method="click", effect="read"))
    return "done"
"""


class TestTargetsAreWeighedAgainstWhatWasSeen:
    """The check that was missing: the census was produced and discarded.

    A workflow addressing controls that demonstrably were not on the page the
    agent looked at passed all sixteen stages, because nothing compared the two.
    """

    async def test_none_confirmed_is_called_out_as_a_different_page(self) -> None:
        result = await ObservedTargetsStage().run(
            BOOKING, CheckContext(observed=[INTERSTITIAL])
        )
        assert result.issues
        joined = " ".join(i.message for i in result.issues)
        assert "different pages" in joined
        # The redirect is named, because it is the likeliest explanation and the
        # agent has no other way to find it from here.
        assert "reserve/message" in joined

    async def test_it_never_errors(self) -> None:
        """Severity is the whole safety argument. `report.errors` drives the
        repair loop, and a control that only appears after an interaction is
        legitimately absent from a single-shot census — so an error here would
        set the model rewriting correct code."""
        result = await ObservedTargetsStage().run(
            BOOKING, CheckContext(observed=[INTERSTITIAL])
        )
        assert {i.severity for i in result.issues} == {"warning"}
        assert not [i for i in result.issues if i.severity == "error"]

    async def test_confirmed_targets_say_nothing(self) -> None:
        result = await ObservedTargetsStage().run(
            BOOKING, CheckContext(observed=[FORM])
        )
        assert not result.issues
        assert "2/2" in result.reason

    async def test_partial_coverage_is_reported_as_coverage(self) -> None:
        partial = ObservedPage(target="https://book.test/reserve",
                               landed="https://book.test/reserve",
                               names=("2 Guests",))
        result = await ObservedTargetsStage().run(
            BOOKING, CheckContext(observed=[partial])
        )
        assert result.issues
        joined = " ".join(i.message for i in result.issues)
        assert "only appears after an interaction" in joined
        assert "different pages" not in joined

    async def test_nothing_observed_is_a_skip_not_a_pass(self) -> None:
        """A check that could not run has found nothing — and must not be
        readable as having found nothing wrong."""
        result = await ObservedTargetsStage().run(BOOKING, CheckContext())
        assert result.skipped
        assert not result.issues

    async def test_a_non_browser_workflow_is_skipped(self) -> None:
        result = await ObservedTargetsStage().run(
            "from loom import workflow\n", CheckContext(observed=[INTERSTITIAL])
        )
        assert result.skipped

    async def test_a_dynamic_name_is_not_guessed_at(self) -> None:
        """An f-string target cannot be checked. Skipping it is correct;
        treating it as absent would be a finding invented out of ignorance."""
        code = BOOKING.replace('name="2 Guests"', 'name=f"{n} Guests"')
        result = await ObservedTargetsStage().run(
            code, CheckContext(observed=[FORM])
        )
        assert not result.issues

    async def test_a_redirect_is_reported_even_when_every_target_matches(self) -> None:
        """The case the real run produced: an agent that could not see the page
        stops using literal names at all and resolves everything from an intent,
        so target coverage comes back clean while the census still describes the
        wrong page. The hop is a fact, and it is reported on its own footing."""
        code = BOOKING.replace('name="2 Guests"', 'name="Confirm and continue"').replace(
            'name="Select a time"', 'name="TopNav Sidenav Button"')
        result = await ObservedTargetsStage().run(
            code, CheckContext(observed=[INTERSTITIAL])
        )
        joined = " ".join(i.message for i in result.issues)
        assert "redirected to" in joined
        assert "2/2" in result.reason

    async def test_navigating_straight_to_the_destination_is_silent(self) -> None:
        """A workflow that already accounts for the hop has nothing to learn
        from it, and repeating the warning would train the reader past it."""
        code = BOOKING.replace("https://book.test/reserve", "https://book.test/reserve/message")
        result = await ObservedTargetsStage().run(
            code, CheckContext(observed=[INTERSTITIAL, FORM])
        )
        assert "redirected to" not in " ".join(i.message for i in result.issues)


# ---------------------------------------------------------------------------
# Two stages that fired on correct code
# ---------------------------------------------------------------------------

MODEL_PAYLOAD = '''
from loom import Context, workflow
from loom.nodes.browser import ActIn, NavigateIn, TargetIn

@workflow
async def book(ctx: Context, n: int) -> str:
    """Click past the interstitial notice.

    Observed live: /reserve redirects to /reserve/message, a page carrying a
    policy notice with a #urgent .banner > button in front of the real form.
    """
    page = await ctx.node("browser.navigate", NavigateIn(url="https://b.test/x"))
    await ctx.node("browser.act", ActIn(
        session=page.session, method="click",
        target=TargetIn(role="button", name="Next"), effect="read"))
    return "done"
'''


class TestStagesDoNotFireOnCorrectCode:
    """The failure both of these had, and the one a stage must never have.

    `report.errors` drives the repair loop and unchanged code is how a model
    disagrees with a finding — so a stage that reports on correct code sets the
    loop rewriting working code to silence it.
    """

    async def test_an_effect_declared_on_a_model_payload_is_seen(self) -> None:
        """A node payload is written either as a dict literal or as its own
        input model. Reading only the dict reported every model-built call as
        undeclared — the same defect `_effect_arguments` carried one layer
        down, where it made `effect_by` dead for every node."""
        result = await BrowserEffectStage().run(MODEL_PAYLOAD, CheckContext())

        assert not [i for i in result.issues if "declare no `effect`" in i.message]

    async def test_a_dict_payload_still_works(self) -> None:
        """The shape that always worked, pinned so the new branch cannot
        replace it rather than join it."""
        code = MODEL_PAYLOAD.replace(
            'ActIn(\n        session=page.session, method="click",\n'
            '        target=TargetIn(role="button", name="Next"), effect="read")',
            '{"session": page.session, "method": "click", "effect": "read"}')
        result = await BrowserEffectStage().run(code, CheckContext())

        assert not [i for i in result.issues if "declare no `effect`" in i.message]

    async def test_an_undeclared_effect_is_still_caught(self) -> None:
        code = MODEL_PAYLOAD.replace(', effect="read"', "")
        result = await BrowserEffectStage().run(code, CheckContext())

        assert [i for i in result.issues if "declare no `effect`" in i.message]

    async def test_a_selector_in_a_docstring_is_not_a_selector(self) -> None:
        """`_string_literals` claimed to exclude docstrings and included every
        one, so a module explaining that a page redirects was reported as a CSS
        selector — the stage reading its own prose as data."""
        result = await SelectorStage().run(MODEL_PAYLOAD, CheckContext())

        assert not result.issues

    async def test_a_selector_in_real_code_is_still_caught(self) -> None:
        code = MODEL_PAYLOAD.replace('name="Next"', 'name="#urgent .banner > button"')
        result = await SelectorStage().run(code, CheckContext())

        assert result.issues
