"""Driving a page while writing code against it.

The failure this closes, observed end to end: a reservation URL redirected to a
policy notice, and the entire booking application sat behind one click on it.
The probe reported the notice accurately and confidently; the coding agent wrote
a workflow against controls it had never seen. Twenty-eight observations could
not have found them, because looking cannot click.

The property under test throughout is the one that makes that safe to fix:
exploration can reveal controls and cannot supply data.
"""

from __future__ import annotations

import json

import pytest

from loom.agents.coding_tools import build_coding_tools, make_exploration_tools
from loom.agents.probes.exploration import (
    EXPLORATORY_METHODS,
    BrowserExploration,
    ExplorationRefused,
    click,
)
from loom.browser import FakeBrowserProvider
from loom.browser.base import (
    ActionMethod,
    ActionPlan,
    BrowserPolicy,
    SessionScope,
    Target,
)
from loom.testing.conformance import verify_exploration_session

URL = "https://book.test/reserve"
_STEP_POLICY = BrowserPolicy(scope=SessionScope.STEP)


@pytest.fixture
def exploring():
    return BrowserExploration(FakeBrowserProvider(permissive=True))


class TestTheContract:
    async def test_the_reference_session_conforms(self, exploring) -> None:
        """Including the part an author would not write a test for: that it
        cannot type. The code obviously does not fill anything in, right up
        until a convenience overload means it does."""
        await verify_exploration_session(exploring, url=URL)


class TestItCannotSupplyData:
    """The load-bearing layer. Every write worth worrying about — a booking, a
    signup, a purchase, a message — needs data supplied; panels, calendars and
    tabs need none. So the allowlist is what makes this read-only, not care.
    """

    @pytest.mark.parametrize("method", [ActionMethod.FILL, ActionMethod.PRESS])
    async def test_typing_and_keys_are_refused(self, exploring, method) -> None:
        await exploring.open(URL)
        with pytest.raises(ExplorationRefused, match="cannot"):
            await exploring.act(ActionPlan(
                method=method,
                target=Target(role="textbox", name="Email", exact=True),
                value="someone@example.com",
            ))

    async def test_the_allowlist_is_stated_positively(self) -> None:
        """A new ActionMethod must be refused until somebody adds it
        deliberately. A denylist would admit each one by default, which is the
        wrong direction for a list deciding what a model may do."""
        assert ActionMethod.FILL not in EXPLORATORY_METHODS
        assert ActionMethod.PRESS not in EXPLORATORY_METHODS
        assert ActionMethod.CLICK in EXPLORATORY_METHODS

    async def test_a_refusal_is_not_recorded_as_work(self, exploring) -> None:
        await exploring.open(URL)
        with pytest.raises(ExplorationRefused):
            await exploring.act(ActionPlan(
                method=ActionMethod.FILL,
                target=Target(role="textbox", name="Email", exact=True),
                value="x",
            ))
        assert exploring.trace() == ()


class TestItIsBounded:
    async def test_the_budget_stops_it(self) -> None:
        """An agent that cannot find a control does not stop looking; it looks
        differently, repeatedly. Twenty-eight times, in the run this comes
        from — and clicks cost more than looks in every sense."""
        exploring = BrowserExploration(FakeBrowserProvider(permissive=True),
                                       max_actions=2)
        await exploring.open(URL)
        for _ in range(2):
            await exploring.act(click("button", "Confirm and continue"))
        with pytest.raises(ExplorationRefused, match="budget"):
            await exploring.act(click("button", "Confirm and continue"))

    async def test_acting_before_opening_is_refused(self, exploring) -> None:
        with pytest.raises(ExplorationRefused, match="no page is open"):
            await exploring.act(click("button", "Next"))


class _RecordingProvider:
    """Wraps the fake and keeps the policy it was opened with."""

    def __init__(self) -> None:
        self._inner = FakeBrowserProvider(permissive=True)
        self.policies: list[object] = []

    @property
    def id(self) -> str:
        return "recording"

    def supports(self) -> frozenset[str]:
        return self._inner.supports()

    async def open(self, policy):
        self.policies.append(policy)
        return await self._inner.open(policy)


class TestItIsAnonymous:
    async def test_no_storage_state_is_carried(self) -> None:
        """The layer that removes the one write needing no typing: a purchase
        completed from saved details. An anonymous session has none.

        The policy is built inside `open` rather than accepted from a caller
        precisely so this cannot be arranged away — a caller who could pass a
        policy could pass an authenticated one."""
        provider = _RecordingProvider()
        await BrowserExploration(provider).open(URL)

        assert provider.policies[-1].storage_state is None
        assert provider.policies[-1].scope is SessionScope.STEP


