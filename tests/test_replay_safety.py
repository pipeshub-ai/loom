"""Replay must serve the value the call actually asked for.

The journal keys entries by position. Position is enough to find *an* entry;
it is not enough to prove the entry belongs to the call that found it. These
tests pin the two places that gap shows, plus the ingress gate that keeps a
malformed payload from becoming a run at all.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.core.exceptions import InputMismatch, NondeterminismError
from loom.runtime.journal import (
    CompatibilityMode,
    EntryKind,
    Journal,
    JournalEntry,
    VerifyMode,
)
from loom.stores.memory import MemoryStore


class Order(BaseModel):
    """Module scope on purpose.

    Under ``from __future__ import annotations`` a function-local model never
    resolves, so ``input_schema()`` and the engine's ``decode`` both fall back
    silently and the body receives a bare dict. That is a real sharp edge, but
    it is not the one under test here.
    """

    sku: str
    quantity: int

# ---------------------------------------------------------------------------
# Reordering two same-named calls
# ---------------------------------------------------------------------------


class TestArgumentVerification:
    """Position finds the entry; the fingerprint proves it is the right one."""

    def _journal(self, verify: VerifyMode) -> Journal:
        """A journal holding two `fetch` entries recorded in a-then-b order."""
        return Journal(
            [
                JournalEntry(
                    path="0",
                    kind=EntryKind.STEP,
                    name="fetch",
                    fingerprint="fp-a",
                    output="body-a",
                ),
                JournalEntry(
                    path="1",
                    kind=EntryKind.STEP,
                    name="fetch",
                    fingerprint="fp-b",
                    output="body-b",
                ),
            ],
            verify=verify,
        )

    def test_off_serves_the_wrong_value(self) -> None:
        """The behaviour that shipped, kept reachable so the change is visible."""
        journal = self._journal(VerifyMode.OFF)
        entry = journal.lookup("0", EntryKind.STEP, "fetch", fingerprint="fp-b")
        assert entry is not None
        assert entry.output == "body-a"  # asked for b, got a

    def test_warn_serves_the_value_and_flags_it(self) -> None:
        journal = self._journal(VerifyMode.WARN)
        entry = journal.lookup("0", EntryKind.STEP, "fetch", fingerprint="fp-b")
        assert entry is not None
        assert entry.metadata.get("argument_drift") is True

    def test_strict_refuses(self) -> None:
        journal = self._journal(VerifyMode.STRICT)
        with pytest.raises(NondeterminismError) as caught:
            journal.lookup("0", EntryKind.STEP, "fetch", fingerprint="fp-b")
        message = str(caught.value)
        assert "fetch" in message
        assert "arguments" in message

    def test_matching_arguments_replay_clean(self) -> None:
        journal = self._journal(VerifyMode.STRICT)
        entry = journal.lookup("0", EntryKind.STEP, "fetch", fingerprint="fp-a")
        assert entry is not None
        assert entry.output == "body-a"
        assert "argument_drift" not in entry.metadata

    def test_an_unrecorded_fingerprint_is_not_a_divergence(self) -> None:
        """Entries journaled before verification existed carry no fingerprint."""
        journal = Journal(
            [JournalEntry(path="0", kind=EntryKind.STEP, name="fetch", output="old")],
            verify=VerifyMode.STRICT,
        )
        entry = journal.lookup("0", EntryKind.STEP, "fetch", fingerprint="fp-new")
        assert entry is not None
        assert entry.output == "old"

    @pytest.mark.asyncio
    async def test_end_to_end_reorder_is_caught(self) -> None:
        """The defect as a user meets it: two calls to one step, swapped."""

        @step
        async def fetch(url: str) -> str:
            return f"body of {url}"

        @workflow(name="two_fetches")
        async def original(ctx: Context, _: object = None) -> str:
            first = await ctx.step(fetch, "https://a")
            second = await ctx.step(fetch, "https://b")
            return f"{first}|{second}"

        store = MemoryStore()
        rt = Runtime(store=store, verify=VerifyMode.STRICT)
        result = await rt.run(original)
        assert result.status is ExecutionStatus.COMPLETED

        @workflow(name="two_fetches")
        async def swapped(ctx: Context, _: object = None) -> str:
            second = await ctx.step(fetch, "https://b")
            first = await ctx.step(fetch, "https://a")
            return f"{first}|{second}"

        resumed = Runtime(store=store, verify=VerifyMode.STRICT)
        resumed.register(swapped)
        replayed = await resumed.replay(result.run_id)

        # A divergence inside the body is a failed run, not a raise out of
        # replay() — the engine records what went wrong where an operator can
        # read it. Before verification this replay "succeeded", serving each
        # call the other one's body.
        assert replayed.status is ExecutionStatus.FAILED
        assert replayed.error is not None
        assert "different arguments" in replayed.error.message

    @pytest.mark.asyncio
    async def test_warn_lets_the_same_reorder_through(self) -> None:
        """WARN keeps running, and says so on the entry.

        No longer the default — see :class:`TestVerificationDefaultsToStrict`
        — but still the setting for a workflow that deliberately derives a
        step's arguments from unjournaled state, so the behaviour is pinned
        as an explicit opt-in.
        """

        @step
        async def fetch_two(url: str) -> str:
            return f"body of {url}"

        @workflow(name="two_fetches_warn")
        async def original(ctx: Context, _: object = None) -> str:
            first = await ctx.step(fetch_two, "https://a")
            second = await ctx.step(fetch_two, "https://b")
            return f"{first}|{second}"

        store = MemoryStore()
        rt = Runtime(store=store, verify=VerifyMode.WARN)
        result = await rt.run(original)

        @workflow(name="two_fetches_warn")
        async def swapped(ctx: Context, _: object = None) -> str:
            second = await ctx.step(fetch_two, "https://b")
            first = await ctx.step(fetch_two, "https://a")
            return f"{first}|{second}"

        resumed = Runtime(store=store, verify=VerifyMode.WARN)
        resumed.register(swapped)
        replayed = await resumed.replay(result.run_id)
        assert replayed.status is ExecutionStatus.COMPLETED

        entries = await store.load_journal(replayed.run_id)
        assert any(e.metadata.get("argument_drift") for e in entries)


# ---------------------------------------------------------------------------
# Ingress validation
# ---------------------------------------------------------------------------


class TestIngressValidation:
    """A payload that cannot run should not become a run."""

    @pytest.mark.asyncio
    async def test_bad_payload_is_rejected_before_a_record_exists(self) -> None:
        @workflow(name="place_order")
        async def place_order(ctx: Context, order: Order) -> str:
            return order.sku

        store = MemoryStore()
        rt = Runtime(store=store)

        with pytest.raises(InputMismatch) as caught:
            await rt.run(place_order, {"sku": "abc"})

        assert "quantity" in str(caught.value)
        assert await store.list_executions() == []

    @pytest.mark.asyncio
    async def test_a_workflow_without_an_input_model_accepts_anything(self) -> None:
        @workflow(name="untyped")
        async def untyped(ctx: Context, payload: object = None) -> str:
            return type(payload).__name__

        rt = Runtime(store=MemoryStore())
        result = await rt.run(untyped, {"anything": [1, 2, 3]})
        assert result.status is ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a_valid_payload_still_runs(self) -> None:
        @workflow(name="place_order_ok")
        async def place_order(ctx: Context, order: Order) -> str:
            return f"{order.quantity}x{order.sku}"

        rt = Runtime(store=MemoryStore())
        result = await rt.run(place_order, {"sku": "abc", "quantity": 2})
        assert result.output == "2xabc"

    @pytest.mark.asyncio
    async def test_the_declared_model_is_accepted_as_itself(self) -> None:
        """Passing the input model the workflow declares must not be refused.

        A workflow annotated ``order: Order`` publishes an object schema, and
        an object schema accepted only ``dict``. So the most obvious call there
        is — hand it an ``Order`` — was rejected by an error that named the
        type it was refusing as the type it wanted: "takes object, but the
        input was Order", for a workflow that "expects order: Order".
        """
        @workflow(name="place_order_model")
        async def place_order(ctx: Context, order: Order) -> str:
            return f"{order.quantity}x{order.sku}"

        rt = Runtime(store=MemoryStore())
        result = await rt.run(place_order, Order(sku="abc", quantity=2))
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "2xabc"

    def test_a_model_is_not_a_free_pass_for_other_types(self) -> None:
        """The widening is for objects only — a model where a list is declared
        is still a mismatch, so this did not turn the check off."""
        from loom.runtime.validation import shape_error

        assert shape_error({"type": "array"}, Order(sku="a", quantity=1)) is not None
        assert shape_error({"type": "object"}, Order(sku="a", quantity=1)) is None
        assert shape_error({"type": "object"}, "not an object") is not None


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


class TestDefaultsUnchanged:
    def test_verification_defaults_to_strict(self) -> None:
        """It defaulted to WARN, and WARN served another call site's answer.

        The argument for WARN was that a difference is not always a bug — a
        step whose input comes from ``ctx.state`` legitimately replays with
        different arguments. What that argument missed is that the *common*
        cause of a difference was the engine's own path allocation racing under
        ``ctx.gather``, and warning there meant handing one branch the other's
        result and reporting the run ``completed``. With branch-local numbering
        the common cause is gone, so what is left really is a divergence.
        """
        rt = Runtime(store=MemoryStore())
        assert rt.verify is VerifyMode.STRICT

    def test_compatibility_mode_is_independent_of_verification(self) -> None:
        """Two separate axes: what diverged, and what to do about it."""
        journal = Journal(
            [JournalEntry(path="0", kind=EntryKind.STEP, name="a", fingerprint="x")],
            compatibility=CompatibilityMode.RESUME_FROM_DIVERGENCE,
            verify=VerifyMode.STRICT,
        )
        # A *shape* divergence still truncates rather than raising.
        assert journal.lookup("0", EntryKind.STEP, "b") is None


class TestIngressBoundaries:
    """Every surface renders the refusal in its own vocabulary."""

    def test_the_cli_reports_usage_not_failure(self) -> None:
        """Exit 2, not 1: a rejected payload left no run to have failed."""
        from loom.cli.commands import run_async
        from loom.cli.output import Exit

        async def boom() -> int:
            raise InputMismatch("missing required field 'quantity'")

        assert run_async(boom()) == Exit.USAGE

    @pytest.mark.asyncio
    async def test_http_maps_it_to_422(self) -> None:
        """422, not 400: the request parsed; the payload is not what this takes."""
        pytest.importorskip("fastapi")
        import httpx

        from loom.server.app import create_app

        @workflow(name="http_order")
        async def http_order(ctx: Context, order: Order) -> str:
            return order.sku

        rt = Runtime(store=MemoryStore())
        rt.register(http_order)
        transport = httpx.ASGITransport(app=create_app(rt))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://loom.test"
        ) as http:
            response = await http.post(
                "/runs", json={"workflow": "http_order", "input": {"sku": "abc"}}
            )

        assert response.status_code == 422
        assert "quantity" in response.text
        assert await rt.store.list_executions() == []

    @pytest.mark.asyncio
    async def test_the_escape_hatch_restores_the_old_behaviour(self) -> None:
        """A codebase whose annotations were never contracts can opt out."""

        @workflow(name="lenient")
        async def lenient(ctx: Context, order: Order) -> str:
            return str(order)

        rt = Runtime(store=MemoryStore(), validate_input=False)
        result = await rt.run(lenient, {"sku": "abc"})
        # It runs; the body decides what a partial payload means.
        assert result.run_id


class TestPositionalKeysInvalidateTheTail:
    """What an ordinal journal key costs when the workflow is edited.

    A path is allocated by order, so inserting a call shifts every later one.
    The entry now sitting at the shifted position records a different name, so
    ``lookup`` treats it as a divergence and ``RESUME_FROM_DIVERGENCE``
    truncates from there — discarding completed work that has nothing to do
    with the edit.

    Pinned as a measurement rather than a bug: this is the cost that a
    structural (call-site) key would remove, and the number is the argument.
    """

    @pytest.mark.asyncio
    async def test_inserting_one_step_re_executes_the_rest(self) -> None:
        executed: list[str] = []

        @step
        async def charge(n: int) -> str:
            executed.append(f"charge{n}")
            return f"ch{n}"

        @step
        async def audit(n: int) -> str:
            executed.append("audit")
            return "ok"

        store = MemoryStore()

        @workflow(name="ten_charges")
        async def before(ctx: Context, _: object = None) -> str:
            return ",".join([await ctx.step(charge, i) for i in range(10)])

        first = await Runtime(store=store).run(before)
        assert len(executed) == 10

        @workflow(name="ten_charges")
        async def after(ctx: Context, _: object = None) -> str:
            out = []
            for i in range(10):
                if i == 2:
                    await ctx.step(audit, i)
                out.append(await ctx.step(charge, i))
            return ",".join(out)

        executed.clear()
        resumed = Runtime(
            store=store, compatibility=CompatibilityMode.RESUME_FROM_DIVERGENCE
        )
        resumed.register(after)
        await resumed.replay(first.run_id)

        # One step was added; nine ran. Eight of those had already completed
        # and were re-executed only because their position moved.
        assert executed[0] == "audit"
        assert len(executed) == 9
        assert [c for c in executed if c.startswith("charge")] == [
            f"charge{i}" for i in range(2, 10)
        ]


class TestReplayedValueContractDrift:
    """A replayed value that no longer fits its declared type is downgraded.

    ``serde.decode`` catches the validation failure and hands back the raw
    payload, so an in-flight run survives a refactor rather than dying at the
    boundary. That leniency is deliberate and worth keeping — what was missing
    is any record that it happened, which left the workflow failing later at an
    attribute access that reads like a bug in the workflow.
    """

    def test_a_mismatched_payload_is_reported(self) -> None:
        from pydantic import BaseModel

        from loom.core.serde import decode, drift_of

        class Widened(BaseModel):
            sku: str
            quantity: int

        stored = {"sku": "abc"}  # journaled before `quantity` existed
        value = decode(stored, Widened)

        assert value == stored  # lenient: the raw payload, not an exception
        assert drift_of(value, Widened) is not None
        assert "quantity" in str(drift_of(value, Widened))

    def test_a_matching_payload_reports_nothing(self) -> None:
        from pydantic import BaseModel

        from loom.core.serde import decode, drift_of

        class Order(BaseModel):
            sku: str

        value = decode({"sku": "abc"}, Order)
        assert isinstance(value, Order)
        assert drift_of(value, Order) is None


# ---------------------------------------------------------------------------
# Replay must not need the capability that produced the answer
# ---------------------------------------------------------------------------


class TestReplayDoesNotNeedTheCapability:
    """A recorded answer is an answer, whatever this process can reach.

    Both checks below used to run when the *call was constructed*, so they
    fired on every body re-entry — including the ones where the journal
    already held the result. That made replaying a finished run demand the
    ability to recompute it: a completed ``human.review_edit`` could not be
    replayed in a worker with no human channel, and a completed ``ctx.agent``
    could not be replayed in one with no model. Both are ordinary deployments —
    a CI replay, or a process that only re-drives parked runs.

    They now run inside the journaled call, which for a first execution is the
    same moment for every purpose that matters: still before the node body, and
    still before a suspending node parks.
    """

    @pytest.mark.asyncio
    async def test_a_completed_human_node_replays_with_no_channel(self) -> None:
        from loom.nodes.human import ApprovalIn
        from loom.nodes.human.channels import AutoRespondChannel

        @workflow(name="needs_a_person")
        async def needs_a_person(ctx: Context, _: None = None) -> bool:
            answer = await ctx.node(
                "human.approval", ApprovalIn(subject="refund", question="ok?")
            )
            return bool(answer.approved)

        store = MemoryStore()
        channel = AutoRespondChannel(approve=True)
        answering = Runtime(store=store, human=channel)
        channel.bind(answering)
        answering.register(needs_a_person)
        first = await answering.run(needs_a_person)
        assert first.status is ExecutionStatus.COMPLETED
        assert first.output is True

        # A different process, with no way to ask anybody anything.
        deaf = Runtime(store=store)
        deaf.register(needs_a_person)
        assert deaf.human is None
        replayed = await deaf.replay(first.run_id)

        assert replayed.status is ExecutionStatus.COMPLETED
        assert replayed.output is True

    @pytest.mark.asyncio
    async def test_a_fresh_run_still_refuses_before_it_parks(self) -> None:
        """The property the eager check existed for, and it is unchanged.

        A run parked with nobody listening is indistinguishable from patience,
        so it is found a day late. Nothing recorded means the check still runs.
        """
        from loom.nodes.errors import HumanChannelMissing
        from loom.nodes.human import ApprovalIn

        @workflow(name="nobody_listening")
        async def nobody_listening(ctx: Context, _: None = None) -> bool:
            answer = await ctx.node(
                "human.approval", ApprovalIn(subject="refund", question="ok?")
            )
            return bool(answer.approved)

        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(nobody_listening)

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert result.error.type == HumanChannelMissing.__name__
        record = await runtime.get(result.run_id)
        assert record is not None
        assert record.awaiting_event is None, "it must not have parked"

    @pytest.mark.asyncio
    async def test_a_completed_agent_call_replays_with_no_backend(self) -> None:
        from loom.agents.result import AgentResult
        from loom.testing import given, run_with

        @workflow(name="needs_a_model")
        async def needs_a_model(ctx: Context, _: None = None) -> str:
            answer = await ctx.agent("summarise this", name="think")
            return str(answer.output)

        store = MemoryStore()
        runtime = Runtime(store=store)
        assert runtime.agent_backend is None

        # Seeded rather than generated: the point is that a *recorded* agent
        # result is served without the backend that would have produced it.
        first = await run_with(
            needs_a_model,
            None,
            given("think", kind=EntryKind.AGENT, returns=AgentResult(output="done")),
            runtime=runtime,
        )
        assert first.status is ExecutionStatus.COMPLETED
        assert first.output == "done"

    @pytest.mark.asyncio
    async def test_a_fresh_agent_call_still_names_the_missing_backend(self) -> None:
        @workflow(name="no_backend_at_all")
        async def no_backend_at_all(ctx: Context, _: None = None) -> str:
            answer = await ctx.agent("summarise this")
            return str(answer.output)

        result = await Runtime(store=MemoryStore()).run(no_backend_at_all)

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "agent_backend" in result.error.message
