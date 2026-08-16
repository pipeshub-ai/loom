"""Turning a signal into an ordinary exception, so cleanup runs.

A Ctrl+C or a ``docker stop`` is not an error, but the default handling of both
makes it look like one: SIGINT unwinds the stack as a ``KeyboardInterrupt`` and
prints every frame it passes, and SIGTERM does not unwind at all — the process
simply stops, mid-step, with no ``finally`` block getting a turn.

That second one is the load-bearing case here. LOOM's recovery story depends on
cleanup happening: :meth:`Runtime.shutdown` stops the schedulers, and
``_drive``'s ``finally`` settles the lease that ``reclaim_orphans`` later matches
on. None of it runs if the process is killed where it stands.

So both signals are routed to the same place — cancel the work, let it unwind,
report it as what it is. Two properties are worth stating because they are the
ones people ask about:

**A second signal forces.** The first cancels and waits; the second restores the
default disposition and re-raises, so the process dies exactly as it would have
with none of this installed. A cleanup path that cannot itself be interrupted is
a hang with extra steps.

**Nothing here is installed by importing it.** A library that grabs the
process's signal handlers on import is a library you cannot embed. The CLI
composes these; a host that wants them asks.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable, Generator
from typing import Any, TypeVar

__all__ = [
    "DEFAULT_SIGNALS",
    "Interrupted",
    "guarded",
    "report",
    "run_main",
    "terminate_on",
]

T = TypeVar("T")

#: What a program is normally asked to stop by: a keyboard, or an orchestrator.
DEFAULT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)


class Interrupted(Exception):  # noqa: N818 - deliberate: a signal, not a failure
    """A signal cut the work short. Not a failure — nothing went wrong.

    Carries the signal so a caller can exit with the shell's convention for it
    (``128 + signum``: 130 for SIGINT, 143 for SIGTERM), which is how a calling
    script tells "the user pressed Ctrl+C" from "the workflow failed".
    """

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        super().__init__(f"interrupted by {self.name}")

    @property
    def name(self) -> str:
        try:
            return signal.Signals(self.signum).name
        except ValueError:  # pragma: no cover - not a real signal number
            return f"signal {self.signum}"

    @property
    def exit_code(self) -> int:
        """The conventional shell exit status for dying to this signal."""
        return 128 + self.signum


def _describe(signum: int) -> str:
    return Interrupted(signum).name


def report(signum: int, forced: bool, *, stream: Any = None) -> None:
    """A default *notify* for programs that want to say something.

    Writes to stderr, never stdout — ``loom mcp`` serves its protocol on stdout
    under stdio transport, and a friendly word there corrupts the session.
    """
    out = stream if stream is not None else sys.stderr
    name = _describe(signum)
    if forced:
        print(f"\n{name} again — stopping now, cleanup incomplete.", file=out)
    else:
        print(f"\n{name} — finishing up. Press again to stop now.", file=out)
    with contextlib.suppress(Exception):
        out.flush()


async def guarded(
    work: Awaitable[T],
    *,
    signals: tuple[signal.Signals, ...] = DEFAULT_SIGNALS,
    notify: Callable[[int, bool], None] | None = None,
) -> T:
    """Await *work*, cancelling it on a signal rather than dying on one.

    Raises :class:`Interrupted` when a signal arrived, having first let *work*
    unwind — so every ``finally`` on the stack has run by the time this returns.
    Anything else *work* raises passes through untouched.

    *notify* is called as ``(signum, forced)`` when a signal lands, before
    anything is cancelled. The default is silence, because this module has no
    business deciding what a program prints.

    Handlers are installed through the event loop rather than
    :func:`signal.signal`, because a handler that runs between bytecodes can
    land anywhere — including inside the cleanup it is trying to trigger. The
    loop's version is delivered as a callback between iterations, where
    cancelling a task is a well-defined thing to do. Platforms without it
    (Windows) fall back to Python's own SIGINT behaviour, which is caught here
    all the same.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    caught: list[int] = []
    # Bound before anything is installed, because `handle` closes over it and a
    # signal can land during installation.
    previous: dict[signal.Signals, Any] = {}

    def handle(signum: int) -> None:
        forced = bool(caught)
        caught.append(signum)
        if notify is not None:
            with contextlib.suppress(Exception):
                notify(signum, forced)
        if not forced:
            task.cancel()
            return
        # Forced. Put the signal back the way we found it and re-send it, so the
        # process dies now and with the exit status it would have had anyway.
        _restore(loop, previous)
        signal.raise_signal(signum)

    _install(loop, signals, handle, previous)
    try:
        return await task
    except asyncio.CancelledError:
        if caught:
            # Deliberately `from None`: the CancelledError is the mechanism, not
            # the cause, and chaining it prints the stack this exists to avoid.
            raise Interrupted(caught[0]) from None
        raise
    finally:
        _restore(loop, previous)


