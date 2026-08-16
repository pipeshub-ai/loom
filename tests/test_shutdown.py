"""Interruption: what happens to a run when the process goes away.

A workflow body is re-entrant by construction, so an interrupted run is never
*lost* — it is resumed from the journal and finished. That guarantee has one
prerequisite, and it is the whole subject of this file: something has to be able
to *find* the run afterwards.

`reclaim_orphans` finds runs by expired lease. So a drive that stops working on
a run without settling its lease correctly takes that run out of circulation
permanently: it stays RUNNING, no timer covers it, and no scan matches it. The
failure is silent and looks exactly like a slow step.

The asymmetry these tests pin down is that a Ctrl+C must be no worse than a
`kill -9`. A killed process leaves the lease untouched and recovers on the next
scan; an interrupted one used to *clear* the lease on its way out and never
recover at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from unittest import mock

import pytest

import loom.cli as cli
import loom.runtime.shutdown as shutdown
from loom import Context, Runtime, step, workflow
from loom.cli import commands
from loom.cli.output import Exit
from loom.core.exceptions import ConfigurationError
from loom.core.models import ExecutionRecord, ExecutionStatus
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# A workflow that can be caught in the middle
# ---------------------------------------------------------------------------


class Gate:
    """Lets a test hold a step open until it has interrupted the run."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.compensated = False
        self.finished = False


#: Set per-test rather than passed through, because a @step's arguments are
#: journaled and a Gate is not serializable.
GATE: Gate


@step
async def hold(n: int) -> int:
    """Block until the test lets go, so the interrupt lands mid-step."""
    GATE.entered.set()
    await GATE.release.wait()
    GATE.finished = True
    return n * 2


@step
async def undo() -> None:
    GATE.compensated = True


@workflow(name="held")
async def held(ctx: Context, n: int) -> int:
    await ctx.compensate(undo)
    return await ctx.step(hold, n)


@pytest.fixture(autouse=True)
def gate():
    global GATE
    GATE = Gate()
    return GATE


async def record_of(rt: Runtime, run_id: str) -> ExecutionRecord:
    """The run's record, asserted present so the reads below stay readable."""
    record = await rt.get(run_id)
    assert record is not None
    return record


async def interrupt(rt: Runtime, n: int = 21) -> str:
    """Start a run, wait until it is mid-step, then cancel it as Ctrl+C would.

    Cancelling the task driving the run is precisely what a Ctrl+C does:
    `asyncio.run` cancels the main task and re-raises KeyboardInterrupt once the
    cancellation has been delivered. Doing it directly keeps the test at
    millisecond speed and off the process's signal disposition.
    """
    task = asyncio.create_task(rt.run(held, n))
    await asyncio.wait_for(GATE.entered.wait(), timeout=5)
    run_id = next(iter(rt._driving))
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return run_id


class PausedRead(MemoryStore):
    """A store that holds the first record read open.

    That read is `_require`, the first thing `_drive_inner` does and the last
    moment at which a run is still PENDING with no lease — the window this
    fixture exists to sit inside.
    """

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def get_execution(self, run_id: str):
        if self.armed:
            self.armed = False
            self.reached.set()
            await self.release.wait()
        return await super().get_execution(run_id)


async def interrupt_before_start(rt: Runtime, store: PausedRead, n: int = 21) -> str:
    """Cancel a run in the gap between its record existing and its lease."""
    store.armed = True
    task = asyncio.create_task(rt.run(held, n))
    await asyncio.wait_for(store.reached.wait(), timeout=5)
    run_id = next(iter(rt._driving))
    task.cancel()
    store.release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return run_id



# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


