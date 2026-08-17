"""The effect broker: every durable operation passes through one seam.

Three things are being established here.

**That the seam exists at all** — no operation reaches the outside world
without a broker seeing it. That is tested by installing a broker that records
and asserting on what it saw, rather than by trusting the call sites.

**That enforcement is per dispatch, not per resolution.** A grant applied when
tools were handed out can be outlived by the tool it produced; the sibling-
operation test below is exactly that case, and it fails if the check moves back
to resolution time.

**That the default costs effectively nothing.** ``DirectBroker`` is on every
run, including the ones nobody asked to be guarded, so its overhead is a
correctness property and not a nice-to-have.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from loom import Context, Retry, Runtime, step, workflow
from loom.runtime.effects import (
    DirectBroker,
    EffectBroker,
    EffectCall,
    EffectDenied,
    EffectResult,
    GuardedBroker,
)
from loom.security.authority import Authority
from loom.security.grants import GrantSet
from loom.stores import MemoryStore
from loom.toolsets.manifest import EffectClass


class RecordingBroker:
    """A ``DirectBroker`` that remembers what it was asked to do."""

    def __init__(self) -> None:
        self.seen: list[EffectCall] = []

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        self.seen.append(call)
        if call.perform is None:
            return EffectResult(ok=False, error="nothing to perform")
        return EffectResult(value=await call.perform())


@step(name="broker_add")
async def broker_add(a: int, b: int) -> int:
    return a + b


@step(name="broker_boom")
async def broker_boom() -> str:
    raise RuntimeError("upstream is down")


@workflow(name="broker_flow")
async def broker_flow(ctx: Context, payload: int = 1) -> int:
    first = await ctx.step(broker_add, payload, 1)
    return await ctx.step(broker_add, first, 10)


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_the_protocol_is_satisfied_by_both_adapters() -> None:
    """Design rule 1: a port with one adapter is a guess."""
    assert isinstance(DirectBroker(), EffectBroker)
    assert isinstance(GuardedBroker(), EffectBroker)


async def test_a_bare_runtime_gets_the_cheap_broker() -> None:
    assert isinstance(Runtime().broker, DirectBroker)


async def test_every_durable_operation_reaches_the_broker() -> None:
    broker = RecordingBroker()
    rt = Runtime(store=MemoryStore(), broker=broker)
    rt.register(broker_flow)

    result = await rt.run(broker_flow, 1)

    assert result.output == 12
    assert [(call.kind, call.target) for call in broker.seen] == [
        ("step", "broker_add"),
        ("step", "broker_add"),
    ]
    assert {call.run_id for call in broker.seen} == {result.run_id}
    assert [call.path for call in broker.seen] == ["0", "1"]


async def test_replay_dispatches_nothing() -> None:
    """A journaled result is served from the journal, so no effect recurs.

    Which also means a replay consumes none of an authority's budget — the
    rehearsal is free in permissions as well as in side effects.
    """
    broker = RecordingBroker()
    rt = Runtime(store=MemoryStore(), broker=broker)
    rt.register(broker_flow)

    original = await rt.run(broker_flow, 1)
    broker.seen.clear()
    replayed = await rt.replay(original.run_id)

    assert replayed.output == 12
    assert broker.seen == []


async def test_a_retried_step_is_one_effect_not_three() -> None:
    """The broker weighs operations; the retry policy owns attempts."""
    broker = RecordingBroker()
    rt = Runtime(store=MemoryStore(), broker=broker)

    @workflow(name="broker_retry_flow")
    async def flow(ctx: Context, _: Any = None) -> str:
        return await ctx.step(broker_boom, retry=3)

    rt.register(flow)
    result = await rt.run(flow, None)

    assert result.status.value == "failed"
    assert len(broker.seen) == 1
    journal = await rt.history(result.run_id)
    assert journal[0].attempts == 3


# ---------------------------------------------------------------------------
# Grants, per dispatch
# ---------------------------------------------------------------------------


def _tool_call(target: str, effect: EffectClass = EffectClass.WRITE) -> EffectCall:
    async def perform() -> str:
        return "did it"

    return EffectCall(kind="tool", target=target, effect=effect, perform=perform)


async def test_a_sibling_operation_of_a_granted_toolset_is_refused() -> None:
    """The exit criterion: one operation granted does not grant its neighbour."""
    broker = GuardedBroker()
    who = Authority(grant=GrantSet(toolsets=["jira.issues:read"]))

    allowed = await broker.dispatch(
        _tool_call("jira.issues.search", EffectClass.READ), who
    )
    assert allowed.ok

    refused = await broker.dispatch(_tool_call("jira.issues.create"), who)
    assert not refused.ok
    assert "jira.issues.create" in refused.error
    assert refused.needs == "jira.issues.create:write"
    # The denial says what would have allowed it, and what is held instead.
    assert "jira.issues:read" in refused.error


async def test_an_empty_grant_permits_everything() -> None:
    """Otherwise configuring a broker would break every existing Runtime.

    Deny-by-default applies *within* a dimension the caller has spoken to, not
    to a caller who has declared no policy at all.
    """
    broker = GuardedBroker()
    assert (await broker.dispatch(_tool_call("jira.issues.create"), Authority())).ok


async def test_a_denial_does_not_consume_the_budget() -> None:
    broker = GuardedBroker(max_calls=2)
    who = Authority(grant=GrantSet(toolsets=["jira:read"]))

    for _ in range(5):
        await broker.dispatch(_tool_call("jira.issues.create"), who)

    assert broker.dispatched == 0
    assert (await broker.dispatch(_tool_call("jira.issues.get", EffectClass.READ), who)).ok


async def test_a_call_ceiling_halts_a_loop() -> None:
    broker = GuardedBroker(max_calls=3)
    performed = [
        (await broker.dispatch(_tool_call("svc.ping", EffectClass.READ), Authority())).ok
        for _ in range(10)
    ]

    assert performed.count(True) == 3
    last = await broker.dispatch(_tool_call("svc.ping", EffectClass.READ), Authority())
    assert "call ceiling reached" in last.error
    assert last.needs == "max_calls > 3"


async def test_a_dry_run_reads_and_refuses_to_write() -> None:
    broker = GuardedBroker()
    who = Authority(dry_run=True)

    read = await broker.dispatch(_tool_call("crm.contacts.get", EffectClass.READ), who)
    write = await broker.dispatch(_tool_call("crm.contacts.upsert"), who)
    destroy = await broker.dispatch(
        _tool_call("crm.contacts.purge", EffectClass.DESTRUCTIVE), who
    )

    assert read.ok and read.value == "did it"
    assert not write.ok and "dry run" in write.error
    assert not destroy.ok and "destructive" in destroy.error


async def test_an_unclassified_operation_is_treated_as_a_write() -> None:
    """Failing closed. An effect class nobody declared is not evidence of safety."""
    assert EffectCall(kind="tool", target="x.y").writes
    refused = await GuardedBroker().dispatch(
        EffectCall(kind="tool", target="x.y", perform=None), Authority(dry_run=True)
    )
    assert not refused.ok


@pytest.mark.parametrize(
    ("kind", "field", "target"),
    [("agent", "agents", "triage"), ("child", "subflows", "sub_flow")],
)
async def test_agents_and_subflows_are_granted_by_name(
    kind: str, field: str, target: str
) -> None:
    async def perform() -> str:
        return "ok"

    who = Authority(grant=GrantSet(**{field: [target]}))
    call = EffectCall(kind=kind, target=target, perform=perform)
    other = EffectCall(kind=kind, target="something_else", perform=perform)

    assert (await GuardedBroker().dispatch(call, who)).ok
    refused = await GuardedBroker().dispatch(other, who)
    assert not refused.ok
    assert "something_else" in refused.error


# ---------------------------------------------------------------------------
# strict grants: closing the "undeclared dimension" fail-open hole
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["agent", "child"])
async def test_a_toolset_only_grant_leaves_other_dimensions_open_by_default(
    kind: str,
) -> None:
    """The bug this plan exists to fix, pinned down before the fix for it.

    A grant that only ever mentions ``toolsets`` says nothing about agents or
    sub-workflows — and saying nothing is not the same as forbidding, unless
    ``strict`` says otherwise.
    """
    async def perform() -> str:
        return "ok"

    who = Authority(grant=GrantSet(toolsets=["jira:read"]))
    call = EffectCall(kind=kind, target="anything", perform=perform)

    assert (await GuardedBroker().dispatch(call, who)).ok


@pytest.mark.parametrize("kind", ["agent", "child"])
async def test_strict_closes_an_undeclared_dimension(kind: str) -> None:
    async def perform() -> str:
        return "ok"

    who = Authority(grant=GrantSet(toolsets=["jira:read"], strict=True))
    call = EffectCall(kind=kind, target="anything", perform=perform)

    refused = await GuardedBroker().dispatch(call, who)
    assert not refused.ok
    assert "anything" in refused.error
    assert refused.needs == "anything"


async def test_strict_closes_the_toolset_dimension_when_undeclared() -> None:
    who = Authority(grant=GrantSet(agents=["triage"], strict=True))
    refused = await GuardedBroker().dispatch(_tool_call("jira.issues.get"), who)
    assert not refused.ok


async def test_strict_still_permits_what_is_explicitly_granted() -> None:
    """Strict changes the default for undeclared dimensions; it does not
    touch enforcement of the dimensions that *are* declared."""
    who = Authority(grant=GrantSet(toolsets=["jira:read"], strict=True))
    allowed = await GuardedBroker().dispatch(
        _tool_call("jira.issues.search", EffectClass.READ), who
    )
    assert allowed.ok


def _dotted_step_call(target: str, effect: EffectClass = EffectClass.WRITE) -> EffectCall:
    async def perform() -> str:
        return "did it"

    return EffectCall(kind="step", target=target, effect=effect, perform=perform)


async def test_a_dotted_step_target_is_grant_checked_like_a_tool() -> None:
    """A bridged step (one `@step` fronting a dynamic tool registry, since the
    operations are not known until a task resolves its tools) names its
    semantic operation via `ctx.step(fn, name="jira.create_issue", ...)`
    rather than `ctx.tool(...)`. Without this branch that call reaches
    `GuardedBroker` as `kind="step"`, which `_check_grant` fell through
    unchecked -- a toolset-scoped grant would never apply to it."""
    who = Authority(grant=GrantSet(toolsets=["jira:write"]))

    allowed = await GuardedBroker().dispatch(_dotted_step_call("jira.create_issue"), who)
    assert allowed.ok

    refused = await GuardedBroker().dispatch(_dotted_step_call("slack.send_message"), who)
    assert not refused.ok
    assert "slack.send_message" in refused.error
    assert refused.needs == "slack.send_message:write"


async def test_an_undotted_step_target_is_not_grant_checked() -> None:
    """An ordinary local `@step` (no manifest, no toolset) is not a
    declaration a grant can speak to -- it must stay unchecked exactly as it
    was before the dotted-step branch existed."""
    who = Authority(grant=GrantSet(toolsets=["jira:write"], strict=True))
    allowed = await GuardedBroker().dispatch(_dotted_step_call("format_address"), who)
    assert allowed.ok


async def test_strict_with_nothing_declared_denies_everything() -> None:
    """``is_empty`` must not swallow ``strict`` — a strict grant with no
    entries anywhere means deny-everything, the opposite of unrestricted."""
    async def perform() -> str:
        return "ok"

    who = Authority(grant=GrantSet(strict=True))
    assert not (await GuardedBroker().dispatch(_tool_call("jira.issues.get"), who)).ok
    assert not (
        await GuardedBroker().dispatch(EffectCall(kind="agent", target="x", perform=perform), who)
    ).ok
    assert who.is_unrestricted is False


# ---------------------------------------------------------------------------
# Through a real run
# ---------------------------------------------------------------------------


async def test_a_denied_step_fails_the_run_and_names_the_grant() -> None:
    class DenyEverything:
        async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
            return EffectResult(
                ok=False, error=f"'{call.target}' is not granted", needs=call.target
            )

    rt = Runtime(store=MemoryStore(), broker=DenyEverything())
    rt.register(broker_flow)

    result = await rt.run(broker_flow, 1)

    assert result.status.value == "failed"
    assert "broker_add" in result.error.message
    assert "not granted" in result.error.message


async def test_a_denial_is_journaled_so_replay_sees_it() -> None:
    """A refusal that left no trace would replay as if it never happened."""

    class DenyWrites:
        async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
            return EffectResult(ok=False, error="denied", needs=call.target)

    rt = Runtime(store=MemoryStore(), broker=DenyWrites())
    rt.register(broker_flow)
    result = await rt.run(broker_flow, 1)

    journal = await rt.history(result.run_id)
    assert [entry.status.value for entry in journal] == ["failed"]
    assert "denied" in journal[0].error.message

    # And the replay reproduces it without asking the broker again.
    rt.broker = DirectBroker()
    replayed = await rt.replay(result.run_id)
    assert replayed.status.value == "failed"


async def test_a_workflow_grant_narrows_but_the_runtime_wins() -> None:
    """A workflow can limit itself; it cannot widen what the Runtime allows."""
    from loom.runtime.context import _authority_for

    @workflow(name="broker_granted_flow", grants=GrantSet(agents=["triage"]))
    async def granted(ctx: Context, _: Any = None) -> str:
        return "ok"

    rt = Runtime(store=MemoryStore())
    rt.register(granted)

    # The Runtime declared nothing: the workflow's own declaration applies.
    assert _authority_for(rt, granted).grant.agents == ["triage"]

    # The Runtime declared something: that is what gets enforced.
    rt.authority = Authority(grant=GrantSet(agents=["auditor"]))
    assert _authority_for(rt, granted).grant.agents == ["auditor"]


async def test_a_dry_run_is_carried_into_every_dispatch() -> None:
    """Set once on the Runtime, weighed at each effect the body performs."""
    rt = Runtime(
        store=MemoryStore(),
        broker=GuardedBroker(),
        authority=Authority(dry_run=True),
    )
    rt.register(broker_flow)

    result = await rt.run(broker_flow, 1)

    assert result.status.value == "failed"
    assert "dry run" in result.error.message


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


async def test_the_default_broker_does_not_tax_the_common_case() -> None:
    """Exit criterion: the seam must not tax a run that asked for no policy.

    Measured on the seam itself rather than through a whole run. Two earlier
    attempts are why.

    Timing complete runs put the broker's cost — a dataclass and an extra await
    — underneath a journal write and a store round trip that are three orders
    of magnitude larger, so the number that came back was the machine's mood.
    It reported a 57% regression once and a 6% improvement the next time, from
    identical code. Interleaving the samples fixed the bias but not the
    variance: the signal simply is not there to be measured at that scale.

    So this measures ``dispatch`` against calling the same coroutine directly,
    and asserts an absolute per-call budget. That is a real bound — a broker
    that grew a lock, a copy, or an await on anything external moves this by
    one to two orders of magnitude, not by percent — and it is stable enough
    to run on a shared machine. The percentage the design cares about follows
    from it: a few microseconds against a step doing real I/O is a fraction of
    one percent.
    """
    budget_us = 50.0
    iterations = 20_000

    broker, who = DirectBroker(), Authority()

    async def perform() -> int:
        return 42

    async def bare() -> None:
        for _ in range(iterations):
            await perform()

    async def brokered() -> None:
        for _ in range(iterations):
            await broker.dispatch(
                EffectCall(kind="step", target="x", perform=perform), who
            )

    async def elapsed(work: Any) -> float:
        start = time.perf_counter()
        await work()
        return time.perf_counter() - start

    baseline: list[float] = []
    through: list[float] = []
    for _ in range(5):
        baseline.append(await elapsed(bare))
        through.append(await elapsed(brokered))

    per_call_us = (min(through) - min(baseline)) / iterations * 1e6
    assert per_call_us < budget_us, (
        f"DirectBroker.dispatch costs {per_call_us:.1f}us per call, "
        f"budget is {budget_us:.0f}us"
    )


def test_a_result_carries_its_refusal_when_unwrapped() -> None:
    with pytest.raises(EffectDenied) as caught:
        EffectResult(ok=False, error="nope", needs="jira:read").unwrap()
    assert caught.value.needs == "jira:read"


def test_a_call_describes_itself_without_its_closure() -> None:
    """The wire projection is what a remote broker receives in P3."""

    async def perform() -> None: ...

    call = EffectCall(
        kind="tool",
        target="jira.issues.search",
        arguments={"q": "x"},
        effect=EffectClass.READ,
        run_id="run_1",
        path="3",
        perform=perform,
    )

    assert call.describe() == {
        "kind": "tool",
        "target": "jira.issues.search",
        "arguments": {"q": "x"},
        "effect": "read",
        "run_id": "run_1",
        "path": "3",
        "name": None,
        "local": False,
    }
    assert call.toolset == "jira"
    assert call.operation == "issues.search"
    # Two calls differing only in how they would be performed are the same call.
    assert call == EffectCall(**{**call.describe(), "effect": EffectClass.READ})


# ---------------------------------------------------------------------------
# Through a real agent
# ---------------------------------------------------------------------------


async def test_an_agent_is_refused_a_sibling_operation_mid_turn() -> None:
    """The whole point of moving the check to dispatch time.

    The agent is handed *both* tools — resolution does not narrow anything here
    — and the grant is enforced when it reaches for the second one. That is the
    case a resolution-time check cannot cover: an agent holds its tools for the
    length of the turn loop, and a check that happened before the loop started
    has no say in what happens inside it.

    The refusal comes back as a tool result rather than an exception, so the
    agent can adapt, and the transcript records why.
    """
    from loom.agents.agent import Agent
    from loom.agents.messages import ToolCall
    from loom.agents.tool_registry import Toolset
    from loom.testing import MockModelProvider, mock_response

    @step(name="search")
    async def search(query: str) -> str:
        """Find issues.

        Args:
            query: What to look for.
        """
        return f"found {query}"

    @step(name="create")
    async def create(title: str) -> str:
        """Create an issue.

        Args:
            title: The summary.
        """
        return f"created {title}"

    toolset = Toolset.from_steps(
        "jira",
        [search, create],
        effects={"search": EffectClass.READ, "create": EffectClass.WRITE},
    )
    tools = [toolset.resolve("search"), toolset.resolve("create")]
    assert [t.metadata["toolset"] for t in tools] == ["jira", "jira"]

    model = MockModelProvider(
        responses=[
            mock_response(
                tool_calls=[ToolCall(id="1", name="search", arguments={"query": "bugs"})]
            ),
            mock_response(
                tool_calls=[ToolCall(id="2", name="create", arguments={"title": "x"})]
            ),
            mock_response("done"),
        ]
    )
    agent = Agent(name="triage", model=model, tools=tools)

    rt = Runtime(
        store=MemoryStore(),
        broker=GuardedBroker(),
        authority=Authority(grant=GrantSet(toolsets=["jira:read"])),
    )

    @workflow(name="broker_agent_flow")
    async def flow(ctx: Context, _: Any = None) -> str:
        return str(await ctx.agent(agent, "triage the backlog"))

    rt.register(flow)
    result = await rt.run(flow, None)

    assert result.status.value == "completed"
    journal = await rt.history(result.run_id)
    transcript = str(journal[0].output)
    assert "found bugs" in transcript
    assert "jira.create" in transcript
    assert "not granted" in transcript
    assert "created x" not in transcript


class TestADenialReplaysAsADenial:
    """A refusal must come back as a refusal, not as a generic step failure.

    ``_resolve`` journals a denial as FAILED so that "a replay sees what the
    run saw" — but it rebuilt the failure from ``recorded.error.message``
    alone, so the *type* was lost and ``EffectDenied`` came back as
    ``StepError``. A workflow that tells "policy refused this" from "this
    broke" therefore took a different branch on replay, which is precisely the
    divergence journaling the denial was meant to prevent.

    Narrow on purpose: the engine reproduces the exception types *it* produces,
    because it knows how to rebuild them. A failure raised by user code stays a
    ``StepError`` carrying its message, because reconstructing an arbitrary
    exception means guessing at a constructor.
    """

    @staticmethod
    def _refusing_runtime(store):
        from loom.runtime.effects import DirectBroker, EffectResult

        class Refuses(DirectBroker):
            async def dispatch(self, call, authority):
                if call.target == "danger":
                    return EffectResult(
                        ok=False,
                        error="refused by policy",
                        needs="billing.charge:write",
                    )
                return await super().dispatch(call, authority)

        return Runtime(store=store, broker=Refuses())

    async def test_the_type_survives_a_replay(self) -> None:
        from loom.runtime.effects import EffectDenied

        @step
        async def danger(n: int) -> int:
            return n * 100

        @workflow(name="typed_denial")
        async def typed_denial(ctx: Context, n: int) -> str:
            try:
                await ctx.step(danger, n)
                return "ran"
            except EffectDenied:
                return "policy"
            except Exception as exc:
                return f"broke ({type(exc).__name__})"

        store = MemoryStore()
        strict = self._refusing_runtime(store)
        strict.register(typed_denial)
        first = await strict.run(typed_denial, 1)

        # Replayed by a Runtime with no policy at all: the recorded denial is
        # the truth, not the middleware that is installed now.
        plain = Runtime(store=store)
        plain.register(typed_denial)
        replayed = await plain.replay(first.run_id)

        assert first.output == "policy"
        assert replayed.output == "policy", "a denial replayed as a plain failure"

    async def test_the_reconstructed_denial_still_names_the_fix(self) -> None:
        """``needs`` is the actionable half — "add this grant and it works".
        A replayed denial without it is a worse object than the original."""
        from loom.runtime.effects import EffectDenied

        seen: list[str] = []

        @step
        async def danger(n: int) -> int:
            return n

        @workflow(name="needs_denial")
        async def needs_denial(ctx: Context, n: int) -> str:
            try:
                await ctx.step(danger, n)
            except EffectDenied as denied:
                seen.append(denied.needs)
            return "done"

        store = MemoryStore()
        strict = self._refusing_runtime(store)
        strict.register(needs_denial)
        first = await strict.run(needs_denial, 1)

        plain = Runtime(store=store)
        plain.register(needs_denial)
        await plain.replay(first.run_id)

        assert seen == ["billing.charge:write", "billing.charge:write"]

    async def test_retries_exhausted_replays_as_retries_exhausted(self) -> None:
        """The second row in the same table.

        A step that spent its retry budget raises ``RetriesExhausted``, and a
        replay rebuilt it as a plain ``StepError`` — so a workflow branching on
        "this was transient and we gave up" took a different path the second
        time.

        Safe to widen an existing replay this way because ``RetriesExhausted``
        **is** a ``StepError``: anything already catching the base class keeps
        catching it, unlike ``EffectDenied``, which is not.
        """
        from loom.core.exceptions import RetriesExhausted

        @step(retry=Retry(max_attempts=3, initial_delay=0.01))
        async def flaky(n: int) -> int:
            raise ConnectionError("upstream is down")

        @workflow(name="exhausting")
        async def exhausting(ctx: Context, n: int) -> str:
            try:
                await ctx.step(flaky, n)
                return "ok"
            except RetriesExhausted as exc:
                return f"gave up after {exc.attempts}"
            except Exception as exc:
                return f"other ({type(exc).__name__})"

        store = MemoryStore()
        rt = Runtime(store=store)
        rt.register(exhausting)
        first = await rt.run(exhausting, 1)
        replayed = await rt.replay(first.run_id)

        assert first.output == "gave up after 3"
        assert replayed.output == "gave up after 3"

    async def test_the_replayed_message_matches_the_original(self) -> None:
        """The reconstruction rebuilds the wrapper's message too.

        The journal records the *original* error — ``ConnectionError`` and its
        text — because that is the useful thing to keep. So the
        ``RetriesExhausted`` wrapper around it has to be rebuilt rather than
        read, and a mismatch here would surface as two different log lines for
        one failure.
        """
        from loom.core.exceptions import RetriesExhausted

        seen: list[str] = []

        @step(retry=Retry(max_attempts=2, initial_delay=0.01))
        async def flaky(n: int) -> int:
            raise ConnectionError("upstream is down")

        @workflow(name="message_match")
        async def message_match(ctx: Context, n: int) -> str:
            try:
                await ctx.step(flaky, n)
            except RetriesExhausted as exc:
                seen.append(str(exc))
            return "done"

        store = MemoryStore()
        rt = Runtime(store=store)
        rt.register(message_match)
        first = await rt.run(message_match, 1)
        await rt.replay(first.run_id)

        assert len(seen) == 2
        assert seen[0] == seen[1], "the replayed failure read differently"
        assert "failed after 2 attempts" in seen[0]

    async def test_a_step_that_never_retried_is_not_reported_as_exhausted(
        self,
    ) -> None:
        """The condition is ``attempts > 1``, matching the engine's own rule.

        With retries off, the run raises the step's *original* exception rather
        than ``RetriesExhausted`` — so claiming a retry budget was spent would
        invent an attempt that never happened.
        """
        from loom.core.exceptions import RetriesExhausted

        @step(retry=Retry(max_attempts=1))
        async def once(n: int) -> int:
            raise ConnectionError("down")

        @workflow(name="single_attempt")
        async def single_attempt(ctx: Context, n: int) -> str:
            try:
                await ctx.step(once, n)
                return "ok"
            except RetriesExhausted:
                return "exhausted"
            except Exception as exc:
                return type(exc).__name__

        store = MemoryStore()
        rt = Runtime(store=store)
        rt.register(single_attempt)
        first = await rt.run(single_attempt, 1)
        replayed = await rt.replay(first.run_id)

        assert first.output == "ConnectionError"
        assert replayed.output == "StepError", (
            "a single attempt must not be dressed up as an exhausted retry"
        )

    async def test_a_user_exception_still_widens_to_a_step_error_on_replay(
        self,
    ) -> None:
        """The gap this fix does **not** close, asserted so it stays visible.

        A ``ValueError`` raised inside a step reaches the workflow as itself on
        the first run and as a ``StepError`` on replay, so a workflow catching
        its own domain exception still branches differently the second time.
        The journal even records ``type: "ValueError"`` — what is missing is not
        the information but a safe way to use it, since rebuilding an arbitrary
        exception means guessing at a constructor and the class may not be
        importable in the process doing the replay.

        Closing it properly is a different piece of work (a declared, opt-in
        replayable-error contract), and pretending to close it by calling
        ``cls(message)`` would fail on any exception whose ``__init__`` takes
        more than a string — silently, and only for the workflows unlucky
        enough to have one.
        """

        @step
        async def broken(n: int) -> int:
            raise ValueError("genuinely broken")

        @workflow(name="ordinary")
        async def ordinary(ctx: Context, n: int) -> str:
            try:
                await ctx.step(broken, n)
                return "ran"
            except Exception as exc:
                return type(exc).__name__

        store = MemoryStore()
        rt = Runtime(store=store)
        rt.register(ordinary)
        first = await rt.run(ordinary, 1)
        replayed = await rt.replay(first.run_id)

        assert first.output == "ValueError"
        assert replayed.output == "StepError"

        # The type is recorded; only the reconstruction is missing.
        entries = await store.load_journal(first.run_id)
        assert entries[0].error is not None
        assert entries[0].error.type == "ValueError"
