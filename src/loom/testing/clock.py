"""Moving time forward, and letting what is due actually happen.

`ManualClock` moves the reading. That alone does not run anything: a parked run
resumes when the scheduler notices it is due, and in a test nothing is driving
the scheduler. So a test that only advances the clock sees a run still parked
and concludes the timer is broken.

:func:`advance` is the pair of those two steps — move the clock, then tick, then
wait for the resumed runs to settle — which is almost always what "let five
minutes pass" is meant to mean.

The same is true of every other background component, and each one names its
pass differently: a ``TriggerDispatcher`` ticks, an ``EventDispatcher`` polls,
a ``WatchRenewer`` and a ``CredentialRefreshService`` sweep. ``dispatcher=``
takes any of them, so "let five minutes pass" covers cron, the event backbone,
and the things that keep provider subscriptions and OAuth tokens alive —
rather than cron alone, which is what it used to mean and did not say.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from loom.core.exceptions import ConfigurationError
from loom.core.types import Duration
from loom.runtime.clock import ManualClock

__all__ = ["advance", "advance_to"]


#: What a "driver" has to expose for :func:`advance` to pump it, in the order
#: they are tried.
#:
#: Four names rather than one because the components that answer "run whatever
#: is due now" grew separately and did not converge: ``TriggerDispatcher`` and
#: ``Runtime`` have ``tick``, ``EventDispatcher`` had only ``poll_once``,
#: ``WatchRenewer`` and ``CredentialRefreshService`` have ``sweep``. Requiring
#: one name would mean either renaming three public methods or leaving three
#: subsystems unreachable from the helper a test reaches for to move time —
#: and the second is what happened: ``advance(rt, dispatcher=…)`` raised
#: ``AttributeError`` on anything but a ``TriggerDispatcher``.
_DRIVER_METHODS = ("tick", "poll_once", "sweep", "drain")


def _manual(runtime: Any) -> ManualClock:
    if isinstance(runtime, ManualClock):
        # A bare clock, for a test whose subject is a background loop rather
        # than a run — a renewer or a credential sweep has no Runtime to tick.
        return runtime
    clock = getattr(runtime, "clock", None)
    if not isinstance(clock, ManualClock):
        raise ConfigurationError(
            "advancing time needs a ManualClock: "
            "Runtime(clock=ManualClock()). The Runtime is on "
            f"{clock!r}, which cannot be moved."
        )
    return clock


async def _drive(driver: Any) -> list[str]:
    """Run one pass of *driver*, whatever it calls that. Returns its run ids.

    ``tick`` is the only name that *promises* run ids, so it is the only one
    whose bare strings are taken as such. A ``sweep`` also returns a list of
    strings — the resources it renewed — and folding those into the list a test
    asserts on would report a mailbox address as a run that started. Everything
    else is read structurally, from the report each component already returns.
    """
    for name in _DRIVER_METHODS:
        method = getattr(driver, name, None)
        if method is None:
            continue
        produced = await method()
        if name == "tick":
            return [item for item in (produced or ()) if isinstance(item, str)]
        return _reported_runs(produced)
    raise ConfigurationError(
        f"{driver!r} cannot be driven by advance(): it exposes none of "
        f"{', '.join(_DRIVER_METHODS)}. Pass the component that runs what is "
        "due — a TriggerDispatcher, an EventDispatcher, a WatchRenewer — or "
        "call it yourself after advancing."
    )


def _reported_runs(produced: Any) -> list[str]:
    """Run ids out of whatever report a pass returned.

    ``DispatchReport.started`` and ``ConsumeReport.submitted`` are the two
    shapes in the tree. Anything else contributes nothing rather than being
    guessed at — a pass that started no runs and a pass whose report this does
    not understand are both honestly empty, where a guess would be a run id
    that never existed.
    """
    submitted = getattr(produced, "submitted", None)
    if submitted is not None:
        return [str(item) for item in submitted]
    rows = produced if isinstance(produced, list | tuple) else (produced,)
    found: list[str] = []
    for row in rows:
        started = getattr(row, "started", None)
        if started is not None:
            found.extend(str(item) for item in started)
    return found


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

    Pass ``dispatcher`` to drive a background component in the same move; its
    run ids come back in the same list. Anything exposing ``tick``,
    ``poll_once``, ``sweep`` or ``drain`` qualifies — a ``TriggerDispatcher``
    for cron and intervals, an ``EventDispatcher`` for the event backbone, a
    ``WatchRenewer`` or a ``CredentialRefreshService`` for what they keep
    alive. Without one, only timers and sleeps wake: each of those is a
    separate thing a host may or may not be running.

    *runtime* may also be a bare :class:`~loom.runtime.clock.ManualClock`, for
    a test whose subject is a loop rather than a run.

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

    Takes the same ``dispatcher`` as :func:`advance`, on the same terms.
    """
    clock = _manual(runtime)
    clock.set(when)
    return await _fire(runtime, dispatcher=dispatcher, settle=settle)


async def _fire(runtime: Any, *, dispatcher: Any, settle: bool) -> list[str]:
    """Tick both schedulers, then let the work finish."""
    # A parked sleeper is released synchronously by `advance()` but does not
    # *run* until the loop next gets a turn, so a background loop released here
    # would otherwise still be one iteration behind everything asserted below.
    await asyncio.sleep(0)

    fired: list[str] = []
    if hasattr(runtime, "tick"):
        fired.extend(await runtime.tick())
    if dispatcher is not None:
        fired.extend(await _drive(dispatcher))
    if settle and hasattr(runtime, "_background"):
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
