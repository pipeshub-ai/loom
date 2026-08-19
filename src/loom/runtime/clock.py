"""Where the runtime reads the time, and how it waits.

A durable workflow is mostly a thing that waits: sleep four minutes, wake at 9am
on the first of the month, retry in eight seconds. Testing that against the wall
clock means either waiting for real or reaching under the covers to patch
`asyncio.sleep` — and the second one is what this module exists to replace,
because a test that patches an implementation detail passes until the detail
moves.

So the runtime reads the time through a port:

`SystemClock`
    The wall clock. The default, and what production uses.
`ManualClock`
    Time you move by hand. `now()` returns whatever it was last set to, and
    `sleep()` moves it forward instead of waiting.

The second is the interesting one. `ManualClock.sleep(240)` returns immediately
*and* advances 240 seconds, so a workflow that slept four minutes observes that
four minutes passed — its own `ctx.now()` agrees with the sleep it just did.
Returning without advancing would be faster and wrong: the workflow would come
back believing no time had passed, and any logic comparing timestamps would take
a branch it never takes in production.

`ManualClock(park=True)` is the **second** mode, and it exists because that
answer is right for work a test is waiting on and wrong for a service polling
in the background. `while True: await clock.sleep(interval); await sweep()`
against the default clock does not wait at all: it runs as fast as the event
loop will let it and drags virtual time along with it, a day per turn for a
renewer on a daily interval. In park mode a sleep suspends until somebody
*else* moves the clock past its deadline, which is what makes a background loop
in a test step once per `advance()` instead of thousands of times per second.

Park mode is opt-in rather than the default, and the reason is specific:
``Runtime._drive`` keeps a heartbeat task on a **real** ``asyncio.sleep`` for
the whole of every run — deliberately, since a lease is a claim against other
processes on wall time. So while a workflow body is executing there is always a
task nothing virtual will ever wake, and a `ctx.sleep` inside that body has
nobody to advance the clock for it. Parking by default would hang every inline
timer under a `ManualClock`. Reach for park mode when the thing under test is a
loop; leave it off when the thing under test is a run.

>>> from datetime import UTC, datetime, timedelta
>>> from loom.runtime.clock import ManualClock
>>> clock = ManualClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
>>> clock.advance(timedelta(minutes=5)).isoformat()
'2026-01-01T09:05:00+00:00'
>>> clock.now().hour, clock.now().minute
(9, 5)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from loom.core.types import Duration, to_seconds

__all__ = ["Clock", "ManualClock", "SystemClock"]

#: How long a parked sleeper waits in *real* seconds before giving up loudly.
#:
#: A test-hang guard, not a schedule — the same role the timeout in
#: :func:`loom.testing.settled` plays, and measured the same way, off the event
#: loop rather than off the virtual clock (a virtual clock that nobody is
#: advancing cannot time its own deadlock). Without it, a test that starts a
#: background loop and forgets to advance hangs forever with no output, which
#: is the one failure mode worse than a wrong answer.
PARK_TIMEOUT = 15.0


@runtime_checkable
class Clock(Protocol):
    """The runtime's only source of "now" and "wait"."""

    def now(self) -> datetime:
        """The current time, timezone-aware and in UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for *seconds*, however this clock understands waiting."""
        ...


