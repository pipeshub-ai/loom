"""Read-to-write taint, as a broker decorator.

The rule: once a run has read data it did not bring with it, a destructive call
needs a human. It is a property of *generated* code in general, not of any one
platform — a workflow that searches the web and then deletes tickets has taken
instructions from something nobody reviewed.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.runtime.effects import DirectBroker, EffectCall, EffectResult
from loom.runtime.taint import TaintBroker, TaintPolicy, TaintState
from loom.security.authority import Authority
from loom.toolsets.manifest import EffectClass

AUTHORITY = Authority()


def _call(kind: str, target: str, effect: EffectClass, run: str = "run-1") -> EffectCall:
    async def perform() -> str:
        return f"{target} done"

    return EffectCall(
        kind=kind, target=target, effect=effect, run_id=run, perform=perform
    )


@pytest.fixture
def broker() -> TaintBroker:
    return TaintBroker(DirectBroker())


class TestTheRule:
    async def test_a_write_before_any_read_is_allowed(self, broker) -> None:
        outcome = await broker.dispatch(
            _call("tool", "jira.create", EffectClass.WRITE), AUTHORITY
        )
        assert outcome.ok is True

    async def test_a_write_after_an_external_read_is_refused(self, broker) -> None:
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        outcome = await broker.dispatch(
            _call("tool", "jira.create", EffectClass.WRITE), AUTHORITY
        )
        assert outcome.ok is False
        assert "read external data" in outcome.error
        assert "web.search" in outcome.error, "the message must name what tainted it"
        assert outcome.needs == "approval"

    async def test_a_destructive_call_after_a_read_is_refused(self, broker) -> None:
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        outcome = await broker.dispatch(
            _call("tool", "jira.delete", EffectClass.DESTRUCTIVE), AUTHORITY
        )
        assert outcome.ok is False

    async def test_further_reads_stay_allowed(self, broker) -> None:
        """Taint restricts what a run may *do*, not what it may learn."""
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        outcome = await broker.dispatch(
            _call("tool", "jira.search", EffectClass.READ), AUTHORITY
        )
        assert outcome.ok is True

    async def test_a_refusal_returns_rather_than_raises(self, broker) -> None:
        """Matching every other broker: the caller decides how a refusal
        surfaces — a step failure, a message back to an agent, or handled."""
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        outcome = await broker.dispatch(
            _call("tool", "x.write", EffectClass.WRITE), AUTHORITY
        )
        assert isinstance(outcome, EffectResult) and outcome.ok is False


class TestApprovalClearsIt:
    async def test_an_approval_event_clears_the_taint(self, broker) -> None:
        """The escape hatch that keeps the rule usable.

        The answer to "this workflow legitimately writes after reading" is a
        person saying so, not disabling the check.
        """
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        assert not (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok

        await broker.dispatch(
            _call("event", "approval:refund", EffectClass.READ), AUTHORITY
        )
        assert (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok

    async def test_a_read_after_approval_taints_again(self, broker) -> None:
        """Approval covers what was read before it, not everything forever."""
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        await broker.dispatch(
            _call("event", "approval:ok", EffectClass.READ), AUTHORITY
        )
        await broker.dispatch(_call("tool", "web.other", EffectClass.READ), AUTHORITY)

        outcome = await broker.dispatch(
            _call("tool", "x.write", EffectClass.WRITE), AUTHORITY
        )
        assert outcome.ok is False
        assert "web.other" in outcome.error

    async def test_a_non_approval_event_does_not_clear(self, broker) -> None:
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        await broker.dispatch(_call("event", "webhook.hit", EffectClass.READ), AUTHORITY)
        assert not (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok


class TestScopeAndPolicy:
    async def test_taint_is_per_run(self, broker) -> None:
        """A run is the unit a workflow author reasons about; leaking taint
        across runs would make one workflow's read block another's write."""
        await broker.dispatch(
            _call("tool", "web.search", EffectClass.READ, run="run-1"), AUTHORITY
        )
        outcome = await broker.dispatch(
            _call("tool", "x.write", EffectClass.WRITE, run="run-2"), AUTHORITY
        )
        assert outcome.ok is True

    async def test_a_refused_read_does_not_taint(self) -> None:
        """A read that was denied brought nothing in. Tainting on it would let
        a *denied* call restrict everything downstream."""

        class Refusing:
            async def dispatch(self, call: EffectCall, authority: Authority) -> Any:
                if call.effect is EffectClass.READ:
                    return EffectResult(ok=False, error="denied")
                return EffectResult(ok=True, value="written")

        broker = TaintBroker(Refusing())
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        assert (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok

    async def test_exempt_targets_neither_taint_nor_are_blocked(self) -> None:
        """A workflow's writes *about itself* — reports, its own artifacts —
        are not the exfiltration path this guards."""
        broker = TaintBroker(
            DirectBroker(), TaintPolicy(exempt=frozenset({"artifact:report"}))
        )
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        assert (
            await broker.dispatch(
                _call("artifact", "report", EffectClass.WRITE), AUTHORITY
            )
        ).ok

    async def test_destructive_can_be_blocked_while_writes_are_allowed(self) -> None:
        """The two have very different false-positive rates: nearly every
        useful workflow writes after reading, few need to delete."""
        broker = TaintBroker(DirectBroker(), TaintPolicy(block_writes=False))
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)

        assert (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok
        assert not (
            await broker.dispatch(
                _call("tool", "x.delete", EffectClass.DESTRUCTIVE), AUTHORITY
            )
        ).ok

    async def test_approval_clearing_can_be_turned_off(self) -> None:
        broker = TaintBroker(DirectBroker(), TaintPolicy(approval_clears=False))
        await broker.dispatch(_call("tool", "web.search", EffectClass.READ), AUTHORITY)
        await broker.dispatch(
            _call("event", "approval:x", EffectClass.READ), AUTHORITY
        )
        # The approval did not clear it, so the write is still refused.
        assert not (
            await broker.dispatch(_call("tool", "x.write", EffectClass.WRITE), AUTHORITY)
        ).ok

    def test_state_is_inspectable_and_forgettable(self, broker) -> None:
        state = broker.state_for("run-1")
        assert isinstance(state, TaintState) and state.tainted is False
        state.taint("tool:web.search")
        assert broker.state_for("run-1").tainted is True
        broker.forget_run("run-1")
        assert broker.state_for("run-1").tainted is False


class TestItComposes:
    async def test_it_wraps_and_is_wrapped(self) -> None:
        """A decorator on the chain, not a new concept in the engine — so
        grants, budgets, dry-run, and taint all apply through one path.

        Taint on the outside: it decides *whether* to dispatch, so it must sit
        above whatever performs the effect.
        """
        from loom.runtime.effects import GuardedBroker

        chained = TaintBroker(GuardedBroker())
        outcome = await chained.dispatch(
            _call("tool", "x.read", EffectClass.READ), AUTHORITY
        )
        assert outcome.ok is True

    async def test_a_runtime_without_it_is_unchanged(self) -> None:
        from loom import Context, Runtime, workflow
        from loom.stores.memory import MemoryStore

        @workflow(name="untainted")
        async def untainted(ctx: Context, _: Any = None) -> str:
            return "fine"

        made = Runtime(store=MemoryStore())
        made.register(untainted)
        assert (await made.run(untainted)).output == "fine"

    async def test_a_runtime_with_it_still_runs(self) -> None:
        from loom import Context, Runtime, step, workflow
        from loom.stores.memory import MemoryStore

        @step
        async def look() -> str:
            return "seen"

        @workflow(name="tainted_flow")
        async def tainted_flow(ctx: Context, _: Any = None) -> str:
            return await ctx.step(look)

        made = Runtime(store=MemoryStore(), broker=TaintBroker(DirectBroker()))
        made.register(tainted_flow)
        assert (await made.run(tainted_flow)).output == "seen"
