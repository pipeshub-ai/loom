"""Middleware around durable operations.

Phase 1: the effect family — everything that flows through the broker, which is
every durable operation a workflow performs.

The properties worth asserting are the ones that fail *silently* when they
break. A hook that does not run looks like a hook that found nothing; a
weakened decision looks like a permitted call; a hook that fires on replay looks
like a duplicate log line until the day it is a duplicate charge. Each of those
gets a test that would go red rather than quiet.

Three claims run through the file:

**Replay never reaches a hook.** The journal serves a completed entry before
``broker.dispatch`` is reached, so the entire family is replay-free by
construction. This is the claim the whole taxonomy rests on, so it is asserted
first and directly.

**Order and refusal are structural, not conventional.** ``before`` runs forward
and ``after`` in reverse because onions nest, not because anything sorts them.
A refusal cannot be lifted because escalation is ``max()``, not assignment.

**The cost of not using this is zero.** No hooks, no ``HookBroker`` — asserted,
because "nearly free" on the hot path of every durable operation is a claim that
decays without a test.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from loom import Context, Runtime, step, workflow
from loom.runtime.effects import DirectBroker, EffectCall, EffectResult
from loom.runtime.hooks import Decision, HookBroker, HookContext, HookRegistry
from loom.stores.memory import MemoryStore
from loom.toolsets.manifest import EffectClass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@step
async def add(n: int) -> int:
    return n + 1


@step
async def double(n: int) -> int:
    return n * 2


@step
async def boom(n: int) -> int:
    raise ValueError("step exploded")


@workflow(name="two_steps")
async def two_steps(ctx: Context, n: int) -> int:
    a = await ctx.step(add, n)
    return await ctx.step(double, a)


@workflow(name="one_step")
async def one_step(ctx: Context, n: int) -> int:
    return await ctx.step(add, n)


@workflow(name="failing")
async def failing(ctx: Context, n: int) -> int:
    return await ctx.step(boom, n)


@pytest.fixture
def rt() -> Runtime:
    runtime = Runtime(store=MemoryStore())
    runtime.register(two_steps)
    runtime.register(one_step)
    runtime.register(failing)
    return runtime


def a_call(**kw) -> EffectCall:
    """A bare EffectCall, for testing routing without running a workflow."""
    fields = {"kind": "step", "target": "add", "effect": EffectClass.WRITE}
    fields.update(kw)
    return EffectCall(**fields)


# ---------------------------------------------------------------------------
# The claim the taxonomy rests on
# ---------------------------------------------------------------------------


class TestHooksDoNotFireOnReplay:
    """Effect hooks sit behind the journal lookup, so replay never reaches them.

    This is what makes the whole family safe to give I/O and refusal powers,
    and it is inherited from the broker rather than enforced here — which is
    exactly why it deserves a direct test rather than a comment.
    """

    async def test_a_replayed_run_runs_no_hooks(self, rt) -> None:
        seen: list[str] = []

        @rt.hooks.before_step
        async def note(ctx: HookContext) -> None:
            seen.append(ctx.target)

        result = await rt.run(two_steps, 1)
        assert seen == ["add", "double"]

        seen.clear()
        replayed = await rt.replay(result.run_id)

        assert seen == [], "a hook fired on replay"
        assert replayed.output == result.output

    async def test_a_hook_registered_after_the_run_does_not_see_a_replay(
        self, rt
    ) -> None:
        """The other half: registering late does not retroactively hook a run.

        Someone debugging a finished run will reach for exactly this, and a
        replay that suddenly ran their new gate would be the worst kind of
        surprise — enforcement applied to a decision already made.
        """
        result = await rt.run(two_steps, 1)

        seen: list[str] = []

        @rt.hooks.before_step
        async def late(ctx: HookContext) -> None:
            seen.append(ctx.target)

        await rt.replay(result.run_id)

        assert seen == []


# ---------------------------------------------------------------------------
# Shapes and ordering
# ---------------------------------------------------------------------------


class TestTheThreeShapes:
    async def test_before_sees_the_call_and_after_sees_the_result(self, rt) -> None:
        seen: dict[str, object] = {}

        @rt.hooks.before_step(target="add")
        async def pre(ctx: HookContext) -> None:
            seen["target"] = ctx.target
            seen["effect"] = ctx.effect

        @rt.hooks.after_step(target="add")
        async def post(ctx: HookContext) -> None:
            seen["result"] = ctx.result

        await rt.run(one_step, 41)

        assert seen["target"] == "add"
        assert seen["result"] == 42

    async def test_only_named_arguments_are_visible(self, rt) -> None:
        """A sharp edge, inherited from the broker and worth stating.

        ``EffectCall.arguments`` reports keyword arguments only — "a step
        invoked positionally has no argument names to report", per
        ``_effect_arguments``. So a hook deciding on *values* must be called
        with keywords; one deciding on *what* is being called (the common case,
        and what grants and taint both do) is unaffected.

        Widening this would change a wire type the sandbox and taint paths both
        read, so it is a decision of its own rather than something to slip into
        this phase.
        """
        seen: list[dict] = []

        @rt.hooks.before_step(target="add")
        async def watch(ctx: HookContext) -> None:
            seen.append(dict(ctx.arguments))

        @workflow(name="by_keyword")
        async def by_keyword(ctx: Context, value: int) -> int:
            return await ctx.step(add, n=value)

        rt.register(by_keyword)

        await rt.run(one_step, 41)          # positional
        await rt.run(by_keyword, 41)        # keyword

        assert seen[0] == {}, "positional arguments are not reported"
        assert seen[1] == {"n": 41}, "keyword arguments are"

    async def test_around_wraps_both_halves(self, rt) -> None:
        order: list[str] = []

        @rt.hooks.around_step(target="add")
        async def timed(ctx: HookContext, next) -> int:
            order.append("in")
            value = await next()
            order.append("out")
            return value

        await rt.run(one_step, 1)

        assert order == ["in", "out"]

    async def test_around_may_replace_the_result(self, rt) -> None:
        @rt.hooks.around_step(target="add")
        async def substitute(ctx: HookContext, next) -> int:
            await next()
            return 999

        result = await rt.run(one_step, 1)

        assert result.output == 999

    async def test_after_may_rewrite_the_result(self, rt) -> None:
        """The shape `Guardrail.REPLACE` becomes when it is re-homed."""

        @rt.hooks.after_step(target="add")
        async def redact(ctx: HookContext) -> None:
            ctx.result = -1

        result = await rt.run(one_step, 1)

        assert result.output == -1

    async def test_before_may_mutate_the_arguments(self, rt) -> None:
        """Sanitisation and defaulting, without wrapping the step."""
        captured: list[dict] = []

        @rt.hooks.before_step(target="add")
        async def watch(ctx: HookContext) -> None:
            ctx.arguments["injected"] = True
            captured.append(dict(ctx.arguments))

        await rt.run(one_step, 1)

        assert captured[0]["injected"] is True


class TestOrdering:
    """Forward for `before`, reverse for `after`, nested for `around`.

    None of this is sorted or enforced anywhere — it is what composing onions
    means. The test exists because that is easy to break while refactoring the
    composer and impossible to notice from the outside.
    """

    async def test_before_runs_in_registration_order(self, rt) -> None:
        order: list[str] = []

        @rt.hooks.before_step
        async def first(ctx: HookContext) -> None:
            order.append("first")

        @rt.hooks.before_step
        async def second(ctx: HookContext) -> None:
            order.append("second")

        await rt.run(one_step, 1)

        assert order == ["first", "second"]

    async def test_after_runs_in_reverse(self, rt) -> None:
        order: list[str] = []

        @rt.hooks.after_step
        async def first(ctx: HookContext) -> None:
            order.append("first")

        @rt.hooks.after_step
        async def second(ctx: HookContext) -> None:
            order.append("second")

        await rt.run(one_step, 1)

        assert order == ["second", "first"], "after must unwind, not repeat"

    async def test_before_and_after_interleave_as_one_onion(self, rt) -> None:
        order: list[str] = []

        @rt.hooks.before_step
        async def outer_pre(ctx: HookContext) -> None:
            order.append("outer-before")

        @rt.hooks.after_step
        async def outer_post(ctx: HookContext) -> None:
            order.append("outer-after")

        @rt.hooks.before_step
        async def inner_pre(ctx: HookContext) -> None:
            order.append("inner-before")

        await rt.run(one_step, 1)

        assert order == ["outer-before", "inner-before", "outer-after"]


class TestAroundIsWhyThereAreTwoPrimitives:
    async def test_it_may_call_next_more_than_once(self, rt) -> None:
        """The case a single-pass `next()` cannot express, and the entire
        reason `around` exists beside `before`/`after`. A retry policy is this
        test with a condition on it."""
        attempts: list[int] = []

        @rt.hooks.around_step(target="add")
        async def twice(ctx: HookContext, next) -> int:
            await next()
            attempts.append(1)
            return await next()

        result = await rt.run(one_step, 1)

        assert len(attempts) == 1
        assert result.output == 2

    async def test_it_may_call_next_zero_times(self, rt) -> None:
        """A cache hit never touches the step."""
        ran: list[int] = []

        @step
        async def counted(n: int) -> int:
            ran.append(n)
            return n

        @workflow(name="cached_flow")
        async def cached_flow(ctx: Context, n: int) -> int:
            return await ctx.step(counted, n)

        rt.register(cached_flow)

        @rt.hooks.around_step(target="counted")
        async def from_cache(ctx: HookContext, next) -> int:
            return 7

        result = await rt.run(cached_flow, 1)

        assert result.output == 7
        assert ran == [], "the step ran despite a cache hit"


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class TestDecisionsEscalateOnly:
    """A permissive middleware cannot undo a strict one, in either order.

    This is the property that makes registration order safe to not think about,
    and the one that quietly disappears the moment someone adds a setter.
    """

    def test_allow_cannot_lower_a_deny(self) -> None:
        ctx = HookContext(a_call())
        ctx.deny("no")
        ctx.allow()

        assert ctx.decision is Decision.DENY

    def test_ask_cannot_lower_a_deny(self) -> None:
        ctx = HookContext(a_call())
        ctx.deny("no")
        ctx.ask("maybe")

        assert ctx.decision is Decision.DENY
        assert ctx.reason == "no", "the reason followed the weaker decision"

    def test_deny_may_raise_an_ask(self) -> None:
        ctx = HookContext(a_call())
        ctx.ask("maybe")
        ctx.deny("no")

        assert ctx.decision is Decision.DENY
        assert ctx.reason == "no"

    def test_there_is_no_public_setter(self) -> None:
        """Escalation is the only path, so the guarantee cannot be bypassed
        by a middleware that assigns rather than calls."""
        ctx = HookContext(a_call())

        with pytest.raises(AttributeError):
            ctx.decision = Decision.ALLOW  # type: ignore[misc]

    async def test_a_later_permissive_hook_cannot_lift_a_refusal(self, rt) -> None:
        @rt.hooks.before_step(target="add")
        async def strict(ctx: HookContext) -> None:
            ctx.deny("policy")

        @rt.hooks.before_step(target="add")
        async def permissive(ctx: HookContext) -> None:
            ctx.allow()

        result = await rt.run(one_step, 1)

        assert result.status.value == "failed"


class TestDenial:
    async def test_a_denied_step_never_runs(self, rt) -> None:
        ran: list[int] = []

        @step
        async def guarded(n: int) -> int:
            ran.append(n)
            return n

        @workflow(name="guarded_flow")
        async def guarded_flow(ctx: Context, n: int) -> int:
            return await ctx.step(guarded, n)

        rt.register(guarded_flow)

        @rt.hooks.before_step(target="guarded")
        async def refuse(ctx: HookContext) -> None:
            ctx.deny("not allowed")

        result = await rt.run(guarded_flow, 1)

        assert ran == []
        assert result.status.value == "failed"

    async def test_the_denial_names_the_middleware_that_refused(self, rt) -> None:
        """A denial that only says "denied" becomes a support ticket. The same
        reasoning `EffectDenied.needs` already applies to grants."""

        @rt.hooks.before_step(target="add")
        async def house_policy(ctx: HookContext) -> None:
            ctx.deny("out of hours")

        result = await rt.run(one_step, 1)

        message = result.error.message if result.error else ""
        assert "house_policy" in message
        assert "out of hours" in message

    async def test_a_denial_stops_hooks_registered_behind_it(self, rt) -> None:
        reached: list[str] = []

        @rt.hooks.before_step(target="add")
        async def refuse(ctx: HookContext) -> None:
            ctx.deny("no")

        @rt.hooks.before_step(target="add")
        async def behind(ctx: HookContext) -> None:
            reached.append("behind")

        await rt.run(one_step, 1)

        assert reached == []


# ---------------------------------------------------------------------------
# Failure policy
# ---------------------------------------------------------------------------


class TestFailurePolicy:
    async def test_a_before_hook_that_raises_denies(self, rt) -> None:
        """Fail closed. A gate that could not run has not passed — the rule
        `CheckPipeline` applies to a missing linter and `Guardrail` applies to a
        check that raises."""
        ran: list[int] = []

        @step
        async def sensitive(n: int) -> int:
            ran.append(n)
            return n

        @workflow(name="sensitive_flow")
        async def sensitive_flow(ctx: Context, n: int) -> int:
            return await ctx.step(sensitive, n)

        rt.register(sensitive_flow)

        @rt.hooks.before_step(target="sensitive")
        async def broken(ctx: HookContext) -> None:
            raise RuntimeError("the gate is broken")

        result = await rt.run(sensitive_flow, 1)

        assert result.status.value == "failed"
        assert ran == [], "the step ran despite its gate being broken"

    async def test_an_after_hook_that_raises_does_not_destroy_the_result(
        self, rt
    ) -> None:
        """Fail open. The work already happened; a broken formatter must not
        turn a successful step into a failed one."""

        @rt.hooks.after_step(target="add")
        async def broken(ctx: HookContext) -> None:
            raise RuntimeError("bad format string")

        result = await rt.run(one_step, 41)

        assert result.status.value == "completed"
        assert result.output == 42

    async def test_after_runs_when_the_step_failed_and_sees_the_error(
        self, rt
    ) -> None:
        """A logger that only fires on success is a logger that misses every
        interesting event."""
        seen: list[str] = []

        @rt.hooks.after_step(target="boom")
        async def record(ctx: HookContext) -> None:
            seen.append(type(ctx.error).__name__ if ctx.error else "none")

        await rt.run(failing, 1)

        assert seen and seen[0] != "none"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_an_event_selects_its_kind(self, rt) -> None:
        seen: list[str] = []

        @rt.hooks.before_tool
        async def only_tools(ctx: HookContext) -> None:
            seen.append(ctx.target)

        await rt.run(two_steps, 1)

        assert seen == [], "a tool hook fired on a step"

    async def test_a_target_glob_narrows_within_an_event(self, rt) -> None:
        seen: list[str] = []

        @rt.hooks.before_step(target="dou*")
        async def only_double(ctx: HookContext) -> None:
            seen.append(ctx.target)

        await rt.run(two_steps, 1)

        assert seen == ["double"]

    def test_an_effect_class_narrows_by_what_the_call_does(self) -> None:
        """Effect class before path convention: Loom already carries it on
        every call and already relies on it for taint, so it is a truer key
        than a naming scheme."""
        registry = HookRegistry()
        seen: list[str] = []

        @registry.before_any(effect=EffectClass.DESTRUCTIVE)
        async def only_deletes(ctx: HookContext) -> None:
            seen.append(ctx.target)

        assert registry.chain_for(a_call(effect=EffectClass.READ)) == []
        assert len(registry.chain_for(a_call(effect=EffectClass.DESTRUCTIVE))) == 1

    def test_a_predicate_can_decide_anything(self) -> None:
        registry = HookRegistry()

        @registry.before_step(where=lambda ctx: ctx.target.startswith("a"))
        async def only_a(ctx: HookContext) -> None: ...

        assert len(registry.chain_for(a_call(target="add"))) == 1
        assert registry.chain_for(a_call(target="double")) == []

    def test_before_any_matches_every_kind(self) -> None:
        registry = HookRegistry()

        @registry.before_any
        async def everything(ctx: HookContext) -> None: ...

        for kind in ("step", "tool", "agent", "child"):
            assert len(registry.chain_for(a_call(kind=kind))) == 1, kind

    def test_an_unknown_event_says_what_exists(self) -> None:
        registry = HookRegistry()

        with pytest.raises(AttributeError, match="is not a hook event"):
            registry.before_banana  # noqa: B018


class TestNodeRouting:
    """Nodes journal as steps named `node:<id>`, so routing derives the event
    from the name. The alternative was a new EntryKind, which would change the
    journal format for runs already on disk to buy nothing this does not."""

    def test_a_node_call_is_routed_as_a_node(self) -> None:
        registry = HookRegistry()

        @registry.before_node
        async def on_node(ctx: HookContext) -> None: ...

        assert len(registry.chain_for(a_call(target="node:human.approval"))) == 1

    def test_a_step_hook_does_not_silently_catch_nodes(self) -> None:
        """Asking for steps and getting every node too would be a surprise
        rooted in an implementation detail of the journal."""
        registry = HookRegistry()

        @registry.before_step
        async def on_step(ctx: HookContext) -> None: ...

        assert registry.chain_for(a_call(target="node:human.approval")) == []
        assert len(registry.chain_for(a_call(target="add"))) == 1

    def test_a_node_glob_matches_the_id_not_the_prefix(self) -> None:
        registry = HookRegistry()

        @registry.before_node(node="human.*")
        async def on_human(ctx: HookContext) -> None: ...

        assert len(registry.chain_for(a_call(target="node:human.approval"))) == 1
        assert registry.chain_for(a_call(target="node:guard.pii")) == []

    def test_the_context_exposes_the_node_id_without_the_prefix(self) -> None:
        ctx = HookContext(a_call(target="node:human.approval"))

        assert ctx.node_id == "human.approval"
        assert HookContext(a_call(target="add")).node_id == ""


# ---------------------------------------------------------------------------
# Durable work inside a hook
# ---------------------------------------------------------------------------


class TestAHookCanJournalItsOwnWork:
    """The property the headline use cases depend on.

    A supervisor, a critique or a verification hook calls a model. Unjournaled,
    it re-runs on every retry and resume and is silently paid for twice. The
    nested path is derived from the hooked call rather than a sequence counter,
    which is what makes it stable enough to journal against.
    """

    async def test_the_hook_gets_a_context_scoped_beneath_the_call(
        self, rt
    ) -> None:
        paths: list[str] = []

        @rt.hooks.before_step(target="add")
        async def inspect(ctx: HookContext) -> None:
            paths.append(ctx.ctx.path_prefix if hasattr(ctx.ctx, "path_prefix") else "")
            assert ctx.ctx is not None

        await rt.run(one_step, 1)

        assert paths, "the hook received no context"

    async def test_a_hooks_own_step_is_journaled(self, rt) -> None:
        calls: list[int] = []

        @step
        async def critique(n: int) -> str:
            calls.append(n)
            return "fine"

        @rt.hooks.before_step(target="add")
        async def review(ctx: HookContext) -> None:
            await ctx.ctx.step(critique, 1)

        result = await rt.run(one_step, 1)
        entries = await rt.store.load_journal(result.run_id)

        assert calls == [1]
        assert any("critique" in e.name for e in entries), (
            "the hook's own work never reached the journal"
        )

    async def test_it_is_paid_for_once_across_a_replay(self, rt) -> None:
        """The whole point: a replayed run must not re-run the critique."""
        calls: list[int] = []

        @step
        async def critique(n: int) -> str:
            calls.append(n)
            return "fine"

        @rt.hooks.before_step(target="add")
        async def review(ctx: HookContext) -> None:
            await ctx.ctx.step(critique, 1)

        result = await rt.run(one_step, 1)
        assert len(calls) == 1

        await rt.replay(result.run_id)

        assert len(calls) == 1, "the critique was paid for twice"


# ---------------------------------------------------------------------------
# Composition with the existing chain
# ---------------------------------------------------------------------------


class TestItComposesWithTheBrokerChain:
    def test_no_hooks_means_no_hook_broker(self) -> None:
        """The cost of this feature to everyone not using it.

        An empty pipeline would otherwise run on every durable operation of
        every workflow, and "nearly free" is a claim that decays without a
        test.
        """
        runtime = Runtime(store=MemoryStore())

        assert not isinstance(runtime.broker, HookBroker)
        assert not runtime.hooks

    def test_the_first_registration_installs_it_once(self) -> None:
        runtime = Runtime(store=MemoryStore())

        @runtime.hooks.before_step
        async def one(ctx: HookContext) -> None: ...

        first = runtime.broker

        @runtime.hooks.before_step
        async def two(ctx: HookContext) -> None: ...

        assert isinstance(runtime.broker, HookBroker)
        assert runtime.broker is first, "a second hook wrapped the chain twice"

    def test_it_wraps_rather_than_replaces_the_existing_chain(self) -> None:
        runtime = Runtime(store=MemoryStore())
        original = runtime.broker

        @runtime.hooks.before_step
        async def one(ctx: HookContext) -> None: ...

        assert runtime.broker.inner is original

    async def test_a_refusal_from_further_down_still_refuses(self) -> None:
        """A hook must not be able to launder a grant denial into a success
        just by being registered above it."""

        class Refuses(DirectBroker):
            async def dispatch(self, call, authority):
                return EffectResult(ok=False, error="denied by policy below")

        runtime = Runtime(store=MemoryStore(), broker=Refuses())
        runtime.register(one_step)
        seen: list[str] = []

        @runtime.hooks.after_step
        async def observe(ctx: HookContext) -> None:
            seen.append("ran")

        result = await runtime.run(one_step, 1)

        assert result.status.value == "failed"
        assert seen == ["ran"], "the after hook did not see the refusal"

    def test_run_observer_calls_are_forwarded(self) -> None:
        """CLAUDE.md makes forwarding a standing obligation on every wrapping
        broker. Hooks discharge it once rather than per author."""
        forwarded: list[str] = []

        class Observing(DirectBroker):
            def observe_run(self, run_id, journal):
                forwarded.append(f"observe:{run_id}")

            def forget_run(self, run_id):
                forwarded.append(f"forget:{run_id}")

        broker = HookBroker(Observing(), HookRegistry())
        broker.observe_run("r1", None)
        broker.forget_run("r1")

        assert forwarded == ["observe:r1", "forget:r1"]


class TestTheSandboxWireShapeIsUnchanged:
    def test_the_context_field_is_not_on_the_wire(self) -> None:
        """`describe()` whitelists, so a local-only field cannot leak — the
        same property that already holds for `perform`. A sandboxed child must
        keep receiving exactly what it received before hooks existed."""
        call = EffectCall(kind="step", target="add", context=object())

        assert set(call.describe()) == {
            "kind",
            "target",
            "arguments",
            "effect",
            "run_id",
            "path",
            "name",
            "local",
        }

    def test_the_field_does_not_affect_equality(self) -> None:
        assert EffectCall(kind="step", target="a", context=object()) == EffectCall(
            kind="step", target="a", context=object()
        )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrentCallsDoNotShareState:
    async def test_each_call_gets_its_own_context(self, rt) -> None:
        """`ctx.gather` runs steps concurrently; one call's decision must not
        leak into another's, which a shared context would allow."""

        @step
        async def slow(n: int) -> int:
            await asyncio.sleep(0.01)
            return n

        @workflow(name="parallel")
        async def parallel(ctx: Context, _n: int) -> list[int]:
            return await ctx.gather(ctx.step(slow, 1), ctx.step(slow, 2))

        rt.register(parallel)
        contexts: list[int] = []

        @rt.hooks.before_step(target="slow")
        async def watch(ctx: HookContext) -> None:
            contexts.append(id(ctx))

        await rt.run(parallel, 0)

        assert len(set(contexts)) == 2, "two concurrent calls shared a context"


