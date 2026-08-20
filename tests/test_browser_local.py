"""``LocalBrowserProvider`` against real page structure, offline.

The corpus exists to answer whether tier 0 reaches real controls. This is where
that answer stops being a measurement and becomes the shipped resolver: the same
frozen pages, driven by ``LocalBrowserProvider`` through a real Chromium, served
from localhost so nothing touches the network.

Skips without the ``[browser]`` extra, which is the right degradation — a check
that cannot run has found nothing.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright", reason="needs the [browser] extra")

from loom.browser import (
    ActionMethod,
    ActionPlan,
    BrowserPolicy,
    LocalBrowserProvider,
    SessionScope,
    Target,
)
from loom.browser.errors import BrowserUnavailable, SessionLost
from loom.testing.conformance import verify_browser_session

PAGES = Path(__file__).parent / "corpus" / "pages"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def corpus_url():
    """Serves the frozen corpus on localhost, or skips.

    ``verify_probe``'s rule, one layer out: point it at a fixture you control,
    so a red test means this provider broke rather than someone else's server.
    """
    if not any(PAGES.glob("*.html")):
        pytest.skip("no corpus pages captured")
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_Quiet, directory=str(PAGES)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield lambda slug: f"http://127.0.0.1:{port}/{slug}.html"
    finally:
        httpd.shutdown()
        httpd.server_close()


def has_page(slug: str) -> bool:
    return (PAGES / f"{slug}.html").exists()


async def open_page(url: str):
    """A live session on *url*. The provider is discarded: a local browser
    dies with its session, so nothing needs it back."""
    session = await LocalBrowserProvider().open(
        BrowserPolicy(scope=SessionScope.STEP))
    await session.navigate(url)
    return session


class TestConformance:
    async def test_the_shipped_provider_conforms(self, corpus_url) -> None:
        """The kit against the real thing.

        ``test_browser_harness`` proves the kit rejects broken adapters; this
        proves the one LOOM ships is not one of them. Neither is worth much
        alone.
        """
        if not has_page("gitlab-signup"):
            pytest.skip("gitlab-signup not captured")
        await verify_browser_session(
            LocalBrowserProvider(),
            url=corpus_url("gitlab-signup"),
            known=Target(role="textbox", name="First name"),
        )


class TestTierZeroOnRealPages:
    """The corpus finding, now as the resolver's behaviour."""

    @pytest.mark.parametrize(
        ("slug", "role", "name"),
        [
            ("gitlab-signup", "textbox", "First name"),
            ("gitlab-signup", "textbox", "Company email"),
            ("gitlab-signup", "button", "Continue with Google"),
            ("heroku-signup", "textbox", "Email address"),
            ("wikipedia-search", "searchbox", "Search Wikipedia"),
        ],
    )
    async def test_it_resolves_a_labelled_control(
        self, corpus_url, slug: str, role: str, name: str
    ) -> None:
        if not has_page(slug):
            pytest.skip(f"{slug} not captured")
        session = await open_page(corpus_url(slug))
        try:
            assert await session.locate(Target(role=role, name=name)) == 1
        finally:
            await session.close()

    @pytest.mark.parametrize(
        ("slug", "role", "name"),
        [
            # The finding. Both of these carry an `aria-label` that differs from
            # the placeholder a person reads, so the accessible name is NOT the
            # text on screen and role+name alone cannot reach them:
            #   substack  placeholder "Your email"  aria-label "Email"
            #   kayak     placeholder "To?"         aria-label "Destination location"
            # Steps 4 and 5 of the chain are what make these resolve, at no cost
            # and no model call.
            ("substack-signup", "textbox", "Your email"),
            ("kayak-search", "textbox", "To?"),
        ],
    )
    async def test_it_resolves_a_control_the_tree_names_differently(
        self, corpus_url, slug: str, role: str, name: str
    ) -> None:
        if not has_page(slug):
            pytest.skip(f"{slug} not captured")
        session = await open_page(corpus_url(slug))
        try:
            assert await session.locate(Target(role=role, name=name)) == 1, (
                f"{name!r} on {slug} did not resolve. This is the case the "
                "placeholder and label accessors exist for — if it regresses, "
                "the corpus rate drops with it.")
        finally:
            await session.close()

    async def test_a_visually_hidden_input_is_still_a_control(
        self, corpus_url
    ) -> None:
        """The defect the control fixture caught, pinned so it cannot return.

        Wikipedia's appearance radios are ``<input class=visually-hidden>``
        beside styled labels — the standard pattern for every custom radio,
        checkbox and switch. Treating ``opacity: 0`` as hidden deletes them, and
        the corpus scored Wikipedia 47% until that was fixed.
        """
        if not has_page("wikipedia-search"):
            pytest.skip("wikipedia-search not captured")
        session = await open_page(corpus_url("wikipedia-search"))
        try:
            page = await session.snapshot()
            radios = [c for c in page.controls() if c.role == "radio"]
            assert len(radios) >= 4, (
                f"only {len(radios)} radios visible. A visually-hidden input is "
                "still in the accessibility tree and still actionable — check "
                "the visibility rule before blaming the page.")
        finally:
            await session.close()

    async def test_ambiguity_refuses_on_a_real_page(self, corpus_url) -> None:
        """A real listing repeats controls, and that must not resolve.

        Resy shows a 'Notify' button beside every fully-booked venue. Picking
        one would be a guess about which restaurant.
        """
        if not has_page("resy-venue"):
            pytest.skip("resy-venue not captured")
        session = await open_page(corpus_url("resy-venue"))
        try:
            target = Target(role="button", name="Notify")
            assert await session.locate(target) > 1
            from loom.browser.errors import AmbiguousTarget

            with pytest.raises(AmbiguousTarget):
                await session.perform(
                    ActionPlan(method=ActionMethod.CLICK, target=target))
        finally:
            await session.close()


