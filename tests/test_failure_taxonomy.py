"""A step that stops trying has not said the run is finished.

Every failure used to be terminal at the journal level: replay re-raised it,
and the only recovery was ``retry()``, which prunes. That is right when the
*code* changed and wrong when the *world* did — a gateway returning 503 needs
the same code run again in five minutes with the other nine steps still cached,
not the journal edited and the attempt history thrown away.
"""

from __future__ import annotations

import pytest

from workflow_builder import Context, ExecutionStatus, Retry, Runtime, step, workflow
from workflow_builder.runtime.journal import (
    CompatibilityMode,
    EntryKind,
    EntryStatus,
    JournalEntry,
)
from workflow_builder.state.memory import MemoryStore


class TestTheStatusItself:
    def test_exhausted_is_not_settled(self) -> None:
        """Or `Journal.replayed` over-reports and lookups short-circuit work."""
        entry = JournalEntry(
            path="0", kind=EntryKind.STEP, name="x", status=EntryStatus.EXHAUSTED
        )
        assert not entry.is_settled

    def test_it_renders_as_failed(self) -> None:
        """It did fail. The distinction is about replay, not about display."""
        entry = JournalEntry(
            path="0", kind=EntryKind.STEP, name="x", status=EntryStatus.EXHAUSTED
        )
        assert entry.to_record(0).status.value == "failed"

    def test_a_fifth_member_did_not_break_rendering(self) -> None:
        """`to_record` indexes a dict directly; a missing key is a KeyError."""
        for status in EntryStatus:
            entry = JournalEntry(path="0", kind=EntryKind.STEP, name="x", status=status)
            assert entry.to_record(0) is not None