class TestRegistryIsPerRuntime:
    def test_two_runtimes_do_not_share_middleware(self) -> None:
        """Deliberately unlike toolsets and nodes, which chain to a global
        catalog. Those answer "what exists?"; this answers "what does this
        deployment enforce?", and sharing that means one test's gate silently
        applies to another's run."""
        first = Runtime(store=MemoryStore())
        second = Runtime(store=MemoryStore())

        @first.hooks.before_step
        async def only_here(ctx: HookContext) -> None: ...

        assert len(first.hooks) == 1
        assert len(second.hooks) == 0
        assert not isinstance(second.broker, HookBroker)

    def test_the_registry_names_what_is_registered(self, rt) -> None:
        """Recorded on the run, per the versioning decision: middleware is run
        provenance, never part of the workflow's identity."""

        @rt.hooks.before_step
        async def audit(ctx: HookContext) -> None: ...

        @rt.hooks.after_step
        async def report(ctx: HookContext) -> None: ...

        assert rt.hooks.names() == ["audit", "report"]


class TestAskParksTheRun:
    """The one thing Loom can do here that a request-scoped middleware cannot.

    Every surveyed system resolves an "ask" by blocking, prompting, or refusing.
    Loom parks: the run costs nothing while it waits, the answer is journaled,
    and the resumed run performs the call exactly once. That falls out of
    machinery that already exists rather than needing any of its own.
    """

    async def test_it_parks_instead_of_refusing(self, rt) -> None:
        ran: list[int] = []

        @step
        async def transfer(amount: int) -> str:
            ran.append(amount)
            return f"sent {amount}"

        @workflow(name="payment")
        async def payment(ctx: Context, amount: int) -> str:
            return await ctx.step(transfer, amount)

        rt.register(payment)

        @rt.hooks.before_step(target="transfer")
        async def needs_a_human(ctx: HookContext) -> None:
            ctx.ask("over threshold")

        result = await rt.run(payment, 5000)
        record = await rt.get(result.run_id)

        assert result.status.value == "suspended"
        assert record.awaiting_event == "approval:hook:transfer"
        assert ran == [], "the call happened before anyone answered"

    async def test_an_approval_lets_it_through_exactly_once(self, rt) -> None:
        ran: list[int] = []

        @step
        async def transfer(amount: int) -> str:
            ran.append(amount)
            return f"sent {amount}"

        @workflow(name="payment2")
        async def payment2(ctx: Context, amount: int) -> str:
            return await ctx.step(transfer, amount)

        rt.register(payment2)

        @rt.hooks.before_step(target="transfer")
        async def needs_a_human(ctx: HookContext) -> None:
            ctx.ask("over threshold")

        parked = await rt.run(payment2, 5000)
        await rt.approve(parked.run_id, "hook:transfer")
        final = await rt.wait(parked.run_id, timeout=5)

        assert final.status.value == "completed"
        assert final.output == "sent 5000"
        # The body re-entered and the hook asked again — but `wait_for_approval`
        # found the journaled answer rather than parking a second time.
        assert ran == [5000], "the call ran more than once across the park"

    async def test_a_rejection_refuses(self, rt) -> None:
        ran: list[int] = []

        @step
        async def transfer(amount: int) -> str:
            ran.append(amount)
            return "sent"

        @workflow(name="payment3")
        async def payment3(ctx: Context, amount: int) -> str:
            return await ctx.step(transfer, amount)

        rt.register(payment3)

        @rt.hooks.before_step(target="transfer")
        async def needs_a_human(ctx: HookContext) -> None:
            ctx.ask("over threshold")

        parked = await rt.run(payment3, 5000)
        await rt.approve(parked.run_id, "hook:transfer", approved=False)
        final = await rt.wait(parked.run_id, timeout=5)

        assert final.status.value == "failed"
        assert ran == []

    async def test_an_ask_with_nobody_to_ask_refuses(self) -> None:
        """A gate that could not run has not passed. Proceeding because there
        was no workflow context to park would be the one outcome an `ask` must
        never produce."""
        from loom.runtime.hooks import HookBroker, HookRegistry

        registry = HookRegistry()

        @registry.before_step
        async def asks(ctx: HookContext) -> None:
            ctx.ask("needs review")

        broker = HookBroker(DirectBroker(), registry)

        async def perform() -> str:
            return "performed"

        outcome = await broker.dispatch(
            EffectCall(kind="step", target="add", perform=perform),
            __import__("loom.security.authority", fromlist=["Authority"]).Authority(),
        )

        assert outcome.ok is False
        assert "no workflow context" in (outcome.error or "")


