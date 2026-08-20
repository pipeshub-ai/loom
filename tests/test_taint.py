"""Read-to-write taint, as a broker decorator.

The rule: once a run has read data it did not bring with it, a destructive call
needs a human. It is a property of *generated* code in general, not of any one
platform — a workflow that searches the web and then deletes tickets has taken
instructions from something nobody reviewed.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom import ExecutionStatus, Runtime, step, workflow
from loom.runtime.effects import DirectBroker, EffectCall, EffectResult
from loom.runtime.taint import TaintBroker, TaintPolicy, TaintState
from loom.security.authority import Authority
from loom.stores.memory import MemoryStore
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


class TestOpenWorldIsWhatTaints:
    """A READ is evidence the run touched the world only if it *reached* the
    world. Twenty of the twenty-six built-in nodes are READ and most are pure
    computation, so keying on the class alone made filtering an in-memory list
    refuse the next write — naming `step:control.filter` as the external data
    it had read."""

    @pytest.mark.asyncio
    async def test_a_closed_world_node_does_not_taint(self) -> None:
        """The regression. Filter a list the run was handed, then write."""
        from loom.nodes.control import FilterIn
        from loom.nodes.registry import load_builtin_nodes

        load_builtin_nodes()

        @step
        async def post_to_slack(msg: str) -> str:
            return "posted"

        @workflow
        async def flow(ctx: Any, _in: Any = None) -> str:
            await ctx.node(
                "control.filter",
                FilterIn(items=[{"a": 1}, {"a": 2}], where={"a": 1}),
            )
            return await ctx.step(post_to_slack, msg="hi")

        runtime = Runtime(store=MemoryStore(), broker=TaintBroker(DirectBroker()))
        runtime.register(flow)
        result = await runtime.run(flow, None)
        assert result.status is ExecutionStatus.COMPLETED, getattr(
            result, "error", None
        )
        assert result.output == "posted"

    @pytest.mark.asyncio
    async def test_an_open_world_read_still_taints(self) -> None:
        """The property the rule exists for, unchanged."""
        broker = TaintBroker(DirectBroker())

        async def perform() -> str:
            return "untrusted content"

        await broker.dispatch(
            EffectCall(
                kind="step", target="exa_search", effect=EffectClass.READ,
                open_world=True, run_id="r1", perform=perform,
            ),
            Authority(),
        )
        refused = await broker.dispatch(
            EffectCall(
                kind="step", target="jira_delete_issue",
                effect=EffectClass.DESTRUCTIVE, run_id="r1", perform=perform,
            ),
            Authority(),
        )
        assert refused.ok is False
        assert refused.needs == "approval"

    @pytest.mark.asyncio
    async def test_a_closed_world_read_does_not(self) -> None:
        broker = TaintBroker(DirectBroker())

        async def perform() -> str:
            return "local"

        await broker.dispatch(
            EffectCall(
                kind="step", target="control.filter", effect=EffectClass.READ,
                open_world=False, run_id="r2", perform=perform,
            ),
            Authority(),
        )
        allowed = await broker.dispatch(
            EffectCall(
                kind="step", target="post", effect=EffectClass.WRITE,
                run_id="r2", perform=perform,
            ),
            Authority(),
        )
        assert allowed.ok is True

    def test_an_unclassified_call_is_assumed_to_reach_the_world(self) -> None:
        """Same direction as `effect` defaulting to WRITE: the assumption that
        adds a refusal, never one that removes it."""
        assert EffectCall(kind="step", target="x").open_world is True

    @pytest.mark.asyncio
    async def test_a_journal_without_the_field_keeps_the_old_meaning(self) -> None:
        """A run journalled before `open_world` existed cannot say, and
        assuming 'open' preserves every refusal it would already have seen."""
        from loom.runtime.journal import EntryKind, EntryStatus, Journal, JournalEntry

        journal = Journal(
            [
                JournalEntry(
                    path="0",
                    kind=EntryKind.STEP,
                    name="exa_search",
                    status=EntryStatus.COMPLETED,
                    metadata={"effect_class": EffectClass.READ},  # no open_world
                )
            ]
        )
        broker = TaintBroker(DirectBroker())
        broker.observe_run("old", journal)
        assert broker.state_for("old").tainted is True


class TestTheNarrowDials:
    """`block_writes` is the strict dial and almost every useful workflow
    writes after reading — so a deployment that finds it unusable turns it off
    and then has nothing. These two say the useful thing instead: after reading
    the open web you may update a record, but you may not send the email or
    share the folder."""

    @staticmethod
    async def _taint(broker: TaintBroker, run_id: str) -> None:
        async def perform() -> str:
            return "untrusted content from the open web"

        await broker.dispatch(
            EffectCall(
                kind="step", target="exa_search", effect=EffectClass.READ,
                open_world=True, run_id=run_id, perform=perform,
            ),
            AUTHORITY,
        )

    @staticmethod
    def _call(target: str, **kw: Any) -> EffectCall:
        async def perform() -> str:
            return "done"

        return EffectCall(kind="step", target=target, perform=perform, **kw)

    @pytest.mark.asyncio
    async def test_writes_allowed_but_the_send_is_not(self) -> None:
        """The policy the whole phase exists to make expressible."""
        broker = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(
                block_writes=False,
                block_destructive=False,
                block_irreversible=True,
            ),
        )
        await self._taint(broker, "r")

        ordinary = await broker.dispatch(
            self._call(
                "jira_update_issue", effect=EffectClass.WRITE,
                open_world=True, reversible=True, run_id="r",
            ),
            AUTHORITY,
        )
        assert ordinary.ok is True, "an ordinary reversible write should pass"

        send = await broker.dispatch(
            self._call(
                "gmail_send_message", effect=EffectClass.WRITE,
                open_world=True, reversible=False, run_id="r",
            ),
            AUTHORITY,
        )
        assert send.ok is False
        assert send.needs == "approval"
        assert "irreversible" in (send.error or "")

    @pytest.mark.asyncio
    async def test_the_gmail_inversion_is_resolved(self) -> None:
        """Ranked by class, trashing outranks sending. Ranked by what a human
        is actually being asked, it does not: untrash restores a message for
        thirty days and nothing unsends an email."""
        broker = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(
                block_writes=False, block_destructive=False, block_irreversible=True
            ),
        )
        await self._taint(broker, "r")

        trash = await broker.dispatch(
            self._call(
                "gmail_trash_message", effect=EffectClass.DESTRUCTIVE,
                open_world=True, reversible=True, run_id="r",
            ),
            AUTHORITY,
        )
        send = await broker.dispatch(
            self._call(
                "gmail_send_message", effect=EffectClass.WRITE,
                open_world=True, reversible=False, run_id="r",
            ),
            AUTHORITY,
        )
        assert trash.ok is True, "the recoverable destructive op is permitted"
        assert send.ok is False, "the irreversible write is not"

    @pytest.mark.asyncio
    async def test_access_control_can_be_blocked_on_its_own(self) -> None:
        """Sharing exfiltrates without writing anything to the thing shared,
        and reads as an ordinary additive write."""
        broker = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(
                block_writes=False, block_destructive=False,
                block_access_control=True,
            ),
        )
        await self._taint(broker, "r")

        write = await broker.dispatch(
            self._call("drive_upload_file", effect=EffectClass.WRITE, run_id="r"),
            AUTHORITY,
        )
        share = await broker.dispatch(
            self._call(
                "drive_share_file", effect=EffectClass.WRITE,
                access_control=True, run_id="r",
            ),
            AUTHORITY,
        )
        assert write.ok is True
        assert share.ok is False
        assert "access-control" in (share.error or "")

    @pytest.mark.asyncio
    async def test_both_dials_are_off_by_default(self) -> None:
        """Populating `reversible` across a deployment's own toolsets is work
        nobody has done yet, and turning this on before that reads every
        operation as irreversible."""
        policy = TaintPolicy()
        assert policy.block_irreversible is False
        assert policy.block_access_control is False

    @pytest.mark.asyncio
    async def test_a_dial_can_only_add_refusals(self) -> None:
        """Checked as well as the class, never instead of it — so enabling one
        cannot make a previously-refused call succeed."""
        strict = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(block_writes=True, block_irreversible=True),
        )
        await self._taint(strict, "r")
        result = await strict.dispatch(
            self._call(
                "jira_update_issue", effect=EffectClass.WRITE,
                reversible=True, run_id="r",
            ),
            AUTHORITY,
        )
        assert result.ok is False, "block_writes still applies"

    @pytest.mark.asyncio
    async def test_an_untainted_run_is_untouched(self) -> None:
        """These are taint dials. A run that has read nothing external may
        still send email — that is what `ctx.wait_for_approval` is for."""
        broker = TaintBroker(DirectBroker(), policy=TaintPolicy(block_irreversible=True))
        result = await broker.dispatch(
            self._call(
                "gmail_send_message", effect=EffectClass.WRITE,
                open_world=True, reversible=False, run_id="clean",
            ),
            AUTHORITY,
        )
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_a_dial_never_refuses_the_read_itself(self) -> None:
        """Every read is "irreversible" under the literal definition — nothing
        un-reads. Applying the dial to reads would refuse the very call that
        taints, stopping the run at step one."""
        broker = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(
                block_writes=False,
                block_destructive=False,
                block_irreversible=True,
                block_access_control=True,
            ),
        )
        await self._taint(broker, "r")
        second_read = await broker.dispatch(
            self._call(
                "exa_search", effect=EffectClass.READ,
                open_world=True, reversible=False, access_control=True, run_id="r",
            ),
            AUTHORITY,
        )
        assert second_read.ok is True


class TestAgentToolCallsCarryTheFacets:
    """A tool call inside an agent loop reaches the same broker a `ctx.step`
    does — and an agent deciding to send an email is the case the narrow dials
    exist for. Until the facets travelled with it, the tool dispatch carried
    only `effect`, so `block_irreversible` saw every agent tool call as
    irreversible and refused all of them rather than the right ones."""

    def test_a_resolved_tool_carries_every_facet(self) -> None:
        from loom.agents.tool_registry import Toolset
        from loom.agents.tools import Tool
        from loom.toolsets.google.gmail.manifest import GMAIL_MANIFEST

        toolset = Toolset(
            GMAIL_MANIFEST,
            lambda op_id: Tool(fn=lambda: None, name=op_id, metadata={}),
        )
        send = toolset.resolve("messages.send").metadata
        trash = toolset.resolve("messages.trash").metadata

        assert send["effect"] is EffectClass.WRITE
        assert send["reversible"] is False, "nothing unsends an email"
        assert trash["effect"] is EffectClass.DESTRUCTIVE
        assert trash["reversible"] is True, "untrash restores it for 30 days"
        assert send["open_world"] is True

    @pytest.mark.asyncio
    async def test_the_dial_reaches_an_agents_tool_call(self) -> None:
        broker = TaintBroker(
            DirectBroker(),
            policy=TaintPolicy(
                block_writes=False, block_destructive=False, block_irreversible=True
            ),
        )

        async def perform() -> str:
            return "x"

        await broker.dispatch(
            EffectCall(
                kind="tool", target="exa.search", effect=EffectClass.READ,
                open_world=True, run_id="r", perform=perform,
            ),
            AUTHORITY,
        )
        update = await broker.dispatch(
            EffectCall(
                kind="tool", target="jira.update", effect=EffectClass.WRITE,
                open_world=True, reversible=True, run_id="r", perform=perform,
            ),
            AUTHORITY,
        )
        send = await broker.dispatch(
            EffectCall(
                kind="tool", target="gmail.send", effect=EffectClass.WRITE,
                open_world=True, reversible=False, run_id="r", perform=perform,
            ),
            AUTHORITY,
        )
        assert update.ok is True, "the agent may still update a record"
        assert send.ok is False and send.needs == "approval"


class TestAskingAPersonIsNeverRefused:
    """The rule must not block its own exit.

    ``approval_clears`` is described as "the escape hatch that keeps the rule
    usable: the answer to 'this workflow legitimately needs to write after
    reading' is a person saying so, not turning the check off". Every
    ``human.*`` node is ``WRITE`` and ``open_world`` — both accurate, it leaves
    the process and records an answer — so a tainted run could not reach the
    person whose approval was the only thing that would clear it. A deadlock,
    not a policy.

    Found by the phase 13 acid test, and it had two halves: the node call and
    the ``deliver:`` call inside it. Exempting only the outer one left the
    deadlock exactly where it was, which is why both are asserted here.
    """

    @staticmethod
    def _channel():
        from loom.nodes.human.channel import DeliveryReceipt

        class Recording:
            name = "recording"

            def __init__(self) -> None:
                self.delivered: list[Any] = []

            async def deliver(self, request: Any) -> Any:
                self.delivered.append(request)
                return DeliveryReceipt(channel=self.name, delivered=True)

            async def withdraw(self, request_id: str, reference: str = "") -> None:
                return None

        return Recording()

    @staticmethod
    def _reader():
        """A node that reads the world and succeeds.

        Defined here rather than borrowed so this test needs no network and no
        other subsystem: what it requires is simply an ``open_world`` READ that
        completes, which is what taints a run.
        """
        from pydantic import BaseModel

        from loom.nodes.base import Node
        from loom.nodes.spec import NodeCategory, NodeSpec

        class In(BaseModel):
            pass

        class Out(BaseModel):
            text: str = ""

        class ReadTheWorld(Node[In, Out]):
            spec = NodeSpec(
                id="custom.read_the_world",
                category=NodeCategory.IO,
                summary="Read something external.",
                effect=EffectClass.READ,
                open_world=True,
                deterministic=False,
            )
            Input, Output = In, Out

            async def run(self, ctx: Any, payload: In) -> Out:
                return Out(text="something nobody reviewed")

        return ReadTheWorld

    async def test_a_tainted_run_can_still_ask(self) -> None:
        channel = self._channel()

        runtime = Runtime(store=MemoryStore(), human=channel,
                          broker=TaintBroker(DirectBroker(), TaintPolicy()))
        runtime.nodes.register_node(self._reader())

        @workflow(name="taint_then_ask")
        async def flow(ctx: Any, _input: Any) -> str:
            await ctx.node("custom.read_the_world", {})
            await ctx.node("human.approval", {"subject": "proceed"})
            return "asked"

        result = await runtime.run(flow, None)

        assert result.status is ExecutionStatus.SUSPENDED, (
            f"the approval was refused instead of parking: "
            f"{result.error and result.error.message}")
        assert channel.delivered, "nobody was actually asked"

    async def test_and_the_write_after_it_is_still_refused(self) -> None:
        """Exempting the ask must not exempt the acting.

        The whole rule would be pointless if letting a run *ask* also let it
        write before anyone answered.
        """
        runtime = Runtime(store=MemoryStore(), human=self._channel(),
                          broker=TaintBroker(DirectBroker(), TaintPolicy()))
        runtime.nodes.register_node(self._reader())

        @workflow(name="taint_then_write")
        async def flow(ctx: Any, _input: Any) -> str:
            await ctx.node("custom.read_the_world", {})
            await ctx.node("io.http_request",
                           {"url": "http://127.0.0.1:9/x", "method": "POST"})
            return "wrote"

        result = await runtime.run(flow, None)
        assert result.status is ExecutionStatus.FAILED
        assert result.error and "read external data" in result.error.message

    async def test_both_the_node_and_its_delivery_are_exempt(self) -> None:
        """The half that was missed the first time.

        A node's ``ctx.call`` inherits the node's classification, so exempting
        the node without its own work leaves the refusal one level down — and
        the symptom is identical.
        """
        seen: list[tuple[str, bool]] = []

        class Spy:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def dispatch(self, call: EffectCall,
                               authority: Authority) -> EffectResult:
                if call.target.startswith("human."):
                    seen.append((call.target, call.asks_human))
                return await self._inner.dispatch(call, authority)

        @workflow(name="ask_twice_dispatched")
        async def flow(ctx: Any, _input: Any) -> str:
            await ctx.node("human.approval", {"subject": "proceed"})
            return "asked"

        runtime = Runtime(store=MemoryStore(), human=self._channel(),
                          broker=Spy(DirectBroker()))
        await runtime.run(flow, None)

        assert len(seen) >= 2, "expected the node call and its delivery"
        assert all(flag for _, flag in seen), (
            f"a human call reached the broker without asks_human: {seen}")
