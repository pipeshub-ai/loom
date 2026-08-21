"""Phase 13.2 — declared effects, taint, tier 1, and drift.

13.1 shipped the deterministic half: a caller-supplied target, resolved without
a model, all reads. This is the half that can change the world, so almost
everything here is about what it refuses.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, workflow
from loom.agents.backend import BuiltInBackend
from loom.browser import FakeBrowserProvider, PageSnapshot, TreeNode
from loom.runtime.effects import GuardedBroker
from loom.runtime.taint import TaintBroker, TaintPolicy
from loom.stores.memory import MemoryStore
from loom.testing.mock import MockModelProvider, mock_response
from loom.toolsets.manifest import EffectClass

PAGE = PageSnapshot(
    url="https://fixture.test/book",
    title="Book a table",
    tree=(
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="button", name="Confirm booking"),
        TreeNode(role="button", name="Cancel booking"),
    ),
    text="Book a table",
)


def runtime(*, policy: TaintPolicy | None = None, model=None):
    provider = FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)
    kwargs = {}
    if policy is not None:
        kwargs["broker"] = TaintBroker(GuardedBroker(), policy)
    if model is not None:
        kwargs["agent_backend"] = BuiltInBackend(model)
    return Runtime(store=MemoryStore(), browser=provider, **kwargs), provider


def acted(provider) -> list[str]:
    return [p.method.value for s in provider.sessions for p in s.performed]


class TestTheEffectIsDeclared:
    def test_it_defaults_to_write(self) -> None:
        """A fail-safe backstop, not a classification.

        `OperationSpec.effect` takes the same position. Phase 12's audit found
        the alternative — guessing from a name — under-classifying 14% of
        LOOM's own operations including seven destructive ones. Here the seven
        would be card charges.
        """
        from loom.nodes.browser import ActIn

        parsed = ActIn.model_validate(
            {"method": "click", "target": {"role": "button"}})
        assert parsed.effect is EffectClass.WRITE

    def test_the_node_declares_the_table_the_broker_reads(self) -> None:
        from loom.nodes.registry import get_node_catalog, load_builtin_nodes

        load_builtin_nodes()
        spec = get_node_catalog().get("browser.act")
        assert spec is not None
        assert spec.effect is EffectClass.WRITE
        assert spec.effect_by["effect"]["DESTRUCTIVE"] is EffectClass.DESTRUCTIVE


class TestTaint:
    """E3. The safety property, and it needs no browser-specific code in runtime/.

    ``TaintBroker`` keys on ``EffectClass`` and ``open_world``, both of which
    the browser nodes declare like any other node.
    """

    @pytest.mark.parametrize(
        ("policy", "effect", "allowed"),
        [
            (TaintPolicy(), "read", True),
            (TaintPolicy(), "write", False),
            (TaintPolicy(), "destructive", False),
            # The narrow dial CLAUDE.md recommends when block_writes is too
            # strict: after reading the open web you may update a record, but
            # you may not delete one.
            (TaintPolicy(block_writes=False), "read", True),
            (TaintPolicy(block_writes=False), "write", True),
            (TaintPolicy(block_writes=False), "destructive", False),
        ],
    )
    async def test_a_page_read_governs_what_may_follow(
        self, policy: TaintPolicy, effect: str, allowed: bool
    ) -> None:
        rt, provider = runtime(policy=policy)

        @workflow(name=f"flow_{effect}_{policy.block_writes}")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Confirm booking"},
                "effect": effect})
            return "DONE"

        result = await rt.run(flow, None)
        if allowed:
            assert result.status.value == "completed", result.error
            assert acted(provider) == ["click"]
        else:
            assert result.status.value == "failed"
            assert acted(provider) == [], "a refused action must not be performed"
            assert result.error and "read external data" in result.error.message

    async def test_the_refusal_names_what_was_read(self) -> None:
        """"You read X, then tried to write Y" is actionable; "denied" is not."""
        rt, _ = runtime(policy=TaintPolicy())

        @workflow(name="named_source")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Confirm booking"},
                "effect": "write"})
            return "DONE"

        result = await rt.run(flow, None)
        assert result.error and "browser.navigate" in result.error.message

    async def test_a_run_that_read_nothing_may_still_write(self) -> None:
        """Taint is about data the run did not bring with it.

        Without a preceding read there is nothing to leak, so a write is
        ordinary work — which is what keeps the rule usable.
        """
        rt, _provider = runtime(policy=TaintPolicy())

        @workflow(name="no_read_first")
        async def flow(ctx: Context, _input) -> str:
            # Navigating is itself an open-world read, so this workflow reaches
            # the page through a permissive provider rather than reading first.
            await ctx.node("browser.navigate", {"url": PAGE.url})
            return "READ ONLY"

        result = await rt.run(flow, None)
        assert result.status.value == "completed"


class TestApprovalNeedsADurableSession:
    async def test_clearing_the_taint_costs_the_browser(self) -> None:
        """13.2's honest dead end, asserted rather than left to be discovered.

        ``ctx.wait_for_approval`` is the escape hatch that makes the taint rule
        usable — and it **parks the run**, which under ``SessionScope.STEP``
        ends the browser session. So the approval clears the taint and the very
        next browser call finds no browser.

        That is not a defect in either mechanism; it is the two of them
        composing honestly, and it is precisely what ``SessionScope.DURABLE``
        exists to fix in 13.3. Pinned here so the limitation is visible in the
        suite rather than in somebody's production run.
        """
        rt, _ = runtime(policy=TaintPolicy())

        @workflow(name="approve_then_write")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.wait_for_approval("booking")
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Confirm booking"},
                "effect": "write"})
            return "BOOKED"

        parked = await rt.run(flow, None)
        assert parked.status.value == "suspended"

        await rt.approve(parked.run_id, "booking")
        resumed = await rt.resume(parked.run_id)

        # Not a taint refusal — the approval did clear it. The browser is gone.
        assert resumed.status.value == "failed"
        assert resumed.error is not None
        assert resumed.error.type == "SessionLost"
        assert "no live browser" in resumed.error.message


class TestObserve:
    async def test_an_exact_name_costs_no_model_call(self) -> None:
        """Tier 0 first, and `tier` records that it answered.

        A model that is never called is the cheapest correct answer available,
        and `ObserveOut.tier` is how "did tier 0 suffice" is read off the
        journal rather than estimated.
        """
        model = MockModelProvider(responses=[mock_response("SHOULD NOT BE CALLED")])
        rt, _ = runtime(model=model)

        @workflow(name="observe_exact")
        async def flow(ctx: Context, _input) -> dict:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node("browser.observe",
                                   {"intent": "Confirm booking"})
            return {"found": found.found, "tier": found.tier,
                    "name": found.target.name if found.target else None}

        result = await rt.run(flow, None)
        assert result.output == {"found": True, "tier": 0,
                                 "name": "Confirm booking"}

    async def test_a_description_reaches_tier_one(self) -> None:
        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        rt, _ = runtime(model=model)

        @workflow(name="observe_described")
        async def flow(ctx: Context, _input) -> dict:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node(
                "browser.observe",
                {"intent": "the button that finalises the reservation"})
            return {"found": found.found, "tier": found.tier,
                    "name": found.target.name if found.target else None}

        result = await rt.run(flow, None)
        assert result.output["tier"] == 1
        assert result.output["name"] == "Confirm booking"

    async def test_it_chooses_only_from_controls_the_page_carries(self) -> None:
        """A model that invents a target must not produce one.

        It picks from a list the page supplied, so the worst outcome is the
        wrong control rather than one that does not exist — and the answer
        still goes back through tier-0 resolution before anything is clicked.
        """
        model = MockModelProvider(responses=[mock_response("Delete everything")])
        rt, _ = runtime(model=model)

        @workflow(name="observe_hallucinated")
        async def flow(ctx: Context, _input) -> dict:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node("browser.observe", {"intent": "something else"})
            return {"found": found.found, "reason": found.reason}

        result = await rt.run(flow, None)
        assert result.output["found"] is False
        assert "no control matched" in result.output["reason"]

    async def test_tier_zero_still_answers_with_no_model_configured(self) -> None:
        """A Runtime with no agent backend is not a Runtime that cannot observe.

        Declaring ``requires=["agent_backend"]`` would be the obvious thing and
        the wrong one — it is checked before every call, and most calls never
        reach tier 1.
        """
        rt, _ = runtime()  # no model

        @workflow(name="observe_no_model_exact")
        async def flow(ctx: Context, _input) -> int:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node("browser.observe", {"intent": "Confirm booking"})
            return found.tier

        result = await rt.run(flow, None)
        assert result.status.value == "completed", result.error
        assert result.output == 0

    async def test_needing_tier_one_without_a_model_explains_itself(self) -> None:
        rt, _ = runtime()  # no model

        @workflow(name="observe_no_model_described")
        async def flow(ctx: Context, _input) -> bool:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node("browser.observe",
                                   {"intent": "whichever one finalises it"})
            return found.found

        result = await rt.run(flow, None)
        assert result.status.value == "failed"
        assert result.error is not None
        # Names browser.observe and what the caller can do, not ctx.agent.
        assert "browser.observe" in result.error.message
        assert "browser.act" in result.error.message

    async def test_observing_never_acts(self) -> None:
        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        rt, provider = runtime(model=model)

        @workflow(name="observe_only")
        async def flow(ctx: Context, _input) -> bool:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node("browser.observe", {"intent": "confirm it"})
            return found.found

        await rt.run(flow, None)
        assert acted(provider) == [], "observe must never perform an action"


class TestDrift:
    """Repair a read, refuse a write. The rule no self-healing agent has."""

    async def test_a_read_is_re_aimed_from_its_intent(self) -> None:
        model = MockModelProvider(responses=[mock_response("Email")])
        rt, provider = runtime(model=model)

        @workflow(name="drift_read")
        async def flow(ctx: Context, _input) -> int:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            result = await ctx.node("browser.act", {
                "method": "fill",
                # A stale target — this control no longer exists by this name.
                "target": {"role": "textbox", "name": "E-mail address"},
                "value": "a@b.com",
                "intent": "the email field",
                "effect": "read"})
            return result.tier

        result = await rt.run(flow, None)
        assert result.status.value == "completed", result.error
        assert result.output == 1, "a repaired action reports tier 1"
        assert acted(provider) == ["fill"]

    async def test_a_write_is_never_silently_re_aimed(self) -> None:
        """The whole reason drift is a policy and not a retry.

        A control that moved under a write is a different control until
        somebody says otherwise. Every browser agent in this space repairs
        here; repairing is how one confirms the wrong reservation.
        """
        model = MockModelProvider(responses=[mock_response("Cancel booking")])
        rt, provider = runtime(model=model)

        @workflow(name="drift_write")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Confirm reservation"},
                "intent": "the button that confirms the booking",
                "effect": "write"})
            return "DONE"

        result = await rt.run(flow, None)
        assert result.status.value == "failed"
        assert result.error is not None
        assert result.error.type == "SelectorDrift"
        assert "will not be re-aimed" in result.error.message
        assert acted(provider) == []

    async def test_repair_is_available_when_the_caller_asks_for_it(self) -> None:
        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        rt, provider = runtime(model=model)

        @workflow(name="drift_forced")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Confirm reservation"},
                "intent": "the button that confirms the booking",
                "effect": "write",
                "if_drifted": "repair"})
            return "DONE"

        result = await rt.run(flow, None)
        assert result.status.value == "completed", result.error
        assert acted(provider) == ["click"]

    async def test_without_an_intent_the_original_error_survives(self) -> None:
        """Nothing to re-aim towards, and "not found" is the better message."""
        rt, _ = runtime()

        @workflow(name="drift_no_intent")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            await ctx.node("browser.act", {
                "method": "click",
                "target": {"role": "button", "name": "Nonexistent"},
                "effect": "read"})
            return "DONE"

        result = await rt.run(flow, None)
        assert result.status.value == "failed"
        assert result.error is not None
        assert result.error.type == "TargetNotFound"


class TestThePlanCache:
    """Paying a model once for an intent, not once per run.

    The saving Stagehand's action cache and Skyvern's code cache both exist to
    capture — here it is 30 lines over ``CacheStore``, because the hard part
    (serving a settled call without re-resolving) is what the journal already
    does, and this only has to cover the *second run*.
    """

    async def test_a_second_run_costs_no_model_call(self) -> None:
        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        provider = FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)
        store = MemoryStore()

        @workflow(name="observe_twice")
        async def flow(ctx: Context, _input) -> dict:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node(
                "browser.observe", {"intent": "whichever one finalises it"})
            return {"tier": found.tier, "name": found.target.name
                    if found.target else None, "why": found.reason}

        rt = Runtime(store=store, browser=provider,
                     agent_backend=BuiltInBackend(model))
        first = await rt.run(flow, None)
        assert first.output["tier"] == 1, first.output

        # One scripted response. A second model call would raise, so this run
        # passing *is* the assertion that none was made.
        second = await rt.run(flow, None)
        assert second.status.value == "completed", second.error
        assert second.output["tier"] == 0
        assert second.output["name"] == "Confirm booking"
        assert "plan cache" in second.output["why"]

    async def test_a_stale_entry_is_discarded_not_acted_on(self) -> None:
        """The property that makes a cross-run cache safe at all.

        A remembered target is verified against the live page before it is
        used. Without that, the cache would be a way to click something that is
        no longer there — or worse, something else that has taken its place.
        """
        from loom.browser import PlanCache, Target

        store = MemoryStore()
        cache = PlanCache(store, workflow="observe_stale")
        # A plan from a page that has since changed.
        await cache.put(PAGE.url, "whichever one finalises it",
                        Target(role="button", name="Submit order", exact=True))

        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        provider = FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)

        @workflow(name="observe_stale")
        async def flow(ctx: Context, _input) -> dict:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node(
                "browser.observe", {"intent": "whichever one finalises it"})
            return {"tier": found.tier, "name": found.target.name
                    if found.target else None}

        rt = Runtime(store=store, browser=provider,
                     agent_backend=BuiltInBackend(model))
        result = await rt.run(flow, None)

        assert result.status.value == "completed", result.error
        # Fell through to tier 1 rather than handing back "Submit order".
        assert result.output == {"tier": 1, "name": "Confirm booking"}

        # And it has been *replaced*, not merely dropped — so the next run gets
        # the corrected answer for free rather than paying tier 1 again.
        remembered = await cache.get(PAGE.url, "whichever one finalises it")
        assert remembered is not None
        assert remembered.name == "Confirm booking"

    async def test_replay_never_reaches_the_cache(self) -> None:
        """A settled observe is served by the journal, which is stricter.

        The cache answers the second *run*; the journal answers the *replay*.
        Confusing the two would let a replay pick up a plan the original run
        never used.
        """
        model = MockModelProvider(responses=[mock_response("Confirm booking")])
        provider = FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)
        store = MemoryStore()

        @workflow(name="observe_then_replay")
        async def flow(ctx: Context, _input) -> int:
            await ctx.node("browser.navigate", {"url": PAGE.url})
            found = await ctx.node(
                "browser.observe", {"intent": "whichever one finalises it"})
            return found.tier

        rt = Runtime(store=store, browser=provider,
                     agent_backend=BuiltInBackend(model))
        first = await rt.run(flow, None)
        assert first.output == 1

        replayed = await rt.replay(first.run_id)
        # Still 1 — the journal returned what actually happened, not what the
        # cache would say now.
        assert replayed.output == 1

    def test_the_key_ignores_the_part_that_identifies_a_record(self) -> None:
        """Two rows of the same form share a plan; two forms do not.

        Keying on the full URL would miss on every record while filling the
        cache with one entry per row — the per-record identity lives in the
        query, which is exactly what must stay out of the key.
        """
        from loom.browser import PlanCache

        cache = PlanCache(MemoryStore(), workflow="w")
        same = cache.key("https://x.test/book?id=1", "the confirm button")
        also = cache.key("https://x.test/book?id=2", "the confirm button")
        other = cache.key("https://x.test/cancel", "the confirm button")

        assert same == also
        assert same != other

    def test_two_workflows_do_not_share_an_answer(self) -> None:
        """The same words can mean different controls to different workflows."""
        from loom.browser import PlanCache

        one = PlanCache(MemoryStore(), workflow="alpha")
        two = PlanCache(MemoryStore(), workflow="beta")
        assert one.key(PAGE.url, "submit") != two.key(PAGE.url, "submit")

    async def test_a_broken_cache_is_a_missing_one(self) -> None:
        """Best-effort throughout: a cache that raises would turn a saving into
        an outage, and there is nothing here a caller cannot simply redo."""
        from loom.browser import PlanCache, Target

        class Hostile:
            async def get(self, key): raise RuntimeError("down")
            async def set(self, key, value, ttl): raise RuntimeError("down")
            async def delete(self, key): raise RuntimeError("down")

        cache = PlanCache(Hostile(), workflow="w")
        assert await cache.get(PAGE.url, "x") is None
        await cache.put(PAGE.url, "x", Target(role="button", name="Go"))
        await cache.forget(PAGE.url, "x")