class TestAnInterruptedRunStaysFindable:
    async def test_it_keeps_a_lease_that_reclaim_can_match(self) -> None:
        """The bug in one assertion.

        `reclaim_orphans` matches on `lease_expires_at`, so a None here is not a
        tidier record — it is a run that no scan will ever return.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(held)

        record = await record_of(rt, await interrupt(rt))

        assert record.status is ExecutionStatus.RUNNING
        assert record.lease_expires_at is not None

    async def test_the_lease_is_already_expired_so_recovery_is_immediate(self) -> None:
        """We know we are not coming back, so there is no TTL to wait out.

        This is the one place an interrupt does better than a crash: a killed
        process leaves the heartbeat's future timestamp behind and the run is
        unavailable until it lapses.
        """
        rt = Runtime(store=MemoryStore(), lease_ttl=3600)
        rt.register(held)

        record = await record_of(rt, await interrupt(rt))

        assert record.lease_expires_at is not None
        assert record.lease_expires_at <= rt.clock.now()

    async def test_the_owner_is_kept_as_a_breadcrumb(self) -> None:
        """So the reclaiming node can say whose run it is picking up."""
        rt = Runtime(store=MemoryStore(), node_id="node-a")
        rt.register(held)

        record = await record_of(rt, await interrupt(rt))

        assert record.lease_owner == "node-a"

    async def test_a_run_that_finished_normally_still_drops_its_lease(self) -> None:
        """The invariant that makes the tests above legible.

        Settling on a terminal run and abandoning a RUNNING one are told apart
        by the record's own status, so this case has to keep working for that
        reading to mean anything.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(held)
        GATE.release.set()

        result = await rt.run(held, 21)
        record = await record_of(rt, result.run_id)

        assert result.status is ExecutionStatus.COMPLETED
        assert record.lease_owner is None
        assert record.lease_expires_at is None


class TestAnInterruptIsNotAnOutcome:
    async def test_it_is_not_recorded_as_failed(self) -> None:
        """Nobody formed an opinion about this run, so nothing is written.

        CancelledError is a BaseException; falling into the engine's
        `except Exception` arm would mark a perfectly healthy run FAILED on a
        keystroke.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(held)

        record = await record_of(rt, await interrupt(rt))

        assert record.status is ExecutionStatus.RUNNING
        assert record.error is None

    async def test_compensations_do_not_unwind(self) -> None:
        """The saga must not roll back work that is about to be resumed."""
        rt = Runtime(store=MemoryStore())
        rt.register(held)

        await interrupt(rt)

        assert GATE.compensated is False


class TestARunCancelledBeforeItStartedIsAlsoFindable:
    """The narrow twin of the case above.

    Only a store round-trip sits between creating the record and taking the
    lease, so a drive cancelled in that gap leaves a run that is PENDING, not
    RUNNING, and that nobody has ever leased. It is the same defect — a record
    no scan can match — and it gets the same answer: claim the lease on the way
    out, and let `reclaim_orphans` look at PENDING too.
    """

    async def test_it_is_left_with_a_lease_it_never_had(self) -> None:
        store = PausedRead()
        rt = Runtime(store=store, node_id="node-a")
        rt.register(held)

        run_id = await interrupt_before_start(rt, store)
        record = await record_of(rt, run_id)

        assert record.status is ExecutionStatus.PENDING
        assert record.lease_owner == "node-a"
        assert record.lease_expires_at is not None

    async def test_reclaim_picks_it_up_and_runs_it(self) -> None:
        store = PausedRead()
        first = Runtime(store=store, node_id="node-a")
        first.register(held)
        run_id = await interrupt_before_start(first, store)

        GATE.release.set()
        second = Runtime(store=store, node_id="node-b")
        second.register(held)
        try:
            assert await second.reclaim_orphans() == [run_id]
            result = await second.wait(run_id, timeout=5)
        finally:
            await second.shutdown(drain=0)

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == 42

    async def test_a_queued_run_nobody_has_touched_is_not_reclaimed(self) -> None:
        """The property that lets PENDING be scanned at all.

        `submit()` creates the record and drives it on a separate task, so there
        is always a moment where a PENDING record is waiting its turn. The
        *expired lease* is the signal, never the status — so a run that is
        merely queued is never stolen out from under the task about to run it,
        however long it waits.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(held)
        await rt.store.create_execution(
            ExecutionRecord(run_id="queued", workflow="held", status=ExecutionStatus.PENDING)
        )

        assert await rt.reclaim_orphans() == []