class TestTheTrace:
    async def test_it_records_what_was_done(self, exploring) -> None:
        await exploring.open(URL)
        await exploring.act(click("button", "Confirm and continue"))
        trace = exploring.trace()
        assert [p.method for p in trace] == [ActionMethod.CLICK]
        assert trace[0].target.name == "Confirm and continue"


class TestTheToolsAreOfferedOnlyWhenTheyWork:
    """The rule `observe_target` and `ask_user` already follow. A tool that is
    present and always answers "not configured" spends context every turn to
    say nothing, and teaches the model to distrust the capability."""

    def test_absent_without_a_provider(self) -> None:
        names = {t.name for t in build_coding_tools()}
        assert "open_page" not in names
        assert "interact" not in names

    def test_present_with_one(self, exploring) -> None:
        names = {t.name for t in build_coding_tools(exploration=exploring)}
        assert {"open_page", "interact"} <= names


class TestItIsWired:
    """`interaction.py` shipped complete, fully unit-tested, and unreachable
    because no caller passed it in. A module's own tests cannot catch that —
    they construct the thing themselves — so this tests the seam."""

    def test_the_facade_passes_the_runtimes_browser(self) -> None:
        import inspect

        from loom.facade import LocalFacade

        source = inspect.getsource(LocalFacade._coding_agent)
        assert "browser=self.runtime.browser" in source

    def test_the_agent_closes_the_session_however_it_returns(self) -> None:
        import inspect

        from loom.agents.coding_agent import WorkflowCodingAgent

        source = inspect.getsource(WorkflowCodingAgent.generate)
        assert "finally" in source
        assert "_close_exploration" in source


class TestTheToolsReport:
    async def test_open_page_names_where_it_landed(self, exploring) -> None:
        import json

        tools = {t.name: t for t in make_exploration_tools(exploring)}
        payload = json.loads(await tools["open_page"].fn(URL))
        assert "landed" in payload
        assert isinstance(payload["controls"], list)

    async def test_a_refusal_comes_back_as_a_refusal(self, exploring) -> None:
        """Not an error. A model told something is wrong tries to fix it, and
        there is nothing here to fix."""
        import json

        tools = {t.name: t for t in make_exploration_tools(exploring)}
        await tools["open_page"].fn(URL)
        exploring.max_actions = 0
        payload = json.loads(await tools["interact"].fn("button", "Next"))
        assert "refused" in payload
        assert "error" not in payload


# ---------------------------------------------------------------------------
# The trace as the smoke fixture
# ---------------------------------------------------------------------------


class TestTheRecordingReplays:
    """Smoke otherwise answers every browser action as though it worked.

    That proves the flow is *wired* and says nothing about whether a single
    control exists — and it is the reason a workflow addressing a party size
    control on a page that had none passed every stage. A recording turns the
    sandbox into a run against the pages the agent actually drove, including
    the ones that only appear after a click.
    """

    async def test_a_click_advances_to_what_followed_it(self, exploring) -> None:
        """The half a census cannot reach. `pages` is keyed by URL, so without
        transitions a replay stays on the first page forever and every control
        revealed by a click is missing."""
        from loom.browser.base import PageSnapshot, TreeNode

        notice = PageSnapshot(url=URL, title="Notice", tree=(
            TreeNode(role="button", name="Confirm and continue"),))
        form = PageSnapshot(url=URL + "/landing", title="Book", tree=(
            TreeNode(role="button", name="2 Guests"),))

        provider = FakeBrowserProvider(
            pages={URL: notice},
            transitions={
                Target(role="button", name="Confirm and continue",
                       exact=True).describe(): form
            },
            permissive=False,
        )
        session = await provider.open(_STEP_POLICY)
        await session.navigate(URL)
        assert [c.name for c in (await session.snapshot()).controls()] == [
            "Confirm and continue"]

        await session.perform(click("button", "Confirm and continue"))
        assert [c.name for c in (await session.snapshot()).controls()] == ["2 Guests"]

    async def test_a_control_that_was_never_there_is_caught(self) -> None:
        """The finding this exists for."""
        from loom.browser.base import PageSnapshot, TreeNode
        from loom.browser.errors import TargetNotFound

        recorded = PageSnapshot(url=URL, tree=(
            TreeNode(role="button", name="Confirm and continue"),))
        provider = FakeBrowserProvider(pages={URL: recorded}, permissive=True)
        session = await provider.open(_STEP_POLICY)
        await session.navigate(URL)

        with pytest.raises(TargetNotFound):
            await session.perform(click("button", "Party size"))

    async def test_a_control_seen_elsewhere_is_not_a_failure(self) -> None:
        """A replay taking the steps in a different order than the recording is
        at a different point in the flow, not looking at a missing control.
        Calling that a failure would fail correct workflows, and smoke blocks."""
        from loom.browser.base import PageSnapshot, TreeNode

        recorded = PageSnapshot(url=URL, tree=(
            TreeNode(role="button", name="Confirm and continue"),))
        provider = FakeBrowserProvider(
            pages={URL: recorded}, permissive=True,
            known_names=frozenset({"2 Guests"}),
        )
        session = await provider.open(_STEP_POLICY)
        await session.navigate(URL)

        result = await session.perform(click("button", "2 Guests"))
        assert result.ok
        assert "not on this page" in result.detail

    async def test_an_unexplored_url_still_answers(self) -> None:
        """The permissive fallback, and it is load-bearing: a page nobody
        explored must not fail a workflow that is perfectly correct — the
        reasoning AutoRespondChannel is built on."""
        from loom.browser.base import PageSnapshot

        provider = FakeBrowserProvider(pages={URL: PageSnapshot(url=URL)},
                                       permissive=True)
        session = await provider.open(_STEP_POLICY)
        page = await session.navigate("https://elsewhere.test/other")
        assert page.url == "https://elsewhere.test/other"

    async def test_the_recording_round_trips_through_a_subprocess(self, exploring) -> None:
        """It crosses a process boundary as JSON, so the shape has to survive."""
        from loom.agents.probes.exploration import recording_from_dict

        await exploring.open(URL)
        await exploring.act(click("button", "Confirm and continue"))
        rebuilt = recording_from_dict(json.loads(json.dumps(
            exploring.recording().as_dict())))

        assert URL in rebuilt.pages
        assert rebuilt.transitions


