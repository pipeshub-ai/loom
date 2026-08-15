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

>>> from datetime import UTC, datetime, timedelta
>>> from workflow_builder.runtime.clock import ManualClock
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

from workflow_builder.core.types import Duration, to_seconds

__all__ = ["Clock", "ManualClock", "SystemClock"]


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
    :func:`workflow_builder.testing.advance`, which does both.

    **A sleep moves it.** `sleep()` advances rather than waits, so a workflow
    holding a short timer in memory finishes instantly with its own clock
    correctly ahead. This is what stops a four-minute `ctx.sleep` from costing a
    four-minute test.

    Not thread-safe and not intended to be: a test drives one timeline.
    """

    def __init__(self, start: datetime | None = None) -> None:
        moment = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        self.slept: list[float] = []
        """Every duration `sleep()` was asked for, in order.

        Worth asserting on. "The run completed" does not distinguish a workflow
        that waited the right amount from one whose timer was skipped, and this
        is the difference."""

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> datetime:
        """Jump to an absolute moment. Returns the new time.

        Going backwards is allowed — testing a clock skew or a late-arriving
        event needs it — so this does not guard against it. `advance()` is the
        one to reach for when you mean "later".
        """
        self._now = when if when.tzinfo else when.replace(tzinfo=UTC)
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
            step += timedelta(**units)  # type: ignore[arg-type]
        self._now += step
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance by *seconds* rather than waiting for them.

        Yields to the event loop once, so anything else pending gets a turn —
        without it a workflow looping on short sleeps would starve the loop and
        never let a concurrent branch progress.
        """
        self.slept.append(seconds)
        if seconds > 0:
            self.advance(seconds)
        await asyncio.sleep(0)

    def __repr__(self) -> str:
        return f"<ManualClock {self._now.isoformat()}>"