# ---------------------------------------------------------------------------
# Phase 2 — the body family
# ---------------------------------------------------------------------------


class TestBodyHooksRunOnReplay:
    """The opposite property from the effect family, and the reason the two
    are different types rather than more events on one."""

    async def test_they_fire_on_every_body_entry(self, rt) -> None:
        entries: list[bool] = []

        @rt.hooks.on_workflow_start
        async def started(ctx) -> None:
            entries.append(ctx.re_entry)

        result = await rt.run(one_step, 1)
        await rt.replay(result.run_id)

        assert entries == [False, True], "a body hook must see its re-entry"

    async def test_re_entry_distinguishes_started_from_resumed(self, rt) -> None:
        """The signal that stops a logger emitting a duplicate "run started"
        every time a parked run wakes up."""
        seen: list[bool] = []

        @step
        async def pause(_n: int) -> str:
            return "done"

        @workflow(name="parks")
        async def parks(ctx: Context, n: int) -> str:
            await ctx.wait_for_event("go")
            return await ctx.step(pause, n)

        rt.register(parks)

        @rt.hooks.on_workflow_start
        async def started(ctx) -> None:
            seen.append(ctx.re_entry)

        parked = await rt.run(parks, 1)
        await rt.send_event(parked.run_id, "go", {})
        await rt.wait(parked.run_id, timeout=5)

        assert seen[0] is False
        assert True in seen[1:], "the resume looked like a fresh start"

    async def test_end_names_how_the_body_exited(self, rt) -> None:
        """Parking is not failure, and a body hook is the one place that is
        visible without re-reading the record afterwards."""
        statuses: list[str] = []

        @rt.hooks.on_workflow_end
        async def ended(ctx) -> None:
            statuses.append(ctx.status)

        await rt.run(one_step, 1)
        await rt.run(failing, 1)

        assert statuses == ["completed", "failed"]

    async def test_a_parked_body_reports_suspended_not_failed(self, rt) -> None:
        statuses: list[str] = []

        @workflow(name="waits")
        async def waits(ctx: Context, _n: int) -> str:
            await ctx.wait_for_event("never")
            return "done"

        rt.register(waits)

        @rt.hooks.on_workflow_end
        async def ended(ctx) -> None:
            statuses.append(ctx.status)

        await rt.run(waits, 1)

        assert statuses == ["suspended"]

    async def test_end_carries_the_output(self, rt) -> None:
        outputs: list[object] = []

        @rt.hooks.on_workflow_end
        async def ended(ctx) -> None:
            outputs.append(ctx.output)

        await rt.run(one_step, 41)

        assert outputs == [42]