def run_main(
    work: Awaitable[Any],
    *,
    signals: tuple[signal.Signals, ...] = DEFAULT_SIGNALS,
    notify: Callable[[int, bool], None] | None = report,
) -> int:
    """Run a program's entry coroutine. Returns a process exit code.

        if __name__ == "__main__":
            sys.exit(run_main(main()))

    :func:`asyncio.run` with the two things a *program* needs on top of it and a
    library call does not: signals routed through :func:`guarded` so cleanup
    runs, and an interrupt reported as an exit status rather than a traceback.
    A bare ``asyncio.run(main())`` prints twenty frames of asyncio internals for
    a keypress, which reads as a crash.

    An ``int`` returned by *work* is passed through as the exit code, so a
    program that already computes one keeps it.
    """
    try:
        result = asyncio.run(guarded(work, signals=signals, notify=notify))
    except Interrupted as stop:
        return stop.exit_code
    except KeyboardInterrupt:
        return Interrupted(signal.SIGINT).exit_code
    return result if isinstance(result, int) else 0


@contextlib.contextmanager
def terminate_on(*signals: signal.Signals) -> Generator[None]:
    """Make these signals raise :class:`Interrupted` in the main thread.

    The synchronous counterpart to :func:`guarded`, for the parts of a program
    that are not inside an event loop: startup imports, argument parsing, and
    any command that never opens one. Without it those windows are uncovered —
    SIGINT prints a traceback through whatever import it landed in, and SIGTERM
    kills the process outright.

    Restores the previous handlers on exit, so wrapping a whole program in this
    does not stop a server inside it from installing its own.
    """

    def handle(signum: int, _frame: Any) -> None:
        raise Interrupted(signum)

    previous: dict[signal.Signals, Any] = {}
    for sig in signals:
        # ValueError: not the main thread. OSError/AttributeError: not this
        # platform. All three mean the same thing — we do not get to do this
        # here — and none of them should stop the program from running.
        with contextlib.suppress(ValueError, OSError, AttributeError):
            previous[sig] = signal.signal(sig, handle)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def _install(
    loop: asyncio.AbstractEventLoop,
    signals: tuple[signal.Signals, ...],
    handle: Callable[[int], None],
    previous: dict[signal.Signals, Any],
) -> None:
    """Route *signals* to *handle*, recording what was there before.

    The previous disposition is captured through :func:`signal.getsignal` rather
    than left to ``remove_signal_handler``, which restores the *default* rather
    than the handler it displaced — so an outer :func:`terminate_on` would not
    survive an inner :func:`guarded` without this.
    """
    for sig in signals:
        try:
            current = signal.getsignal(sig)
            loop.add_signal_handler(sig, handle, sig)
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            continue
        previous[sig] = current


def _restore(
    loop: asyncio.AbstractEventLoop, previous: dict[signal.Signals, Any]
) -> None:
    for sig, handler in list(previous.items()):
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(sig)
        if handler is not None:
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)
        previous.pop(sig, None)