class TestTheFixturesAreFrozen:
    async def test_no_corpus_page_reaches_the_network(self, corpus_url) -> None:
        """Empirical, not a regex over the HTML.

        A snapshot that phones home is not frozen, and it would present as
        flakiness rather than as a broken fixture.
        """
        from playwright.async_api import async_playwright

        external: list[str] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            async def block(route, request):
                if "127.0.0.1" in request.url or request.url.startswith("data:"):
                    await route.continue_()
                else:
                    external.append(request.url)
                    await route.abort()

            await page.route("**", block)
            for path in sorted(PAGES.glob("*.html"))[:6]:
                await page.goto(corpus_url(path.stem), wait_until="load")
            await browser.close()

        assert not external, f"frozen pages reached out to: {external[:5]}"


class TestRefusals:
    async def test_durable_scope_is_refused_not_downgraded(self) -> None:
        """E4/E5. A local browser dies with the process, and says so.

        Silently downgrading would let a host believe a two-hour approval will
        survive a deploy. ``ExecutionSandbox.enforces``, one layer out.
        """
        provider = LocalBrowserProvider()
        assert "reattach" not in provider.supports()
        with pytest.raises(BrowserUnavailable, match="DURABLE"):
            await provider.open(BrowserPolicy(scope=SessionScope.DURABLE))

    async def test_reattach_raises_rather_than_opening_a_fresh_session(self) -> None:
        from loom.browser import SessionHandle

        provider = LocalBrowserProvider()
        with pytest.raises(SessionLost):
            await provider.reattach(
                SessionHandle(session_id="gone", provider="local"))

    async def test_a_missing_driver_names_the_extra_to_install(self) -> None:
        provider = LocalBrowserProvider(engine="patchright")
        if _importable("patchright"):
            pytest.skip("patchright is installed here")
        with pytest.raises(BrowserUnavailable, match="loomsdk\\[stealth\\]"):
            await provider.open(BrowserPolicy())


def _importable(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


def test_corpus_manifests_match_what_the_resolver_can_reach() -> None:
    """The corpus and the resolver must not drift apart.

    Both read role and name; if a manifest ever grew a field the resolver does
    not honour, the measured rate would stop describing the shipped behaviour —
    which is the failure that makes a benchmark worse than none.
    """
    honoured = {"purpose", "role", "name", "ordinal", "exact", "css",
                "visible_label", "required_for_flow", "in_iframe", "note"}
    for path in PAGES.glob("*.controls.json"):
        for control in json.loads(path.read_text())["controls"]:
            unknown = set(control) - honoured
            assert not unknown, (
                f"{path.name} uses {unknown}, which tier-0 resolution does not "
                "read. Either honour it in Target or drop it from the manifest.")