class TestABodyHookCannotDecide:
    """Structural, not conventional. If a body hook could refuse, a replay
    would re-derive an outcome from configuration that has since changed —
    and the answer to "are hooks part of the version?" would flip."""

    def test_the_context_has_no_deny_or_ask(self) -> None:
        from loom.runtime.hooks import BodyContext

        ctx = BodyContext(run_id="r", workflow="w", attempt=1, re_entry=False)

        assert not hasattr(ctx, "deny")
        assert not hasattr(ctx, "ask")
        assert not hasattr(ctx, "decision")

    async def test_a_body_hook_that_raises_does_not_break_the_run(self, rt) -> None:
        """Always fails open. A broken logger must not become a failed
        workflow — and worse, one that fails only where that logger is
        installed."""

        @rt.hooks.on_workflow_start
        async def broken(ctx) -> None:
            raise RuntimeError("bad logger")

        result = await rt.run(one_step, 41)

        assert result.status.value == "completed"
        assert result.output == 42


class TestBodyHooksCostNothingWhenUnused:
    async def test_no_body_hooks_means_no_wrapper(self, rt) -> None:
        """`_invoke_body` short-circuits on `has_body`, so the run path is
        untouched for everyone not using this."""
        assert rt.hooks.has_body is False

        @rt.hooks.before_step
        async def effect_only(ctx: HookContext) -> None: ...

        assert rt.hooks.has_body is False, "an effect hook enabled the body path"


