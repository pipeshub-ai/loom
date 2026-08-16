"""Keeping a provider-side subscription alive, and noticing when it is not.

This is the highest-severity failure in the whole backbone and it is completely
silent. Gmail's ``watch()`` dies after seven days. Microsoft Graph subscriptions
last about three. When one lapses no events arrive, nothing errors, and the
workflow looks *idle* rather than broken — **absence of events is
indistinguishable from absence of activity** unless something is watching for it.

Two mechanisms, and they answer different questions:

:class:`WatchRenewer`
    "Is the provider still going to tell us?" A leased periodic task that
    re-registers anything nearing expiry, on a cadence that is a *fraction* of
    the lifetime — daily against Gmail's seven days — so that several
    consecutive failures are survivable rather than terminal.

:class:`Heartbeat`
    "Has anything actually arrived?" A source that normally sees traffic and has
    gone quiet is a *symptom*, and it should surface in a status command rather
    than be discovered by somebody wondering why nobody was paged.

Neither invents a scheduler. ``WatchRenewer`` registers through
``Runtime.supervise()``, so ``shutdown()`` stops it, and takes the same
``Clock`` port everything else in the engine reads — which is what makes a
seven-day expiry testable in milliseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from loom.events.log import EventLog
    from loom.runtime.clock import Clock

logger = logging.getLogger("workflow.events")

__all__ = [
    "Heartbeat",
    "Watch",
    "WatchRegistration",
    "WatchRenewer",
    "WatchStatus",
    "lifetime_hint",
]

#: Renew when this much of the lifetime is left. A *fraction*, not a fixed
#: margin: a seven-day watch and a three-day one need different absolute
#: warning, and hard-coding one silently under-protects the shorter.
RENEW_AT_FRACTION = 0.5

#: The floor, for a provider whose expiry is unknown or implausibly short.
MIN_RENEW_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class WatchRegistration:
    """What a provider gave back when a subscription was established."""

    resource: str
    """What is being watched — a mailbox address, a Graph resource path. It is
    the identity, so renewing produces a registration with the same one."""
    expires_at: datetime | None = None
    """``None`` means the provider did not say. Treated as *expiring now*, not
    as *never*: an unnecessary renewal costs one API call, a missed one costs a
    silent week."""
    cursor: str = ""
    """Where reading should start. A watch established now says nothing about
    what happened before it, so a first registration's cursor is the position to
    adopt rather than to back-fill from."""
    metadata: dict[str, Any] = field(default_factory=dict)

    def due(self, *, now: datetime, fraction: float = RENEW_AT_FRACTION) -> bool:
        if self.expires_at is None:
            return True
        remaining = (self.expires_at - now).total_seconds()
        return remaining <= 0 or remaining <= self._lifetime() * fraction

    def _lifetime(self) -> float:
        """Assume a week when the provider gave no issue time.

        Deliberately the *longest* plausible lifetime, because it makes the
        renewal window widest and therefore renews soonest. Guessing short would
        renew late.
        """
        hint = self.metadata.get("lifetime_seconds")
        if isinstance(hint, int | float) and hint > 0:
            return float(hint)
        return 7 * 24 * 3600.0


@runtime_checkable
class Watch(Protocol):
    """A provider-side subscription that expires and must be re-established."""

    id: str
    """Matches the source id, so a watch and its events share one name."""

    async def register(self, resource: str) -> WatchRegistration:
        """Establish or renew the subscription for *resource*.

        Must be **idempotent for one resource**: renewal calls it again on a
        live watch, and providers differ on whether that extends the existing
        subscription or creates a second. A Watch that creates duplicates turns
        a daily renewal into a fan-out of notifications.
        """
        ...

    async def stop(self, resource: str) -> None:
        """Tear the subscription down. Called on an explicit unsubscribe, never
        on shutdown — a process restarting must not deafen the mailbox."""
        ...


@dataclass
class WatchStatus:
    """What the renewer knows about one resource."""

    resource: str
    registration: WatchRegistration | None = None
    last_renewed: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.registration is not None and not self.last_error

    @property
    def expires_at(self) -> datetime | None:
        return self.registration.expires_at if self.registration else None


class WatchRenewer:
    """Re-registers provider subscriptions before they lapse.

    Not built on cron, and for the reason the credential refresher gives:
    ``TriggerDispatcher`` fires workflows, which means an ``ExecutionRecord``
    and a journal per occurrence — thousands of run records a year to keep one
    subscription alive. A renewal is idempotent and self-healing, so a missed
    one should simply be retried rather than back-filled. What *is* reused is
    the machinery around it: ``supervise()``, the ``Clock`` port, and
    ``LockProvider`` for cross-process single-flight.

        renewer = WatchRenewer(GmailWatch(client), runtime=rt)
        renewer.track("team@example.com")
        await renewer.start()
    """

    def __init__(
        self,
        watch: Watch,
        *,
        runtime: Any = None,
        log: EventLog | None = None,
        clock: Clock | None = None,
        interval_seconds: float = 24 * 3600.0,
        fraction: float = RENEW_AT_FRACTION,
        on_registered: Callable[[str, WatchRegistration], Awaitable[None]] | None = None,
    ) -> None:
        self._watch = watch
        self._runtime = runtime
        self._log = log if log is not None else getattr(runtime, "events", None)
        self._clock = clock or getattr(runtime, "clock", None) or _SystemClock()
        self._interval = max(interval_seconds, MIN_RENEW_INTERVAL)
        self._fraction = fraction
        self._on_registered = on_registered
        self._tracked: dict[str, WatchStatus] = {}
        self._task: asyncio.Task[None] | None = None

    # -- registration --------------------------------------------------------

    def track(self, resource: str) -> WatchStatus:
        """Add *resource* to what this renewer keeps alive."""
        return self._tracked.setdefault(resource, WatchStatus(resource))

    def untrack(self, resource: str) -> None:
        self._tracked.pop(resource, None)

    @property
    def statuses(self) -> dict[str, WatchStatus]:
        """What is tracked and how it is doing. This is what a status command
        reads, and the reason ``last_error`` is kept rather than only logged."""
        return dict(self._tracked)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Sweep immediately, then on a timer.

        Immediately, because that is what "on restart" means: a process that has
        been down for a day comes back to watches that expired while it was
        gone, and waiting a full interval before checking makes the outage
        longer than the downtime.
        """
        if self._task is not None:
            return
        await self.sweep()
        self._task = asyncio.create_task(self._loop())
        supervise = getattr(self._runtime, "supervise", None)
        if supervise is not None:
            supervise(self)

    async def stop(self) -> None:
        """Stop renewing. Safe before start, and twice.

        Does **not** call ``watch.stop()``: a process restarting must not deafen
        the mailbox, and a deployment that tore its subscriptions down on every
        rolling restart would lose every event in the gap.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        unsupervise = getattr(self._runtime, "unsupervise", None)
        if unsupervise is not None:
            unsupervise(self)

    async def _loop(self) -> None:
        while True:
            await self._clock.sleep(self._interval)
            await self.sweep()

    # -- the sweep -----------------------------------------------------------

    async def sweep(self) -> list[str]:
        """Renew everything due. Returns what was renewed.

        One resource's failure never stops another's — the same isolation a
        scheduler tick gives one failing trigger, and for the same reason: the
        alternative is one dead mailbox silencing every other.
        """
        now = self._clock.now()
        renewed: list[str] = []

        for resource, status in list(self._tracked.items()):
            if status.registration is not None and not status.registration.due(
                now=now, fraction=self._fraction
            ):
                continue
            try:
                registration = await self._watch.register(resource)
            except Exception as exc:
                status.last_error = f"{type(exc).__name__}: {exc}"
                status.consecutive_failures += 1
                logger.error(
                    "could not renew the %s watch on %s (failure %d): %s. It "
                    "expires at %s; if that passes, notifications stop with no "
                    "further error.",
                    self._watch.id,
                    resource,
                    status.consecutive_failures,
                    exc,
                    status.expires_at,
                )
                await self._announce_lapse(status, now)
                continue

            status.registration = registration
            status.last_renewed = now
            status.last_error = ""
            status.consecutive_failures = 0
            renewed.append(resource)
            logger.info(
                "renewed the %s watch on %s until %s",
                self._watch.id,
                resource,
                registration.expires_at,
            )
            if self._on_registered is not None:
                with contextlib.suppress(Exception):
                    await self._on_registered(resource, registration)

        return renewed

    async def _announce_lapse(self, status: WatchStatus, now: datetime) -> None:
        """Append a ``*.watch_lapsed`` event once a watch is actually dead.

        Only once it has *expired*, not on the first failed renewal: the whole
        point of renewing at a fraction of the lifetime is that early failures
        are survivable, and an alert on each of them trains people to ignore the
        one that matters.
        """
        if self._log is None:
            return
        expires_at = status.expires_at
        if expires_at is not None and expires_at > now:
            return

        from loom.events.ingress import topic_for
        from loom.events.models import EventRecord

        event_type = f"{self._watch.id}.watch_lapsed"
        stamp = (expires_at or now).isoformat()
        with contextlib.suppress(Exception):
            await self._log.append(
                topic_for(event_type),
                [
                    EventRecord(
                        # Keyed by the expiry rather than by `now`, so a renewer
                        # retrying every hour against a dead credential appends
                        # one event rather than one an hour.
                        event_id=f"{topic_for(event_type)}/{status.resource}@{stamp}",
                        type=event_type,
                        payload={
                            "resource": status.resource,
                            "expired_at": stamp,
                            "error": status.last_error,
                            "consecutive_failures": status.consecutive_failures,
                        },
                        key=status.resource,
                        source=self._watch.id,
                    )
                ],
            )


class Heartbeat:
    """Notices that a source which normally sees traffic has gone quiet.

    The third gap-detection route, and the only one that needs no cooperation
    from the provider. It is deliberately a *symptom* rather than an alarm: a
    quiet Saturday and a lapsed subscription look the same from here, so this
    reports and lets a human or a status command judge.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or _SystemClock()
        self._seen: dict[str, datetime] = {}
        self._expected: dict[str, float] = {}

    def expect(self, topic: str, within_seconds: float) -> None:
        """Declare that *topic* normally sees something this often."""
        self._expected[topic] = within_seconds
        self._seen.setdefault(topic, self._clock.now())

    def saw(self, topic: str) -> None:
        self._seen[topic] = self._clock.now()

    def quiet(self) -> dict[str, float]:
        """Topics past their expected interval, and by how many seconds."""
        now = self._clock.now()
        late: dict[str, float] = {}
        for topic, window in self._expected.items():
            last = self._seen.get(topic)
            if last is None:
                continue
            overdue = (now - last).total_seconds() - window
            if overdue > 0:
                late[topic] = overdue
        return late


class _SystemClock:
    """The fallback when no Runtime is in the picture. Kept private: the real
    ``Clock`` port lives in ``runtime/clock.py`` and is what a test swaps."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def lifetime_hint(seconds: float) -> dict[str, Any]:
    """Metadata declaring how long this provider's subscriptions last.

    Worth setting: without it :meth:`WatchRegistration.due` assumes a week, and
    a Graph subscription living three days would then be renewed at the
    three-and-a-half-day mark — which is to say, after it died.
    """
    return {"lifetime_seconds": float(seconds)}

