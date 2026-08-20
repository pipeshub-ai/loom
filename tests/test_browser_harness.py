"""The browser conformance kit, driven by the defects it exists to catch.

``verify_browser_session`` runs against providers that are all correct on day
one, so on day one it passes whether or not it works. That is exactly the state
CERT-04 spent its whole life in — it claimed to require an explicit effect
class, was ``if not op.effect`` against a truthy ``StrEnum``, and could never
fail.

So each mutant below is a real way an adapter can look correct and lose a run,
and the kit must reject every one. Same shape as
``tests/conformance/test_harness.py`` for stores and
``tests/test_effect_gates_can_fail.py`` for effect gates.

Deliberately in-process and synthetic: mutating the shipped provider from a test
would leave the tree broken when one fails.
"""

from __future__ import annotations

import pytest

from loom.browser import (
    ActionPlan,
    BrowserPolicy,
    FakeBrowserProvider,
    FakeBrowserSession,
    PageSnapshot,
    SessionHandle,
    Target,
    TreeNode,
)
from loom.testing.conformance import verify_browser_session

PAGE = PageSnapshot(
    url="https://fixture.test/form",
    title="Fixture",
    tree=(
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="button", name="Submit"),
        TreeNode(role="button", name="Notify"),
        TreeNode(role="button", name="Notify"),
    ),
    text="fixture",
)

UNIQUE = Target(role="textbox", name="Email")
REPEATED = Target(role="button", name="Notify")


def good() -> FakeBrowserProvider:
    return FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)


async def _verify(provider) -> None:
    await verify_browser_session(
        provider, url=PAGE.url, known=UNIQUE, repeated=REPEATED)


class TestTheKitPassesACorrectProvider:
    async def test_fake_provider_conforms(self) -> None:
        """The baseline. Without this, every assertion below is vacuous."""
        await _verify(good())


class TestTheKitCatchesEachDefect:
    """E7. One class of adapter bug per test. Each must be rejected."""

    async def test_it_catches_a_non_strict_locate(self) -> None:
        """Picking the first of several matches instead of refusing.

        The most valuable single check here. An automation that resolves an
        ambiguous target is one that eventually clicks the wrong button and
        reports success — the failure mode with no error attached to it.
        """
        class PicksFirst(FakeBrowserSession):
            async def perform(self, plan: ActionPlan):
                found = self._matches(plan.target)
                if found:  # takes the first, never raises
                    self.performed.append(plan)
                    from loom.browser import ActResult
                    return ActResult(ok=True, method=plan.method,
                                     target=plan.target.describe(),
                                     url=self._current.url)
                return await super().perform(plan)

        provider = good()
        provider.open = _opening(provider, PicksFirst)  # type: ignore[method-assign]
        with pytest.raises(AssertionError, match="instead of raising"):
            await _verify(provider)

    async def test_it_catches_a_session_that_survives_close(self) -> None:
        """A closed session that keeps answering still holds cookies."""
        class NeverCloses(FakeBrowserSession):
            async def close(self) -> None:
                pass  # the caller believes this released the browser

        provider = good()
        provider.open = _opening(provider, NeverCloses)  # type: ignore[method-assign]
        with pytest.raises(AssertionError, match="after close"):
            await _verify(provider)

    async def test_it_catches_snapshot_that_navigates(self) -> None:
        """Reading a page must not move it.

        An adapter that re-navigates inside ``snapshot`` silently discards
        whatever the flow had typed, and every later step fails somewhere far
        from the cause.
        """
        class SnapshotMoves(FakeBrowserSession):
            async def snapshot(self, *, vision: bool = False):
                return PageSnapshot(url="https://fixture.test/elsewhere")

        provider = good()
        provider.open = _opening(provider, SnapshotMoves)  # type: ignore[method-assign]
        with pytest.raises(AssertionError, match="moved the page"):
            await _verify(provider)

    async def test_it_catches_reattach_opening_a_fresh_session(self) -> None:
        """The failure that looks like success.

        A provider claiming ``reattach`` and quietly returning a *new* session
        loses everything the run had done, while every happy-path test still
        passes. A two-hour approval resumes against a blank browser.
        """
        class LiesAboutReattach(FakeBrowserProvider):
            def supports(self) -> frozenset[str]:
                return frozenset({"reattach"})

            async def reattach(self, handle: SessionHandle):
                return await self.open(BrowserPolicy())  # a *different* session

        with pytest.raises(AssertionError, match="a \\*different\\* session"):
            await _verify(LiesAboutReattach({PAGE.url: PAGE}, permissive=False))

    async def test_it_catches_reattach_that_works_but_is_undeclared(self) -> None:
        """``supports()`` has to be honest in both directions.

        A host reads it to decide whether a durable scope is safe, so a
        capability that works but is not declared is as misleading as one
        declared and absent.
        """
        class UndeclaredReattach(FakeBrowserProvider):
            async def reattach(self, handle: SessionHandle):
                return await self.open(BrowserPolicy())

        with pytest.raises(AssertionError, match="supports\\(\\) omits"):
            await _verify(UndeclaredReattach({PAGE.url: PAGE}, permissive=False))

    async def test_it_catches_a_provider_with_no_id(self) -> None:
        provider = good()
        provider.id = ""
        with pytest.raises(AssertionError, match="non-empty string id"):
            await _verify(provider)

    async def test_it_catches_supports_returning_the_wrong_type(self) -> None:
        class ListSupports(FakeBrowserProvider):
            def supports(self):  # type: ignore[override]
                return ["storage_state"]

        with pytest.raises(AssertionError, match="frozenset"):
            await _verify(ListSupports({PAGE.url: PAGE}, permissive=False))

    async def test_it_catches_locate_matching_a_control_that_cannot_exist(self) -> None:
        """A locator that matches everything resolves nothing.

        The mirror of non-strict matching, and it fails in the opposite
        direction: every target "resolves", to whatever happened to be first.
        """
        class MatchesAnything(FakeBrowserSession):
            async def locate(self, target: Target) -> int:
                return 1

        provider = good()
        provider.open = _opening(provider, MatchesAnything)  # type: ignore[method-assign]
        with pytest.raises(AssertionError, match="cannot exist"):
            await _verify(provider)


def _opening(provider: FakeBrowserProvider, session_class):
    """Bind *session_class* as what this provider opens."""
    async def open_(policy: BrowserPolicy):
        session = session_class(
            provider.id, {PAGE.url: PAGE}, False,
            f"mutant-{len(provider.sessions)}")
        provider.sessions.append(session)
        return session
    return open_