# ---------------------------------------------------------------------------
# Phase 3 — the agent family
# ---------------------------------------------------------------------------


class TestAgentHooks:
    """Observational and mutational, never decisional — the decisions are
    already covered by effect hooks on `kind="agent"` and `kind="tool"`."""

    def test_the_context_cannot_decide(self) -> None:
        from loom.runtime.hooks import AgentHookContext

        ctx = AgentHookContext(agent_name="a")

        assert not hasattr(ctx, "deny")
        assert not hasattr(ctx, "ask")

    def test_stop_is_not_a_refusal(self) -> None:
        """Ending the loop and refusing a call are different things, and
        calling both "deny" would describe one of them badly."""
        from loom.runtime.hooks import AgentHookContext

        ctx = AgentHookContext(agent_name="a", turn=3)
        ctx.stop("no progress in three turns")

        assert ctx.stopped is True
        assert ctx.stop_reason == "no progress in three turns"

    def test_the_events_are_registered_in_firing_order(self) -> None:
        from loom.runtime.hooks import _AGENT_EVENTS

        assert _AGENT_EVENTS == (
            "agent_start",
            "turn_start",
            "model_start",
            "model_end",
            "turn_end",
            "agent_end",
        )

    async def test_dispatch_fails_open(self) -> None:
        """A compaction middleware that throws leaves the messages alone and
        the turn proceeds — degraded honestly, rather than turning a working
        agent into a failed step."""
        from loom.runtime.hooks import AgentHookContext, HookRegistry

        registry = HookRegistry()

        @registry.on_model_start
        async def broken(ctx) -> None:
            raise RuntimeError("compaction failed")

        ctx = AgentHookContext(agent_name="a", messages=[1, 2, 3])
        await registry.dispatch_agent("model_start", ctx)

        assert ctx.messages == [1, 2, 3]
        assert ctx.metadata["hook_errors"]

    async def test_messages_are_shaped_in_place(self) -> None:
        """The single largest category of middleware in every system surveyed.
        Rebinding does nothing; mutating is the contract."""
        from loom.runtime.hooks import AgentHookContext, HookRegistry

        registry = HookRegistry()

        @registry.on_model_start
        async def trim(ctx) -> None:
            del ctx.messages[:-1]

        messages = [1, 2, 3, 4]
        await registry.dispatch_agent(
            "model_start", AgentHookContext(agent_name="a", messages=messages)
        )

        assert messages == [4], "the hook shaped a copy, not the real list"

    def test_an_unknown_event_lists_all_three_families(self) -> None:
        registry = HookRegistry()

        with pytest.raises(AttributeError) as caught:
            registry.on_nonsense  # noqa: B018

        message = str(caught.value)
        assert "effect:" in message
        assert "body:" in message
        assert "agent:" in message