class TestOneCallCanRevealSeveralLayers:
    """Turns and actions are different currencies, and only the second was
    capped. An agent revealing a form four panels deep spent four model round
    trips doing it; observed runs put ~40% of the whole turn budget into
    exploring, then ran out before writing any code.
    """

    async def test_click_through_reports_each_state(self, exploring) -> None:
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        payload = json.loads(await tools["open_page"].fn(
            URL, ["Confirm and continue", "2 Guests"]))

        assert [s["clicked"] for s in payload["steps"]] == [
            "Confirm and continue", "2 Guests"]
        assert len(exploring.trace()) == 2

    async def test_the_action_budget_still_bounds_it(self) -> None:
        """Safety is unchanged: several clicks per turn, the same ceiling on
        clicks. Otherwise this would be a way to buy more actions by batching
        them, which is the loophole `AskUserGate` counts questions to avoid."""
        exploring = BrowserExploration(FakeBrowserProvider(permissive=True),
                                       max_actions=1)
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        payload = json.loads(await tools["open_page"].fn(
            URL, ["Confirm and continue", "2 Guests", "Select a time"]))

        assert "refused" in payload["steps"][-1]
        assert len(exploring.trace()) == 1

    async def test_it_stops_at_the_first_failure(self, exploring) -> None:
        """A click that did not happen makes every later one meaningless — the
        page is not where the sequence assumed it would be."""
        exploring.provider = FakeBrowserProvider(
            pages={URL: __import__("loom.browser.base", fromlist=["x"]).PageSnapshot(
                url=URL)}, permissive=False)
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        payload = json.loads(await tools["open_page"].fn(URL, ["Nope", "Also nope"]))

        assert len(payload.get("steps", [])) == 1

    async def test_no_clicks_behaves_exactly_as_before(self, exploring) -> None:
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        payload = json.loads(await tools["open_page"].fn(URL))

        assert "steps" not in payload
        assert exploring.trace() == ()


class TestARepeatedNameNeedsAnOrdinal:
    """Two controls with one name is exactly where picking one is a guess.

    `AmbiguousTarget` refuses for that reason, and batching must not quietly
    undo it — so the ordinal is stated rather than inferred.
    """

    async def test_the_suffix_selects_deliberately(self, exploring) -> None:
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        await tools["open_page"].fn(URL, ["Confirm and continue#1"])

        plan = exploring.trace()[0]
        assert plan.target.name == "Confirm and continue"
        assert plan.target.ordinal == 1

    async def test_a_name_that_merely_contains_a_hash_is_untouched(
        self, exploring
    ) -> None:
        """A control genuinely called "#1 Best Seller" is a name, not an
        ordinal. Only a trailing run of digits with something in front of it
        counts."""
        tools = {t.name: t for t in make_exploration_tools(exploring)}
        await tools["open_page"].fn(URL, ["#1 Best Seller"])

        plan = exploring.trace()[0]
        assert plan.target.name == "#1 Best Seller"
        assert plan.target.ordinal == 0