class SystemClock:
    """The wall clock. The default, and the only one production should use."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def __repr__(self) -> str:
        return "<SystemClock>"


class ManualClock:
    """Time under the test's control.

    Two ways time moves, and the difference matters:

    **You move it.** `advance()` and `set()` are what a test calls to reach a
    moment — the first of the month, 9am on a Tuesday, four minutes from now.
    Nothing happens as a side effect; pair it with `runtime.tick()`, or use
    :func:`loom.testing.advance`, which does both.

    **A sleep moves it.** `sleep()` advances rather than waits, so a workflow
    holding a short timer in memory finishes instantly with its own clock
    correctly ahead. This is what stops a four-minute `ctx.sleep` from costing a
    four-minute test.

    **Unless it parks.** `ManualClock(park=True)` inverts the second rule: a
    sleep then suspends until the clock is advanced past its deadline, and
    advancing it is somebody else's job. That is the mode for a background
    loop — a dispatcher, a renewer, a credential sweep — because those wait on
    a timer they own, and a sleep that returns instantly turns their `while
    True` into a spin that also drags virtual time along with it. See the
    module docstring for why this is not the default.

    Not thread-safe and not intended to be: a test drives one timeline.
    """

    def __init__(
        self,
        start: datetime | None = None,
        *,
        park: bool = False,
        park_timeout: float = PARK_TIMEOUT,
    ) -> None:
        moment = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        self._park = park
        self._park_timeout = park_timeout
        self._waiters: list[_Parked] = []
        self.slept: list[float] = []
        """Every duration `sleep()` was asked for, in order.

        Worth asserting on. "The run completed" does not distinguish a workflow
        that waited the right amount from one whose timer was skipped, and this
        is the difference. Recorded in both modes, and recorded when the sleep
        *starts* — a parked sleeper that is still waiting is already in here,
        which is what lets a test assert that a loop asked for its interval
        without also having to let it finish."""

    @property
    def parks(self) -> bool:
        """Whether a `sleep()` waits to be released instead of advancing."""
        return self._park

    @property
    def parked(self) -> int:
        """How many sleepers are waiting for the clock to reach them.

        The observable a background-loop test asserts on: a loop that has come
        round to its timer is parked, and one that has not is still working.
        Without it the only way to tell them apart is to sleep for real and
        hope."""
        return len(self._waiters)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> datetime:
        """Jump to an absolute moment. Returns the new time.

        Going backwards is allowed — testing a clock skew or a late-arriving
        event needs it — so this does not guard against it. `advance()` is the
        one to reach for when you mean "later".
        """
        self._now = when if when.tzinfo else when.replace(tzinfo=UTC)
        self._release()
        return self._now

    def advance(self, delta: Duration | timedelta = 0.0, **units: float) -> datetime:
        """Move forward. Returns the new time.

        Accepts a `timedelta`, a number of seconds, or keyword units::

            clock.advance(timedelta(hours=2))
            clock.advance(90)
            clock.advance(minutes=5, seconds=30)
        """
        step = delta if isinstance(delta, timedelta) else timedelta(
            seconds=to_seconds(delta)
        )
        if units:
            step += timedelta(**units)
        self._now += step
        self._release()
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance by *seconds* rather than waiting for them — or park.

        Yields to the event loop once, so anything else pending gets a turn —
        without it a workflow looping on short sleeps would starve the loop and
        never let a concurrent branch progress.

        In park mode this instead waits for the clock to reach
        ``now + seconds``, which only :meth:`advance` or :meth:`set` can do.
        A sleeper nothing releases raises after :data:`PARK_TIMEOUT` *real*
        seconds rather than hanging: a forgotten `advance()` and a genuinely
        idle system are indistinguishable from in here, and a test that hangs
        forever reports nothing at all.
        """
        self.slept.append(seconds)
        if not self._park:
            if seconds > 0:
                self.advance(seconds)
            await asyncio.sleep(0)
            return

        if seconds <= 0:
            await asyncio.sleep(0)
            return

        loop = asyncio.get_running_loop()
        parked = _Parked(self._now + timedelta(seconds=seconds), loop.create_future())
        self._waiters.append(parked)
        try:
            await asyncio.wait_for(parked.future, self._park_timeout)
        except TimeoutError:
            raise TimeoutError(
                f"a parked sleep of {seconds}s is still waiting after "
                f"{self._park_timeout:.0f} real seconds. This ManualClock is on "
                f"{self._now.isoformat()} and the sleeper wants "
                f"{parked.deadline.isoformat()}; nothing moved the clock that "
                "far. Call clock.advance(...) — or loom.testing.advance(rt, ...) "
                "— from the test, or drop park=True if this sleep is inside "
                "work the test is itself awaiting."
            ) from None
        finally:
            if parked in self._waiters:
                self._waiters.remove(parked)

    def _release(self) -> None:
        """Wake every sleeper the clock has now reached.

        Called from `advance()` and `set()` rather than from a background pump,
        so the timeline moves only where a test says it does. A pump would have
        to guess when the system was idle, and guessing wrong in the permissive
        direction reintroduces exactly the free-running advance park mode
        exists to stop.
        """
        if not self._waiters:
            return
        for parked in list(self._waiters):
            if parked.deadline > self._now:
                continue
            self._waiters.remove(parked)
            if not parked.future.done():
                parked.future.set_result(None)

    def __repr__(self) -> str:
        mode = ", parked" if self._park else ""
        return f"<ManualClock {self._now.isoformat()}{mode}>"


class _Parked:
    """One sleeper, and the virtual moment that releases it."""

    __slots__ = ("deadline", "future")

    def __init__(self, deadline: datetime, future: asyncio.Future[None]) -> None:
        self.deadline = deadline
        self.future = future