class TestTurnsPairExactlyOnce:
    """Every exit from the loop — three returns and any raise — must produce
    exactly one `turn_end` per `turn_start`."""

    async def test_beginning_a_turn_closes_the_previous_one(self) -> None:
        from loom.agents.runner import _Turns
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()
        events: list[tuple[str, int]] = []

        @registry.on_turn_start
        async def started(ctx) -> None:
            events.append(("start", ctx.turn))

        @registry.on_turn_end
        async def ended(ctx) -> None:
            events.append(("end", ctx.turn))

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])
        await turns.begin(2, [])
        await turns.close()

        assert events == [
            ("start", 1),
            ("end", 1),
            ("start", 2),
            ("end", 2),
        ]

    async def test_closing_twice_fires_once(self) -> None:
        from loom.agents.runner import _Turns
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()
        ends: list[int] = []

        @registry.on_turn_end
        async def ended(ctx) -> None:
            ends.append(ctx.turn)

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])
        await turns.close()
        await turns.close()

        assert ends == [1]


# ---------------------------------------------------------------------------
# Phase 4 — adapters and recording
# ---------------------------------------------------------------------------


class TestGuardrailAdapter:
    """An adapter, not a migration. `Agent(guardrails=[...])` keeps working —
    it has to, because an agent can run with no Runtime and therefore no
    registry to fall back on."""

    async def test_a_rejecting_guardrail_denies_the_call(self, rt) -> None:
        from loom.agents.guardrails import allow, guardrail, reject

        @step
        async def send(text: str) -> str:
            return f"sent {text}"

        @workflow(name="messaging")
        async def messaging(ctx: Context, msg: str) -> str:
            return await ctx.step(send, text=msg)

        rt.register(messaging)

        @guardrail
        def no_secrets(arguments: dict):
            return (
                reject("looks like a credential")
                if "password" in str(arguments).lower()
                else allow()
            )

        rt.hooks.use_guardrail(no_secrets, event="step")

        clean = await rt.run(messaging, "hello")
        blocked = await rt.run(messaging, "my password is hunter2")

        assert clean.status.value == "completed"
        assert blocked.status.value == "failed"
        assert "no_secrets" in (blocked.error.message if blocked.error else "")

    async def test_a_tripwire_aborts_the_run(self, rt) -> None:
        from loom.agents.guardrails import guardrail, tripwire

        @guardrail
        def always_trips(arguments: dict):
            return tripwire("policy violation")

        rt.hooks.use_guardrail(always_trips, event="step")

        result = await rt.run(one_step, 1)

        assert result.status.value == "failed"

    def test_the_registration_is_named_after_the_guardrail(self, rt) -> None:
        from loom.agents.guardrails import allow, guardrail

        @guardrail
        def house_rules(arguments: dict):
            return allow()

        rt.hooks.use_guardrail(house_rules)

        assert rt.hooks.names() == ["guardrail:house_rules"]


