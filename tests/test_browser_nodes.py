"""Phase 13.1 — the ``browser.*`` nodes over a provider that is not a browser.

Offline by construction: ``FakeBrowserProvider`` serves recorded snapshots, so
none of this needs Chromium, the network, or the ``[browser]`` extra. The one
test that does drive a real browser lives in ``test_browser_local.py`` and skips
without it.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, workflow
from loom.browser import FakeBrowserProvider, PageSnapshot, TreeNode
from loom.stores.memory import MemoryStore

FORM = PageSnapshot(
    url="https://fixture.test/form",
    title="Book a table",
    tree=(
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="textbox", name="Party size"),
        TreeNode(role="combobox", name="Seating"),
        TreeNode(role="button", name="Find a table"),
        TreeNode(role="button", name="Notify"),
        TreeNode(role="button", name="Notify"),
    ),
    text="Book a table at the Fixture",
)


def runtime(**kwargs) -> tuple[Runtime, FakeBrowserProvider]:
    provider = FakeBrowserProvider({FORM.url: FORM}, permissive=False)
    return Runtime(store=MemoryStore(), browser=provider, **kwargs), provider


def actions(provider: FakeBrowserProvider) -> list[tuple[str, str]]:
    return [(p.method.value, p.target.name)
            for s in provider.sessions for p in s.performed]


@workflow(name="fill_form")
async def fill_form(ctx: Context, _input) -> str:
    page = await ctx.node("browser.navigate", {"url": FORM.url})
    await ctx.node("browser.act", {
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": "someone@example.com"})
    await ctx.node("browser.act", {
        "method": "click",
        "target": {"role": "button", "name": "Find a table"}})
    return f"{page.title}: {len(page.controls)} controls"


class TestItDrivesAPage:
    async def test_a_flow_navigates_fills_and_clicks(self) -> None:
        rt, provider = runtime()
        result = await rt.run(fill_form, None)
        assert result.status.value == "completed", result.error
        assert result.output == "Book a table: 6 controls"
        assert actions(provider) == [("fill", "Email"), ("click", "Find a table")]

    async def test_the_page_reports_what_would_not_resolve(self) -> None:
        """``summary()`` says when nothing carries a name.

        The sentence ``BrowserProbe`` was written for: "0 named" is not a
        description of a page, it is the reason a target will not resolve on it.
        """
        blank = PageSnapshot(url="https://fixture.test/canvas", title="Canvas",
                             tree=(TreeNode(role="button", name=""),))
        rt = Runtime(store=MemoryStore(),
                     browser=FakeBrowserProvider({blank.url: blank},
                                                 permissive=False))

        @workflow(name="look")
        async def look(ctx: Context, _input) -> str:
            page = await ctx.node("browser.navigate", {"url": blank.url})
            return page.summary

        result = await rt.run(look, None)
        assert "0 named" in result.output
        assert "will find nothing here" in result.output


class TestAmbiguityIsRefused:
    async def test_two_matches_raise_rather_than_guessing(self) -> None:
        """The property the whole design rests on.

        Picking a match is how an automation clicks the wrong control and
        reports success — a failure with no error attached to it.
        """
        rt, provider = runtime()

        @workflow(name="ambiguous")
        async def ambiguous(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Notify"}})
            return "clicked"

        result = await rt.run(ambiguous, None)
        assert result.status.value == "failed"
        assert result.error and "2 controls matched" in result.error.message
        assert actions(provider) == [], "nothing may be performed on an ambiguity"

    async def test_an_ordinal_is_how_a_caller_chooses_deliberately(self) -> None:
        rt, provider = runtime()

        @workflow(name="ordinal")
        async def ordinal(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Notify", "ordinal": 2}})
            return "clicked"

        result = await rt.run(ordinal, None)
        assert result.status.value == "completed", result.error
        assert actions(provider) == [("click", "Notify")]

    async def test_a_missing_control_names_what_the_page_does_carry(self) -> None:
        rt, _ = runtime()

        @workflow(name="missing")
        async def missing(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Checkout"}})
            return "clicked"

        result = await rt.run(missing, None)
        assert result.status.value == "failed"
        # Not just "not found": the message carries the page's own shape, so
        # the next step is visible without opening a browser by hand.
        assert result.error and "6 control(s)" in result.error.message


class TestTheJournalIsTheCache:
    async def test_replay_touches_no_browser_at_all(self) -> None:
        """E2. The headline property, and it is free.

        Stagehand and Skyvern each build a bespoke action cache to get this.
        LOOM's engine serves a completed entry before the broker is reached, so
        a replay resolves nothing, clicks nothing, and opens no session.
        """
        rt, provider = runtime()
        first = await rt.run(fill_form, None)
        assert first.status.value == "completed"

        performed_before = len(actions(provider))
        sessions_before = len(provider.sessions)

        replayed = await rt.replay(first.run_id)

        assert replayed.output == first.output
        assert len(actions(provider)) == performed_before, "replay re-clicked"
        assert len(provider.sessions) == sessions_before, "replay opened a browser"

    async def test_a_node_call_journals_ordinary_step_entries(self) -> None:
        """E1. A node adds packaging, never durability semantics.

        The claim the whole node system rests on: the entries are ordinary
        steps, one per durable operation, with the node's own I/O nested
        beneath its path exactly as ``ctx.nested()`` produces for any node.
        Deleting ``loom/browser/`` could not change how anything else replays.
        """
        rt, _ = runtime()
        result = await rt.run(fill_form, None)
        entries = await rt.store.load_journal(result.run_id)

        assert {e.kind.value for e in entries} == {"step"}, (
            "browser nodes must not introduce a journal entry kind")

        outer = [e for e in entries if e.name.startswith("node:")]
        inner = [e for e in entries if e.name.startswith("browser:")]
        assert [e.name for e in outer] == [
            "node:browser.navigate", "node:browser.act", "node:browser.act"]
        assert [e.name for e in inner] == [
            "browser:navigate", "browser:fill", "browser:click"]
        for entry in inner:
            assert "." in entry.path, (
                f"{entry.name} at path {entry.path} is not nested under its node")


class TestSessionLifetime:
    async def test_the_session_closes_however_the_body_exits(self) -> None:
        rt, _provider = runtime()
        await rt.run(fill_form, None)
        assert rt.browser_sessions is not None
        assert len(rt.browser_sessions) == 0

    async def test_it_closes_even_when_the_body_fails(self) -> None:
        """A leaked Chromium outlives the run that opened it.

        Which is why this is the engine's job at the body boundary and not
        something a workflow is asked to remember.
        """
        rt, _ = runtime()

        @workflow(name="boom")
        async def boom(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            raise RuntimeError("boom")

        result = await rt.run(boom, None)
        assert result.status.value == "failed"
        assert rt.browser_sessions is not None
        assert len(rt.browser_sessions) == 0

    async def test_resuming_mid_flow_refuses_rather_than_driving_a_blank_page(
        self,
    ) -> None:
        """The honest limit of ``SessionScope.STEP``, made loud.

        A fresh browser is not where the flow left off. Continuing would drive
        the wrong page; re-running the recorded navigations would repeat
        effects. So it refuses, and the message names both ways out.
        """
        rt, _ = runtime()

        @workflow(name="orphan")
        async def orphan(ctx: Context, _input) -> str:
            # No navigate: stands in for a resumed body whose navigate was
            # served from the journal while the browser died with the process.
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Find a table"}})
            return "clicked"

        result = await rt.run(orphan, None)
        assert result.status.value == "failed"
        assert result.error is not None
        message = result.error.message
        assert "no live browser" in message
        assert "ctx.step" in message and "reattach" in message

    async def test_shutdown_closes_what_this_process_still_holds(self) -> None:
        rt, provider = runtime()

        @workflow(name="shutdown_step")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            return "ok"

        await rt.run(flow, None)
        await rt.shutdown(drain=0)
        assert provider.sessions[0].closed

    async def test_shutdown_leaves_a_parked_durable_session_open(self) -> None:
        """The one browser ``shutdown`` must not touch.

        A run parked on a person may resume in another process entirely — that
        is the whole point of the scope — so closing its browser on the way out
        would break exactly the case durability exists for.
        """
        provider = FakeBrowserProvider({FORM.url: FORM}, permissive=False,
                                       durable=True)
        rt = Runtime(store=MemoryStore(), browser=provider)

        @workflow(name="shutdown_durable")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate",
                           {"url": FORM.url, "scope": "durable"})
            await ctx.wait_for_approval("later")
            return "ok"

        parked = await rt.run(flow, None)
        assert parked.status.value == "suspended"
        await rt.shutdown(drain=0)
        assert not provider.sessions[0].closed

    async def test_a_second_navigate_starts_a_clean_flow(self) -> None:
        """Not a reuse: inheriting the first flow's cookies would make the same
        code behave differently depending on what ran before it."""
        rt, provider = runtime()

        @workflow(name="twice")
        async def twice(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": FORM.url})
            await ctx.node("browser.navigate", {"url": FORM.url})
            return "ok"

        await rt.run(twice, None)
        assert len(provider.sessions) == 2


class TestConfiguration:
    async def test_no_provider_is_reported_before_the_body_runs(self) -> None:
        rt = Runtime(store=MemoryStore())
        result = await rt.run(fill_form, None)
        assert result.status.value == "failed"
        assert result.error and "browser" in result.error.message.lower()

    def test_a_runtime_without_a_browser_is_unchanged(self) -> None:
        """``Runtime(browser=None)`` allocates nothing and is exactly what
        shipped before this phase."""
        rt = Runtime(store=MemoryStore())
        assert rt.browser is None
        assert rt.browser_sessions is None
        assert rt._browser_sessions is None


class TestEffectDeclarations:
    """What the nodes tell the broker chain, checked as data.

    These declarations are what make taint, grants and dry-run work on browser
    flows with no browser-specific code anywhere in ``runtime/``, so they are
    asserted here rather than left to be discovered.
    """

    @pytest.mark.parametrize(
        "node_id",
        ["browser.navigate", "browser.snapshot", "browser.act", "browser.extract"],
    )
    def test_every_browser_node_is_open_world(self, node_id: str) -> None:
        from loom.nodes.registry import get_node_catalog

        spec = get_node_catalog().get(node_id)
        assert spec is not None, f"{node_id} is not registered"
        assert spec.open_world is True, (
            "a browser node reaches outside the deployment, so a run that uses "
            "one has read data it did not bring with it")
        assert spec.deterministic is False
        assert "browser" in spec.requires

    def test_the_category_costs_one_line_in_the_prompt(self) -> None:
        """``NodeCategory.BROWSER`` earns its place by being findable.

        The prompt block is O(categories) and never O(nodes) — the property
        that lets a project register five hundred custom nodes without
        lengthening any prompt — so a new category is exactly one line.
        """
        from loom.nodes.spec import CATEGORY_BLURBS, NodeCategory

        assert NodeCategory.BROWSER in CATEGORY_BLURBS
        assert "\n" not in CATEGORY_BLURBS[NodeCategory.BROWSER]


class TestEvidenceReachesTheCaller:
    """`vision` captured a screenshot and threw it away.

    The provider has always taken the picture — `PageSnapshot.screenshot` held
    it, and `local.py` filled it in — but `_page_out` built its `PageOut`
    without that field, so a caller passed `vision=True`, paid for the pixels,
    and received nothing. Found by watching the coding agent: given a spec that
    asked for screenshots it searched the catalogue for "screenshot", "vision",
    "capture", "artifact" and "image" across twelve turns, found nothing that
    could produce one, and ran out of budget without writing a line.
    """

    def test_page_out_can_carry_a_screenshot(self) -> None:
        from loom.nodes.browser import PageOut

        assert "screenshot" in PageOut.model_fields

    def test_it_is_an_attachment_not_a_path(self) -> None:
        """So it journals losslessly, and offloads by content hash when blobs
        are configured rather than putting a PNG in a journal row."""
        from loom.blobs.attachment import Attachment
        from loom.nodes.browser import PageOut

        shot = Attachment.from_bytes("page.png", b"\x89PNG" + b"x" * 64,
                                     mime="image/png")
        page = PageOut(url="https://example.test", screenshot=shot)

        assert page.screenshot is not None
        assert page.screenshot.mime == "image/png"
        assert PageOut.model_validate_json(page.model_dump_json()).screenshot == shot

    def test_no_vision_means_no_screenshot(self) -> None:
        """Pixels stay opt-in: tier 0 resolves from the accessibility tree and
        never reads them."""
        from loom.nodes.browser import PageOut

        assert PageOut(url="https://example.test").screenshot is None

    def test_navigate_can_ask_for_one(self) -> None:
        """Proving you reached a page should not need a second node call."""
        from loom.nodes.browser import NavigateIn

        assert "vision" in NavigateIn.model_fields
        assert NavigateIn(url="https://example.test").vision is False


class TestTheRuntimeCanReachABrowser:
    """Without this the whole node suite is unreachable from every surface.

    `loom run` refused a browser workflow with "requires browser, which this
    Runtime does not have configured", and the only way to satisfy it was to
    construct the Runtime by hand in Python — so no CLI, MCP or HTTP caller
    could use a browser node at all.
    """

    def test_from_env_wires_a_provider_when_the_extra_is_installed(self) -> None:
        import pytest as _pytest

        _pytest.importorskip("playwright.async_api")
        from loom.runtime.engine import Runtime

        assert Runtime.from_env().browser is not None

    def test_an_explicit_none_still_wins(self) -> None:
        """`Runtime(browser=None)` is how a caller insists on having none."""
        from loom.runtime.engine import Runtime

        assert Runtime.from_env(browser=None).browser is None

    def test_a_missing_extra_is_not_an_error(self, monkeypatch) -> None:
        """The node's own requirement check reports it, the same way a missing
        model key leaves `agent_backend` unset rather than failing startup."""
        import builtins

        real = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("no playwright here")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        from loom.runtime.engine import _browser_from_env

        assert _browser_from_env() is None