class TestRecovery:
    async def test_another_node_reclaims_it_and_it_completes(self) -> None:
        """End to end: the interrupt, then the recovery, on one store."""
        store = MemoryStore()
        first = Runtime(store=store, node_id="node-a")
        first.register(held)
        run_id = await interrupt(first, n=21)

        GATE.release.set()
        second = Runtime(store=store, node_id="node-b")
        second.register(held)
        try:
            assert await second.reclaim_orphans() == [run_id]
            result = await second.wait(run_id, timeout=5)
        finally:
            await second.shutdown(drain=0)

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == 42

    async def test_the_step_that_was_in_flight_runs_again(self) -> None:
        """It never completed, so it was never journaled, so it is re-executed.

        Which is the point of leaving the record alone: correctness here comes
        from the journal, not from anything the interrupt managed to write.
        """
        store = MemoryStore()
        first = Runtime(store=store)
        first.register(held)
        run_id = await interrupt(first)

        # The step's entry exists but is still PENDING — it was written on the
        # way in and never answered. Replay serves completed entries and
        # re-executes everything else, so nothing has to be undone here.
        assert GATE.finished is False
        assert [(e.name, e.status.value) for e in await store.load_journal(run_id)] == [
            # Kept: it was answered before the interrupt, so replay serves it.
            ("compensate:undo", "completed"),
            # Written on the way in and never answered, so replay re-executes it.
            ("hold", "pending"),
        ]

        GATE.release.set()
        second = Runtime(store=store)
        second.register(held)
        try:
            await second.reclaim_orphans()
            await second.wait(run_id, timeout=5)
        finally:
            await second.shutdown(drain=0)

        assert GATE.finished is True


# ---------------------------------------------------------------------------
# Runtime.shutdown
# ---------------------------------------------------------------------------