class TestMiddlewareIsRecordedOnTheRun:
    """Per Q10: middleware is run provenance, never workflow identity. Folding
    it into `content_hash` would give one commit as many versions as it has
    environments."""

    async def test_the_active_middleware_is_recorded(self, rt) -> None:
        @rt.hooks.before_step
        async def audit(ctx: HookContext) -> None: ...

        result = await rt.run(one_step, 1)
        record = await rt.get(result.run_id)

        assert record.metadata["loom.middleware"] == ["audit"]

    async def test_nothing_is_recorded_when_nothing_is_registered(self, rt) -> None:
        result = await rt.run(one_step, 1)
        record = await rt.get(result.run_id)

        assert "loom.middleware" not in record.metadata

    async def test_it_does_not_reach_the_workflow_version(self, rt) -> None:
        """The load-bearing half. A logging middleware must not fork the
        workflow."""

        @rt.hooks.before_step
        async def audit(ctx: HookContext) -> None: ...

        await rt.publish(one_step, source="# code\n")
        first = await rt.published()

        @rt.hooks.before_step
        async def another(ctx: HookContext) -> None: ...

        await rt.publish(one_step, source="# code\n")
        second = await rt.published()

        assert [w.code_hash for w in first] == [w.code_hash for w in second]


class TestThePublicSurfaceIsComplete:
    """Every type a hook author has to name must be exported.

    A context class that exists but is not in ``__all__`` is one a user
    annotates by reaching into a private path — or, more likely, does not
    annotate at all. The three context types are the whole vocabulary of this
    module.
    """

    def test_every_context_type_is_exported(self) -> None:
        import loom.runtime.hooks as module

        for name in ("HookContext", "BodyContext", "AgentHookContext"):
            assert name in module.__all__, f"{name} is reachable but undeclared"
            assert hasattr(module, name)

    def test_the_decision_type_and_the_broker_are_exported(self) -> None:
        import loom.runtime.hooks as module

        assert {"Decision", "HookBroker", "HookRegistry", "Next"} <= set(module.__all__)

    def test_everything_declared_actually_exists(self) -> None:
        import loom.runtime.hooks as module

        missing = [n for n in module.__all__ if not hasattr(module, n)]
        assert not missing, f"declared but absent: {missing}"


# ---------------------------------------------------------------------------
# Corner cases
# ---------------------------------------------------------------------------


class TestStopEndsTheTurnLoop:
    """`ctx.stop()` was a dead API before this: it set state nobody read.

    Worse than a missing feature — a documented call that silently does
    nothing. The loop's only other early exit (the turn budget) raises, so this
    does too: a partial result handed back quietly would make "a stall detector
    gave up on this" indistinguishable from "the agent finished".
    """

    async def test_a_turn_end_hook_can_end_the_loop(self) -> None:
        from loom.agents.runner import _Turns
        from loom.core.exceptions import AgentStopped
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()

        @registry.on_turn_end
        async def give_up(ctx) -> None:
            ctx.stop("no progress")

        turns = _Turns(registry, "researcher", "r1")
        await turns.begin(1, [])
        await turns.begin(2, [])  # closes turn 1, which asks to stop

        with pytest.raises(AgentStopped) as caught:
            turns.check(2)

        assert caught.value.turn == 1
        assert caught.value.reason == "no progress"
        assert "researcher" in str(caught.value)

    async def test_a_model_end_hook_can_end_the_loop(self) -> None:
        from loom.agents.runner import _Turns
        from loom.core.exceptions import AgentStopped
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()

        @registry.on_model_end
        async def over_budget(ctx) -> None:
            ctx.stop("spend ceiling")

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])
        await turns.after_model(1, [], response=None)

        with pytest.raises(AgentStopped, match="spend ceiling"):
            turns.check(2)

    async def test_the_turn_being_observed_is_not_abandoned(self) -> None:
        """A hook asking to stop is asking for the loop to end *after* this
        turn, not for the turn it is watching to be cut off halfway."""
        from loom.agents.runner import _Turns
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()

        @registry.on_turn_end
        async def give_up(ctx) -> None:
            ctx.stop("enough")

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])

        # Nothing raised while turn 1 is still open.
        turns.check(1)

        await turns.close()
        assert turns.stopped is True

    async def test_no_stop_means_no_raise(self) -> None:
        from loom.agents.runner import _Turns
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()

        @registry.on_turn_end
        async def quiet(ctx) -> None: ...

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])
        await turns.begin(2, [])
        turns.check(2)  # must not raise

    async def test_the_first_stop_wins(self) -> None:
        """Escalate-once, like the decision model: a later hook cannot restate
        the reason and overwrite why the loop is ending."""
        from loom.agents.runner import _Turns
        from loom.runtime.hooks import HookRegistry

        registry = HookRegistry()

        @registry.on_turn_end
        async def first(ctx) -> None:
            ctx.stop("stalled")

        @registry.on_turn_end
        async def second(ctx) -> None:
            ctx.stop("something else")

        turns = _Turns(registry, "a", "r1")
        await turns.begin(1, [])
        await turns.close()

        assert turns.stop_reason == "stalled"