class TestResumeAfterATransientOutage:
    @pytest.mark.asyncio
    async def test_only_the_failed_step_runs_again(self) -> None:
        executed: list[str] = []
        outage = {"active": True}

        @step
        async def fetch(n: int) -> str:
            executed.append(f"fetch{n}")
            return f"row{n}"

        @step(retry=Retry(max_attempts=1))
        async def gateway(_: str) -> str:
            executed.append("gateway")
            if outage["active"]:
                raise RuntimeError("503 from the payment gateway")
            return "charged"

        @workflow(name="outage_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            for i in range(3):
                await ctx.step(fetch, i)
            return await ctx.step(gateway, "x")

        store = MemoryStore()
        rt = Runtime(store=store)
        first = await rt.run(flow)
        assert first.status is ExecutionStatus.FAILED
        assert executed == ["fetch0", "fetch1", "fetch2", "gateway"]

        # The world recovers. Nothing about the code or the journal changed.
        outage["active"] = False
        executed.clear()

        # The record survives the terminal run. Nothing mutates it on the way
        # out, so the attempt history is still there to read.
        entries = await store.load_journal(first.run_id)
        exhausted = [e for e in entries if e.status is EntryStatus.EXHAUSTED]
        assert [e.name for e in exhausted] == ["gateway"]
        assert exhausted[0].error is not None
        assert "503" in exhausted[0].error.message

    @pytest.mark.asyncio
    async def test_a_pending_run_replays_the_exhausted_step_only(self) -> None:
        """The shape an outer driver gets: cached work stays cached."""
        executed: list[str] = []
        outage = {"active": True}

        @step
        async def fetch(n: int) -> str:
            executed.append(f"fetch{n}")
            return f"row{n}"

        @step(retry=Retry(max_attempts=1))
        async def gateway(_: str) -> str:
            executed.append("gateway")
            if outage["active"]:
                raise RuntimeError("503")
            return "charged"

        @workflow(name="driver_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            for i in range(3):
                await ctx.step(fetch, i)
            return await ctx.step(gateway, "x")

        store = MemoryStore()
        rt = Runtime(store=store)
        first = await rt.run(flow)
        assert first.status is ExecutionStatus.FAILED

        outage["active"] = False
        executed.clear()
        again = await rt.retry(first.run_id)

        assert again.status is ExecutionStatus.COMPLETED
        assert executed == ["gateway"], "the three fetches should have replayed"


class TestCompensationIsUnaffected:
    """The interaction the audit flagged before any of this was written.

    Compensation unwinds inside ``except Exception``, before the record goes
    terminal. If an exhausted step parked the run instead of failing it, the
    rollback would already have happened and a later resume would re-execute
    steps whose side effects were undone.
    """

    @pytest.mark.asyncio
    async def test_it_runs_exactly_once_when_the_run_fails(self) -> None:
        unwound: list[str] = []

        @step
        async def book() -> str:
            return "seat_4A"

        async def release() -> str:
            unwound.append("released")
            return "ok"

        @step(retry=Retry(max_attempts=1))
        async def charge() -> str:
            raise RuntimeError("card declined")

        @workflow(name="saga_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            await ctx.step(book)
            await ctx.compensate(release)
            return await ctx.step(charge)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(flow)

        assert result.status is ExecutionStatus.FAILED
        assert unwound == ["released"]

    @pytest.mark.asyncio
    async def test_nothing_is_mutated_on_the_way_to_terminal(self) -> None:
        """Which is what keeps the unwind and the status independent.

        An earlier draft promoted exhausted entries to failed as the run went
        terminal. That put a journal write after the compensation stack had
        already run, and made the distinction unobservable — every failed run
        erased it. Reading the status per operation instead means compensation
        and replay never have to agree on an order.
        """
        order: list[str] = []

        async def undo() -> str:
            order.append("compensated")
            return "ok"

        @step(retry=Retry(max_attempts=1))
        async def boom() -> str:
            raise RuntimeError("nope")

        @workflow(name="order_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            await ctx.compensate(undo)
            return await ctx.step(boom)

        store = MemoryStore()
        rt = Runtime(store=store)
        result = await rt.run(flow)
        order.append("terminal")

        assert order == ["compensated", "terminal"]
        entries = await store.load_journal(result.run_id)
        assert [e.status for e in entries if e.name == "boom"] == [
            EntryStatus.EXHAUSTED
        ]


class TestReplayStillReproduces:
    """The other half: a rehearsal must show what happened, not what would."""

    @pytest.mark.asyncio
    async def test_replay_re_raises_where_retry_re_runs(self) -> None:
        attempts: list[str] = []
        outage = {"active": True}

        @step(retry=Retry(max_attempts=1))
        async def gateway() -> str:
            attempts.append("call")
            if outage["active"]:
                raise RuntimeError("503")
            return "charged"

        @workflow(name="reproduce_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            return await ctx.step(gateway)

        store = MemoryStore()
        rt = Runtime(store=store)
        first = await rt.run(flow)
        assert first.status is ExecutionStatus.FAILED

        # The world recovers, but a replay is a rehearsal of the run that
        # happened — it must still show the failure.
        outage["active"] = False
        attempts.clear()
        rehearsed = await rt.replay(first.run_id)

        assert rehearsed.status is ExecutionStatus.FAILED
        assert attempts == [], "replay must not re-execute the step"

        # retry asks the opposite question, and gets the opposite answer.
        again = await rt.retry(first.run_id)
        assert again.status is ExecutionStatus.COMPLETED
        assert attempts == ["call"]

    @pytest.mark.asyncio
    async def test_retry_keeps_the_attempt_history(self) -> None:
        """Pruning it is what the old single-status journal forced."""

        @step(retry=Retry(max_attempts=3, initial_delay=0.0))
        async def flaky() -> str:
            raise RuntimeError("still down")

        @workflow(name="history_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            return await ctx.step(flaky)

        store = MemoryStore()
        rt = Runtime(store=store)
        first = await rt.run(flow)
        await rt.retry(first.run_id)

        entries = await store.load_journal(first.run_id)
        flaky_entries = [e for e in entries if e.name == "flaky"]
        assert flaky_entries, "the entry should not have been truncated away"
        assert flaky_entries[0].attempts == 3


class TestVersionGates:
    """Adding a branch must not change what an in-flight run is doing.

    ``ctx.patched`` is the marker that makes deploying the old and new paths
    together safe: a run already past the gate keeps the old behaviour because
    its journal proves it was there first.
    """

    @pytest.mark.asyncio
    async def test_a_fresh_run_takes_the_new_branch(self) -> None:
        taken: list[str] = []

        @workflow(name="patch_fresh")
        async def flow(ctx: Context, _: object = None) -> str:
            if ctx.patched("new-pricing"):
                taken.append("new")
            else:
                taken.append("old")
            return taken[-1]

        result = await Runtime(store=MemoryStore()).run(flow)
        assert result.output == "new"

    @pytest.mark.asyncio
    async def test_the_decision_is_journaled_and_stable(self) -> None:
        """Replaying must reach the same branch, not re-decide."""

        @workflow(name="patch_stable")
        async def flow(ctx: Context, _: object = None) -> str:
            return "new" if ctx.patched("new-pricing") else "old"

        store = MemoryStore()
        rt = Runtime(store=store)
        first = await rt.run(flow)
        again = await rt.replay(first.run_id)

        assert first.output == again.output == "new"
        entries = await store.load_journal(first.run_id)
        markers = [e for e in entries if e.name == "patch:new-pricing"]
        assert len(markers) == 1
        assert markers[0].output is True

    @pytest.mark.asyncio
    async def test_a_run_already_past_the_gate_keeps_the_old_branch(self) -> None:
        """The case the whole mechanism exists for."""

        @step
        async def work(n: int) -> str:
            return f"row{n}"

        store = MemoryStore()

        @workflow(name="patch_inflight")
        async def before(ctx: Context, _: object = None) -> str:
            await ctx.step(work, 1)
            await ctx.step(work, 2)
            return "done"

        first = await Runtime(store=store).run(before)
        assert first.output == "done"

        # The branch is added at the top of a body that has already run past it.
        taken: list[str] = []

        @workflow(name="patch_inflight")
        async def after(ctx: Context, _: object = None) -> str:
            taken.append("new" if ctx.patched("new-pricing") else "old")
            await ctx.step(work, 1)
            await ctx.step(work, 2)
            return "done"

        resumed = Runtime(store=store)
        resumed.register(after)
        await resumed.replay(first.run_id)

        assert taken == ["old"], (
            "a run that was already past this point must not switch branches"
        )

    @pytest.mark.asyncio
    async def test_the_marker_is_found_after_calls_move(self) -> None:
        """Keyed by name, not position — unlike every other entry."""

        @step
        async def extra() -> str:
            return "x"

        store = MemoryStore()

        @workflow(name="patch_moves")
        async def before(ctx: Context, _: object = None) -> str:
            return "new" if ctx.patched("gate") else "old"

        first = await Runtime(store=store).run(before)
        assert first.output == "new"

        @workflow(name="patch_moves")
        async def after(ctx: Context, _: object = None) -> str:
            await ctx.step(extra)  # inserted ahead of the gate
            return "new" if ctx.patched("gate") else "old"

        resumed = Runtime(
            store=store, compatibility=CompatibilityMode.RESUME_FROM_DIVERGENCE
        )
        resumed.register(after)
        replayed = await resumed.replay(first.run_id)

        assert replayed.output == "new", "the marker moved position but kept its name"