class TestShutdownDrains:
    async def test_it_waits_for_a_drive_already_in_flight(self) -> None:
        rt = Runtime(store=MemoryStore())
        rt.register(held)
        task = asyncio.create_task(rt.submit(held, 21))
        run_id = await task
        await asyncio.wait_for(GATE.entered.wait(), timeout=5)

        GATE.release.set()
        await rt.shutdown(drain=5)

        assert (await record_of(rt, run_id)).status is ExecutionStatus.COMPLETED

    async def test_it_gives_up_at_the_deadline_and_leaves_the_run_recoverable(
        self,
    ) -> None:
        """A drain deadline cancels; it does not lose.

        Which is why the deadline can be short: whatever it cuts off is exactly
        the interrupted-run case above, and recovers the same way.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(held)
        run_id = await rt.submit(held, 21)
        await asyncio.wait_for(GATE.entered.wait(), timeout=5)

        await rt.shutdown(drain=0.05)

        record = await record_of(rt, run_id)
        assert record.status is ExecutionStatus.RUNNING
        assert record.lease_expires_at is not None

    async def test_drain_zero_cancels_immediately(self) -> None:
        """What it did before it took an argument, and what a test wants."""
        rt = Runtime(store=MemoryStore())
        rt.register(held)
        await rt.submit(held, 21)
        await asyncio.wait_for(GATE.entered.wait(), timeout=5)

        await rt.shutdown(drain=0)

        assert GATE.finished is False


class TestShutdownStopsTheSourcesOfNewRuns:
    async def test_it_stops_a_supervised_dispatcher(self) -> None:
        """A dispatcher left running submits work into a closing Runtime."""
        from loom.runtime.dispatcher import TriggerDispatcher

        rt = Runtime(store=MemoryStore())
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.start(interval=60.0)
        assert dispatcher._task is not None

        await rt.shutdown(drain=0)

        assert dispatcher._task is None

    async def test_it_stops_a_supervised_queue_consumer(self) -> None:
        from loom.triggers.queue import InMemoryQueue, QueueConsumer

        rt = Runtime(store=MemoryStore())
        rt.register(held)
        consumer = QueueConsumer(rt, InMemoryQueue(), held)
        await consumer.start(interval=60.0)
        assert consumer._task is not None

        await rt.shutdown(drain=0)

        assert consumer._task is None

    async def test_a_service_that_stops_itself_is_forgotten(self) -> None:
        """So shutdown does not stop it twice, or hold it alive to do so."""
        from loom.runtime.dispatcher import TriggerDispatcher

        rt = Runtime(store=MemoryStore())
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.start(interval=60.0)
        await dispatcher.stop()

        assert list(rt._services) == []


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
#
# Every test below signals with SIGUSR1/SIGUSR2 rather than the real pair. The
# handlers are the same code path — `guarded` does not know which signal it was
# given — and a test that installs a handler for SIGINT is one bug away from
# taking down the test runner with it.


async def settle() -> None:
    """Let the loop deliver a signal.

    `add_signal_handler` callbacks arrive through the wakeup fd on a later
    iteration, so a raise is not observable on the line after it.
    """
    for _ in range(10):
        await asyncio.sleep(0)


class TestInterrupted:
    def test_it_reports_the_shell_convention(self) -> None:
        assert shutdown.Interrupted(signal.SIGINT).exit_code == 130
        assert shutdown.Interrupted(signal.SIGTERM).exit_code == 143

    def test_it_names_the_signal(self) -> None:
        assert shutdown.Interrupted(signal.SIGTERM).name == "SIGTERM"
        assert "SIGTERM" in str(shutdown.Interrupted(signal.SIGTERM))


class TestGuarded:
    async def test_it_returns_the_result_when_nothing_happens(self) -> None:
        async def work() -> str:
            return "done"

        assert await shutdown.guarded(work(), signals=(signal.SIGUSR1,)) == "done"

    async def test_it_leaves_the_work_s_own_failure_alone(self) -> None:
        """A signal handler must not become a general exception handler."""

        async def work() -> None:
            raise ValueError("the workflow broke")

        with pytest.raises(ValueError, match="the workflow broke"):
            await shutdown.guarded(work(), signals=(signal.SIGUSR1,))

    async def test_a_signal_raises_interrupted_naming_it(self) -> None:
        async def work() -> None:
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(30)

        with pytest.raises(shutdown.Interrupted) as caught:
            await shutdown.guarded(work(), signals=(signal.SIGUSR1,))

        assert caught.value.signum == signal.SIGUSR1

    async def test_the_work_gets_to_unwind_first(self) -> None:
        """The whole point. Cleanup is what makes an interrupt recoverable."""
        cleaned = []

        async def work() -> None:
            try:
                signal.raise_signal(signal.SIGUSR1)
                await asyncio.sleep(30)
            finally:
                cleaned.append("ran")

        with contextlib.suppress(shutdown.Interrupted):
            await shutdown.guarded(work(), signals=(signal.SIGUSR1,))

        assert cleaned == ["ran"]

    async def test_a_cancellation_from_elsewhere_stays_a_cancellation(self) -> None:
        """No signal arrived, so this is not ours to relabel."""

        async def work() -> None:
            await asyncio.sleep(30)

        task = asyncio.ensure_future(
            shutdown.guarded(work(), signals=(signal.SIGUSR1,))
        )
        await settle()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_it_says_what_is_happening_once_per_signal(self) -> None:
        said: list[tuple[int, bool]] = []

        async def work() -> None:
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(30)

        with contextlib.suppress(shutdown.Interrupted):
            await shutdown.guarded(
                work(),
                signals=(signal.SIGUSR1,),
                notify=lambda num, forced: said.append((num, forced)),
            )

        assert said == [(signal.SIGUSR1, False)]


class TestASecondSignalForces:
    async def test_it_is_reported_as_forced(self) -> None:
        """A cleanup path that cannot be interrupted is a hang with extra steps.

        SIGUSR1 is set to SIG_IGN around this, so the re-raise `guarded` performs
        after restoring the previous disposition lands on an ignore instead of
        killing the test runner — which is exactly the "put it back the way we
        found it" behaviour being asserted.
        """
        said: list[tuple[int, bool]] = []

        async def work() -> None:
            try:
                signal.raise_signal(signal.SIGUSR1)
                await asyncio.sleep(30)
            finally:
                # The real second Ctrl+C: it arrives while the first one's
                # cleanup is still running, which is when someone presses it.
                signal.raise_signal(signal.SIGUSR1)
                await settle()

        previous = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        try:
            with contextlib.suppress(shutdown.Interrupted):
                await shutdown.guarded(
                    work(),
                    signals=(signal.SIGUSR1,),
                    notify=lambda num, forced: said.append((num, forced)),
                )
        finally:
            signal.signal(signal.SIGUSR1, previous)

        assert said == [(signal.SIGUSR1, False), (signal.SIGUSR1, True)]


class TestHandlersAreBorrowed:
    async def test_the_previous_handler_comes_back(self) -> None:
        """`guarded` is composable only if it puts things back.

        `remove_signal_handler` restores the *default*, not what it displaced,
        so without saving this explicitly an outer handler would silently
        disappear the first time a command opened an event loop.
        """

        def mine(_num: int, _frame: object) -> None: ...

        previous = signal.signal(signal.SIGUSR2, mine)
        try:

            async def work() -> None:
                assert signal.getsignal(signal.SIGUSR2) is not mine

            await shutdown.guarded(work(), signals=(signal.SIGUSR2,))
            assert signal.getsignal(signal.SIGUSR2) is mine
        finally:
            signal.signal(signal.SIGUSR2, previous)


class TestTerminateOn:
    def test_it_raises_interrupted_in_synchronous_code(self) -> None:
        """The window `guarded` cannot reach: no loop is running yet."""
        with (
            pytest.raises(shutdown.Interrupted) as caught,
            shutdown.terminate_on(signal.SIGUSR1),
        ):
            signal.raise_signal(signal.SIGUSR1)

        assert caught.value.signum == signal.SIGUSR1

    def test_it_restores_what_it_displaced(self) -> None:
        """So a server started inside it can install its own and get them back."""
        def mine(_num: int, _frame: object) -> None: ...

        previous = signal.signal(signal.SIGUSR1, mine)
        try:
            with shutdown.terminate_on(signal.SIGUSR1):
                assert signal.getsignal(signal.SIGUSR1) is not mine
            assert signal.getsignal(signal.SIGUSR1) is mine
        finally:
            signal.signal(signal.SIGUSR1, previous)

    async def test_an_outer_one_survives_an_inner_guarded(self) -> None:
        """How the CLI is actually arranged: `main` wraps, `run_async` nests."""
        with shutdown.terminate_on(signal.SIGUSR1):
            outer = signal.getsignal(signal.SIGUSR1)

            async def work() -> None: ...

            await shutdown.guarded(work(), signals=(signal.SIGUSR1,))
            assert signal.getsignal(signal.SIGUSR1) is outer


# ---------------------------------------------------------------------------
# What the CLI does with it
# ---------------------------------------------------------------------------


class TestTheCliReportsAnInterrupt:
    def test_it_exits_with_the_signal_s_conventional_code(self) -> None:
        """Not USAGE, which is what it used to be.

        A script branching on the exit code was told to go fix its arguments
        when what actually happened was the user pressing Ctrl+C.
        """

        async def body() -> int:
            raise shutdown.Interrupted(signal.SIGTERM)

        assert commands.run_async(body()) == 143

    def test_a_keyboard_interrupt_lands_in_the_same_place(self) -> None:
        """Reachable where add_signal_handler is not, and before it installs."""

        async def body() -> int:
            raise KeyboardInterrupt

        assert commands.run_async(body()) == Exit.INTERRUPTED

    def test_it_points_at_the_runs_that_survived(self, capsys) -> None:
        """An interrupted run is recoverable, which is worth nothing unsaid."""

        async def body() -> int:
            raise shutdown.Interrupted(signal.SIGINT)

        commands.run_async(body())

        assert "--status running" in capsys.readouterr().err

    def test_a_real_error_still_reports_as_usage(self) -> None:
        """The signal handling did not swallow the cases around it."""

        async def body() -> int:
            raise ConfigurationError("no such workflow")

        assert commands.run_async(body()) == Exit.USAGE


class TestTheCliCoversTheWindowsOutsideALoop:
    def test_an_interrupt_while_dispatching_is_not_a_traceback(self) -> None:
        """Startup imports and the commands that never open a loop."""
        with mock.patch.object(cli, "_dispatch", side_effect=KeyboardInterrupt):
            assert cli.main([]) == Exit.INTERRUPTED

    def test_a_sigterm_there_reports_its_own_code(self) -> None:
        stop = shutdown.Interrupted(signal.SIGTERM)
        with mock.patch.object(cli, "_dispatch", side_effect=stop):
            assert cli.main([]) == 143


# ---------------------------------------------------------------------------
# The whole thing, as a process
# ---------------------------------------------------------------------------
#
# Everything above cancels a task, which is what a signal *does* but not what a
# signal *is*. None of it would have caught the two defects that started this:
# that SIGTERM ran no cleanup at all, and that a Ctrl+C during startup printed
# forty frames of `dataclasses.py`. Both live outside any event loop, so only a
# real process taking a real signal shows them.
#
# The cost is process startup — `import loom` is ~250ms — so this stays a small
# number of cases covering the surfaces, not a matrix.

WORKFLOW_MODULE = '''
import asyncio
from pathlib import Path

from loom import Context, step, workflow

# __file__, not sys.argv[0]: the CLI imports this module, so argv[0] is
# loom's own __main__ and the marker would land in site-packages.
MARKER = Path(__file__).parent / "started"


@step
async def hold(n: int) -> int:
    """Announce that the run is in flight, then block until signalled."""
    MARKER.write_text("go")
    await asyncio.sleep(120)
    return n


@workflow(name="slowflow")
async def slowflow(ctx: Context, n: int) -> int:
    return await ctx.step(hold, n)
'''

SCRIPT = '''
import asyncio
import sys
from pathlib import Path

from loom import Context, Runtime, step, workflow
from loom.runtime.shutdown import run_main
from loom.stores.memory import MemoryStore

MARKER = Path(__file__).parent / "started"


@step
async def hold(n: int) -> int:
    MARKER.write_text("go")
    await asyncio.sleep(120)
    return n


@workflow(name="slowflow")
async def slowflow(ctx: Context, n: int) -> int:
    return await ctx.step(hold, n)


async def main() -> None:
    async with Runtime(store=MemoryStore()) as rt:
        await rt.run(slowflow, 1)


if __name__ == "__main__":
    raise SystemExit(run_main(main()))
'''


@pytest.fixture
def project(tmp_path):
    """A workflow module and a sqlite store, in a directory of their own."""
    (tmp_path / "flows.py").write_text(WORKFLOW_MODULE)
    (tmp_path / "script.py").write_text(SCRIPT)
    return tmp_path


def signal_once(argv: list[str], cwd, *, sig: int, env: dict | None = None):
    """Run *argv*, wait until the run is really in flight, then signal it once.

    Polling for the marker the step writes rather than sleeping a fixed time:
    a fixed wait is either slower than it needs to be or, on a loaded CI box,
    lands before the process has started and tests nothing.
    """
    import os
    import subprocess
    import time

    marker = cwd / "started"
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)

    process.send_signal(sig)
    try:
        output = process.communicate(timeout=60)[0].decode(errors="replace")
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()[0].decode(errors="replace")
        pytest.fail(f"did not exit within 60s of {signal.Signals(sig).name}:\n{output}")
    return process.returncode, output


def store_url(path) -> str:
    return f"sqlite:///{path / 'runs.db'}"


@pytest.mark.slow
class TestASignalledProcessExitsCleanly:
    def test_ctrl_c_during_a_run(self, project) -> None:
        code, output = signal_once(
            [sys.executable, "-m", "loom.cli", "run", "flows.py::slowflow", "-i", "1"],
            project,
            sig=signal.SIGINT,
            env={"LOOM_STORE": store_url(project)},
        )

        assert "Traceback" not in output, output
        assert code == 130, output

    def test_sigterm_during_a_run(self, project) -> None:
        """The case that used to run no cleanup whatsoever."""
        code, output = signal_once(
            [sys.executable, "-m", "loom.cli", "run", "flows.py::slowflow", "-i", "1"],
            project,
            sig=signal.SIGTERM,
            env={"LOOM_STORE": store_url(project)},
        )

        assert "Traceback" not in output, output
        assert code == 143, output

    def test_a_script_using_run_main(self, project) -> None:
        """What every cookbook example now looks like."""
        code, output = signal_once(
            [sys.executable, "script.py"], project, sig=signal.SIGINT
        )

        assert "Traceback" not in output, output
        assert code == 130, output


@pytest.mark.slow
class TestASignalledRunSurvivesInTheStore:
    def test_it_is_left_recoverable_and_says_so(self, project) -> None:
        """The end-to-end version of the lease tests above.

        Worth paying for a subprocess: this is the assertion that a real
        SIGTERM — no Python-level cancellation involved — still reaches the
        `finally` that settles the lease.
        """
        import asyncio as aio

        from loom.stores import from_url

        code, output = signal_once(
            [sys.executable, "-m", "loom.cli", "run", "flows.py::slowflow", "-i", "1"],
            project,
            sig=signal.SIGTERM,
            env={"LOOM_STORE": store_url(project)},
        )
        assert code == 143, output
        assert "--status running" in output, output

        async def inspect():
            store = from_url(store_url(project))
            records = await store.list_executions(status=ExecutionStatus.RUNNING)
            close = getattr(store, "close", None)
            if close is not None:
                await close()
            return records

        records = aio.run(inspect())

        assert len(records) == 1
        # Not None, which is what made the run unfindable and this whole file
        # necessary.
        assert records[0].lease_expires_at is not None