class TestHookCornerCases:
    async def test_an_around_hook_that_raises_fails_the_call(self, rt) -> None:
        """`around` owns its own control flow, so its exception is the call's.

        Unlike `before`, which is compiled into a gate and fails closed, and
        `after`, which is compiled into an observer and fails open — an
        `around` middleware *is* the call path, so there is nothing to fail
        open or closed toward.
        """

        @rt.hooks.around_step(target="add")
        async def broken(ctx: HookContext, next) -> int:
            raise RuntimeError("middleware exploded")

        result = await rt.run(one_step, 1)

        assert result.status.value == "failed"

    async def test_after_may_set_the_result_to_none(self, rt) -> None:
        """A falsy replacement must not be mistaken for "no replacement"."""

        @rt.hooks.after_step(target="add")
        async def blank(ctx: HookContext) -> None:
            ctx.result = None

        result = await rt.run(one_step, 41)

        assert result.status.value == "completed"
        assert result.output is None

    async def test_nested_around_hooks_compose_outermost_first(self, rt) -> None:
        order: list[str] = []

        @rt.hooks.around_step(target="add")
        async def outer(ctx: HookContext, next) -> int:
            order.append("outer in")
            value = await next()
            order.append("outer out")
            return value

        @rt.hooks.around_step(target="add")
        async def inner(ctx: HookContext, next) -> int:
            order.append("inner in")
            value = await next()
            order.append("inner out")
            return value

        await rt.run(one_step, 1)

        assert order == ["outer in", "inner in", "inner out", "outer out"]

    async def test_metadata_carries_from_before_to_after(self, rt) -> None:
        """One context object per call, so a risk score computed once in
        `before` is readable in `after` without a side table."""
        seen: list[object] = []

        @rt.hooks.before_step(target="add")
        async def score(ctx: HookContext) -> None:
            ctx.metadata["risk"] = "low"

        @rt.hooks.after_step(target="add")
        async def read(ctx: HookContext) -> None:
            seen.append(ctx.metadata.get("risk"))

        await rt.run(one_step, 1)

        assert seen == ["low"]

    async def test_a_registry_with_no_owner_never_installs_a_broker(self) -> None:
        """A standalone registry — used in tests and by the adapters — must not
        try to reach a Runtime that does not exist."""
        registry = HookRegistry()

        @registry.before_step
        async def noop(ctx: HookContext) -> None: ...

        assert len(registry) == 1

    async def test_a_service_that_raises_on_stop_does_not_block_shutdown(
        self,
    ) -> None:
        """`shutdown` stops supervised services best-effort: one that throws
        must not strand the others or the scheduler behind it."""

        class Angry:
            async def stop(self) -> None:
                raise RuntimeError("will not stop")

        stopped: list[str] = []

        class Polite:
            async def stop(self) -> None:
                stopped.append("polite")

        runtime = Runtime(store=MemoryStore())
        angry, polite = Angry(), Polite()
        runtime.supervise(angry)
        runtime.supervise(polite)

        await runtime.shutdown(drain=0)

        assert stopped == ["polite"]

    async def test_shutdown_is_safe_to_call_twice(self) -> None:
        runtime = Runtime(store=MemoryStore())
        await runtime.shutdown(drain=0)
        await runtime.shutdown(drain=0)


class TestBodyHookCornerCases:
    async def test_an_abandoned_body_is_reported_as_abandoned(self, rt) -> None:
        """The status a cancelled drive produces — Ctrl+C, SIGTERM, or
        `shutdown` cancelling it. Not `failed`: nobody formed an opinion about
        the run, which is why the record stays RUNNING and reclaimable."""
        statuses: list[str] = []

        @rt.hooks.on_workflow_end
        async def ended(ctx) -> None:
            statuses.append(ctx.status)

        gate = asyncio.Event()

        @step
        async def blocks(_n: int) -> str:
            gate.set()
            await asyncio.sleep(30)
            return "done"

        @workflow(name="abandoned")
        async def abandoned(ctx: Context, n: int) -> str:
            return await ctx.step(blocks, n)

        rt.register(abandoned)
        task = asyncio.create_task(rt.run(abandoned, 1))
        await asyncio.wait_for(gate.wait(), timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert statuses == ["abandoned"]

    async def test_a_rotated_body_is_not_reported_as_completed(self, rt) -> None:
        """`continue_as_new` ends this body and starts a successor. Reporting
        it as `completed` would make a forever-flow look like it finished every
        time it rotated."""
        statuses: list[str] = []

        @rt.hooks.on_workflow_end
        async def ended(ctx) -> None:
            statuses.append(ctx.status)

        @workflow(name="rotates")
        async def rotates(ctx: Context, n: int) -> str:
            if n < 2:
                await ctx.continue_as_new(n + 1)
            return "settled"

        rt.register(rotates)
        await rt.run(rotates, 1)

        assert "rotated" in statuses
