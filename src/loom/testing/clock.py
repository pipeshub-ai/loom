"""Moving time forward, and letting what is due actually happen.

`ManualClock` moves the reading. That alone does not run anything: a parked run
resumes when the scheduler notices it is due, and in a test nothing is driving
the scheduler. So a test that only advances the clock sees a run still parked
and concludes the timer is broken.

:func:`advance` is the pair of those two steps — move the clock, then tick, then
wait for the resumed runs to settle — which is almost always what "let five
minutes pass" is meant to mean.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from loom.core.exceptions import ConfigurationError
from loom.core.types import Duration
from loom.runtime.clock import ManualClock

__all__ = ["advance", "advance_to"]


def _manual(runtime: Any) -> ManualClock:
    clock = getattr(runtime, "clock", None)
    if not isinstance(clock, ManualClock):
        raise ConfigurationError(
            "advancing time needs a ManualClock: "
            "Runtime(clock=ManualClock()). The Runtime is on "
            f"{clock!r}, which cannot be moved."
        )
    return clock


async def advance(
    runtime: Any,
    delta: Duration | timedelta = 0.0,
    *,
    dispatcher: Any = None,
    settle: bool = True,
    **units: float,
) -> list[str]:
    """Move *runtime*'s clock forward and run everything that comes due.

    Returns the ids of the runs that resumed, so a test can assert on *what*
    fired rather than only on what state things ended in::

        clock = ManualClock(datetime(2026, 1, 1, 8, 55, tzinfo=UTC))
        runtime = Runtime(store=MemoryStore(), clock=clock)
        ...
        resumed = await advance(runtime, minutes=10)

    Pass ``dispatcher`` to fire cron and interval triggers in the same move; its
    run ids come back in the same list. Without it only timers and sleeps wake,
    because a `TriggerDispatcher` is a separate thing a host may not be running.

    ``settle=False`` returns as soon as the runs are spawned, for the rare test
    that wants to observe a run mid-flight.
    """
    clock = _manual(runtime)
    clock.advance(delta, **units)
    return await _fire(runtime, dispatcher=dispatcher, settle=settle)


async def advance_to(
    runtime: Any,
    when: datetime,
    *,
    dispatcher: Any = None,
    settle: bool = True,
) -> list[str]:
    """Jump to an absolute moment and run everything that comes due.

    The one to reach for when the schedule is the subject: "9am on the first of
    the month" is a date, and computing the delta to it by hand is where the
    off-by-one lives.
    """
    clock = _manual(runtime)
    clock.set(when)
    return await _fire(runtime, dispatcher=dispatcher, settle=settle)


async def _fire(runtime: Any, *, dispatcher: Any, settle: bool) -> list[str]:
    """Tick both schedulers, then let the work finish."""
    fired: list[str] = list(await runtime.tick())
    if dispatcher is not None:
        fired.extend(await dispatcher.tick())
    if settle:
        await settled(runtime)
    return fired


async def settled(runtime: Any, *, timeout: float = 5.0) -> None:
    """Wait until the runtime has no work in flight.

    ``tick()`` spawns each resumed run as a background task and returns, so a
    test that asserts immediately after it is racing the thing it is testing.
    This waits for those tasks — and for any they spawned in turn, which is how
    a child workflow finishes.

    The timeout is a test-hang guard, not a schedule: it raises rather than
    passing quietly, because a silent timeout here would look exactly like a
    workflow that legitimately did nothing.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [task for task in runtime._background if not task.done()]
        if not pending:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"{len(pending)} run(s) still in flight after {timeout:.0f}s"
            )
        await asyncio.wait(pending, timeout=deadline - asyncio.get_running_loop().time())
