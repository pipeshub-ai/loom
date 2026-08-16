"""Turning recorded events into runs.

One loop per subscriber, and the order inside it is the entire correctness
argument:

1. load the checkpoint — where this subscriber got to;
2. read a batch after it;
3. drop what the filter rejects;
4. **submit**, under a key derived from ``{event_id}#{subscriber}``;
5. **only then** commit the checkpoint.

Committing before the submits are durable loses events permanently: the marker
says "handled", nothing ran, and no provider will send them again. Committing
after costs at most a re-read, and the dispatch key turns that re-read into
nothing. Every hop in this system follows the same rule — *do the durable
idempotent thing first, advance the marker last* — and this is where it lands.

Fan-out is why the checkpoint is per subscriber rather than per topic. Two
workflows reading one topic hold two positions, catch up independently, and
neither can hold the other back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.core.exceptions import AdmissionRejected, ConfigurationError, RegistryError
from loom.events.models import EventRecord, StoredEvent
from loom.events.subscription import StartAt, Subscription

if TYPE_CHECKING:
    from loom.events.log import Checkpoints, EventLog
    from loom.runtime.engine import Runtime

__all__ = ["CHAIN_DEPTH_CAP", "DEAD_LETTER_SUFFIX", "DispatchReport", "EventDispatcher"]

logger = logging.getLogger("workflow.events")

#: How many workflow-to-workflow hops an event may cause before it is dropped.
#:
#: A workflow can publish an event that triggers a workflow that publishes…
#: Unifying ``ctx.publish`` with external events makes that cycle easy to write
#: by accident, and the first one anybody writes takes the system down. Five is
#: deep enough for any chain that was designed and shallow enough that a loop
#: costs five runs rather than the cluster.
CHAIN_DEPTH_CAP = 5

#: Where an event nobody could process ends up. A real topic, so it is
#: subscribable and inspectable rather than a log line nobody reads.
DEAD_LETTER_SUFFIX = ".dead"

_DEFAULT_BATCH = 100
_DEFAULT_IDLE_WAIT = 1.0


@dataclass
class DispatchReport:
    """What one pass over one subscription did."""

    subscriber: str
    topic: str
    read: int = 0
    matched: int = 0
    started: list[str] = field(default_factory=list)
    filtered: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    deferred: int = 0
    """Events left for the next pass because the failure looked transient."""

    @property
    def committed_through(self) -> str | None:
        return self._committed

    _committed: str | None = None


class EventDispatcher:
    """Drives runs from an :class:`~loom.events.log.EventLog`.

    Registered subscriptions come from two places, and both end up in the same
    list: :meth:`register`, which reads a workflow's ``OnAppEvent`` triggers,
    and :meth:`subscribe`, for a host wiring one up by hand or for a replay.

    Started with :meth:`start`, which registers with ``Runtime.supervise`` so
    ``runtime.shutdown()`` stops it — the same seam ``TriggerDispatcher`` and
    ``QueueConsumer`` already use, so a host does not have to know which
    background services it happens to have wired up.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        log: EventLog | None = None,
        checkpoints: Checkpoints | None = None,
        batch_size: int = _DEFAULT_BATCH,
        idle_wait: float = _DEFAULT_IDLE_WAIT,
        deliver_to_waiting: bool = True,
    ) -> None:
        resolved_log = log if log is not None else getattr(runtime, "events", None)
        if resolved_log is None:
            raise ConfigurationError(
                "EventDispatcher needs an EventLog. Pass log=..., or construct "
                "the Runtime with events=StoreBackedEventLog(store) — which "
                "needs no infrastructure beyond the store you already have."
            )
        resolved_marks = (
            checkpoints
            if checkpoints is not None
            else getattr(runtime, "checkpoints", None)
        )
        if resolved_marks is None:
            from loom.events.log import StoreBackedCheckpoints

            resolved_marks = StoreBackedCheckpoints(runtime.store)

        self._runtime = runtime
        self._log = resolved_log
        self._marks = resolved_marks
        self._batch_size = batch_size
        self._idle_wait = idle_wait
        self._deliver_to_waiting = deliver_to_waiting
        self._subscriptions: list[Subscription] = []
        #: Delivery attempts per (subscriber, position), in memory and
        #: deliberately not durable. A restart forgets them, so a process
        #: crash-looping on one poison event retries it past `max_attempts` —
        #: which is the right trade: the alternative is a store write on every
        #: *failed* dispatch, and the checkpoint is already the durable record
        #: of progress. What is bounded here is a wedged subscriber inside one
        #: process lifetime, which is the case that actually stalls.
        self._attempts: dict[tuple[str, str], int] = {}
        self._task: asyncio.Task[None] | None = None

    # -- registration --------------------------------------------------------

    async def subscribe(self, subscription: Subscription) -> Subscription:
        """Add a subscription, pinning where it starts **now**.

        Async, and that is the point. ``LATEST`` has to mean "everything from
        the moment I subscribed", so the position is claimed here rather than
        at the first poll — otherwise every event arriving in the gap between
        subscribing and polling is skipped, silently, which is the exact class
        of loss this system exists to rule out.
        """
        clash = next(
            (
                s
                for s in self._subscriptions
                if s.subscriber == subscription.subscriber
                and s.topic == subscription.topic
            ),
            None,
        )
        if clash is not None and clash != subscription:
            raise ConfigurationError(
                f"two different subscriptions both call themselves "
                f"'{subscription.subscriber}' on topic '{subscription.topic}'. "
                "They would share one checkpoint and silently consume each "
                "other's backlog — give them distinct subscription names."
            )
        if clash is None:
            self._subscriptions.append(subscription)
        await self._pin(subscription)
        return subscription

    async def _pin(self, subscription: Subscription) -> None:
        """Claim a starting position for a subscriber that has never committed.

        ``LATEST`` claims whatever is already there as seen, without running it.
        ``EARLIEST`` claims nothing, so the first read starts at the oldest
        record still retained.
        """
        if await self._marks.load(subscription.subscriber, subscription.topic):
            return
        if subscription.start_at is not StartAt.LATEST:
            return
        head = await self._log.head(subscription.topic)
        if head is not None:
            await self._marks.commit(
                subscription.subscriber, subscription.topic, head
            )

    async def register(self, definition: Any) -> list[Subscription]:
        """Take a workflow's ``OnAppEvent`` triggers as subscriptions.

        Also registers the workflow on the runtime, so that declaring a trigger
        is the only step — a workflow whose trigger is known but whose name the
        runtime cannot resolve fails at dispatch, long after the mistake.
        """
        from loom.triggers.specs import OnAppEvent

        self._runtime.register(definition)
        added: list[Subscription] = []
        for spec in getattr(definition, "triggers", ()):
            if isinstance(spec, OnAppEvent):
                added.append(
                    await self.subscribe(spec.subscription_for(definition.name))
                )
        return added

    @property
    def subscriptions(self) -> Sequence[Subscription]:
        return tuple(self._subscriptions)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Poll every subscription until :meth:`stop`."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        supervise = getattr(self._runtime, "supervise", None)
        if supervise is not None:
            supervise(self)

    async def stop(self) -> None:
        """Stop polling. Safe before start, and twice."""
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
            reports = await self.poll_once()
            if not any(report.read for report in reports):
                # Nothing anywhere: wait on the first topic rather than
                # spinning. `wait_for` polls by default and blocks natively on
                # an adapter that can, which is what keeps a quiet system from
                # costing thousands of reads a second.
                await self._idle()

    async def _idle(self) -> None:
        """Wait before the next pass, and **always actually wait**.

        The subtlety is the failure mode of not doing so. An adapter whose
        ``wait_for`` raises — unsupported, or a broker mid-outage — used to be
        swallowed and returned from instantly, which turned this loop into a
        busy spin. That is not merely wasteful: a store whose ``async def``
        methods never await anything yields no control, so the task starves the
        event loop rather than sharing it, and every other coroutine in the
        process stops. It presents as a hang, not as a hot CPU.
        """
        if not self._subscriptions:
            await asyncio.sleep(self._idle_wait)
            return
        first = self._subscriptions[0]
        after = await self._marks.load(first.subscriber, first.topic)
        try:
            await self._log.wait_for(
                first.topic, after=after, timeout=self._idle_wait
            )
        except Exception:
            logger.debug(
                "event log could not wait on '%s'; falling back to a timed "
                "sleep for this pass",
                first.topic,
                exc_info=True,
            )
            await asyncio.sleep(self._idle_wait)

    # -- the pass ------------------------------------------------------------

    async def poll_once(self) -> list[DispatchReport]:
        """One pass over every subscription.

        Per-subscription isolation is deliberate: a broken subscription must not
        stop the others, exactly as one failing trigger must not stop a
        scheduler tick. Its own checkpoint simply does not advance, so it
        retries and everyone else proceeds.
        """
        reports: list[DispatchReport] = []
        for subscription in list(self._subscriptions):
            try:
                reports.append(await self.drain(subscription))
            except Exception:
                logger.exception(
                    "dispatch pass failed for '%s' on '%s'; its checkpoint is "
                    "unchanged, so it will retry",
                    subscription.subscriber,
                    subscription.topic,
                )
                reports.append(
                    DispatchReport(subscription.subscriber, subscription.topic)
                )
        return reports

    async def drain(self, subscription: Subscription) -> DispatchReport:
        """Read one batch for *subscription*, dispatch it, and commit."""
        report = DispatchReport(subscription.subscriber, subscription.topic)
        after = await self._position_for(subscription)

        batch = await self._log.read(
            subscription.topic, after=after, limit=self._batch_size
        )
        report.read = len(batch)
        if not batch:
            return report

        highest: str | None = None
        for index, event in enumerate(batch):
            outcome = await self._dispatch(subscription, event, report)
            if outcome is _DEFER:
                # Stop here rather than skipping ahead: committing past an event
                # that has not been handled is the loss this ordering exists to
                # prevent, and the events after it are not more important than
                # the one that failed. Everything from this one on — itself
                # included — is left for the next pass.
                report.deferred = len(batch) - index
                break
            highest = event.position

        if highest is not None:
            # Last. Everything it covers is already durable.
            await self._marks.commit(
                subscription.subscriber, subscription.topic, highest
            )
            report._committed = highest
        return report

    async def _position_for(self, subscription: Subscription) -> str | None:
        """Where to read from.

        Just the checkpoint: ``start_at`` was resolved into one when the
        subscription was added (:meth:`_pin`). ``None`` means read from the
        oldest record still retained, which is what ``EARLIEST`` leaves behind.
        """
        return await self._marks.load(subscription.subscriber, subscription.topic)

    async def _dispatch(
        self,
        subscription: Subscription,
        event: StoredEvent,
        report: DispatchReport,
    ) -> Any:
        if event.record.chain_depth >= CHAIN_DEPTH_CAP:
            logger.warning(
                "event %s hit the chain-depth cap (%d) and was dropped; a "
                "workflow is publishing an event that re-triggers it",
                event.event_id,
                CHAIN_DEPTH_CAP,
            )
            return None

        if not subscription.accepts(dict(event.payload)):
            report.filtered += 1
            return None

        report.matched += 1
        attempts_key = (subscription.subscriber, event.position)

        try:
            run_id = await self._runtime.submit(
                subscription.workflow,
                dict(event.payload),
                idempotency_key=f"{event.event_id}#{subscription.subscriber}",
                metadata={
                    "loom.event_id": event.event_id,
                    "loom.event_type": event.record.type,
                    "loom.topic": subscription.topic,
                    "loom.subscriber": subscription.subscriber,
                },
            )
        except (RegistryError, ValueError, TypeError) as exc:
            # Permanent: this event will fail identically forever, and leaving
            # it in place stalls the subscriber. Step over it, loudly.
            await self._dead_letter(subscription, event, exc, report)
            return None
        except AdmissionRejected as exc:
            if getattr(exc, "retryable", False):
                return _DEFER
            await self._dead_letter(subscription, event, exc, report)
            return None
        except Exception as exc:
            attempts = self._attempts.get(attempts_key, 0) + 1
            self._attempts[attempts_key] = attempts
            if attempts >= subscription.max_attempts:
                await self._dead_letter(subscription, event, exc, report)
                return None
            logger.warning(
                "dispatch of %s to '%s' failed (attempt %d/%d): %s",
                event.event_id,
                subscription.workflow,
                attempts,
                subscription.max_attempts,
                exc,
            )
            return _DEFER

        self._attempts.pop(attempts_key, None)
        report.started.append(run_id)

        if self._deliver_to_waiting:
            await self._wake_waiting(event)
        return run_id

    async def _wake_waiting(self, event: StoredEvent) -> None:
        """Resume runs parked on ``ctx.wait_for_event`` for this event type.

        Distinct from starting a run, and both can be right for one event: a
        trigger creates a run, a wait continues one that already exists.
        ``dedupe_key`` is what stops a redelivery advancing a run twice — which,
        unlike a duplicate submit, the idempotency key cannot catch.
        """
        with contextlib.suppress(Exception):
            await self._runtime.send_event(
                None,
                event.record.type,
                dict(event.payload),
                dedupe_key=event.event_id,
            )

    async def _dead_letter(
        self,
        subscription: Subscription,
        event: StoredEvent,
        reason: Exception,
        report: DispatchReport,
    ) -> None:
        """Park an unprocessable event and step over it.

        Bounded attempts then a dead letter, because the two failure modes
        either side of it are both worse: retrying forever stalls the
        subscriber behind one bad event, and skipping silently loses it with no
        record that anything went wrong.
        """
        report.dead_lettered += 1
        logger.error(
            "dead-lettering %s for '%s': %s: %s",
            event.event_id,
            subscription.subscriber,
            type(reason).__name__,
            reason,
        )
        with contextlib.suppress(Exception):
            await self._log.append(
                subscription.topic + DEAD_LETTER_SUFFIX,
                [
                    EventRecord(
                        event_id=f"{event.event_id}#{subscription.subscriber}",
                        type=f"{event.record.type}.dead",
                        payload={
                            "original": dict(event.payload),
                            "event_id": event.event_id,
                            "subscriber": subscription.subscriber,
                            "workflow": subscription.workflow,
                            "error": f"{type(reason).__name__}: {reason}",
                        },
                        key=event.record.key,
                        source=event.record.source,
                    )
                ],
            )
        self._attempts.pop((subscription.subscriber, event.position), None)

    def __repr__(self) -> str:
        return f"<EventDispatcher {len(self._subscriptions)} subscription(s)>"


class _Defer:
    """Sentinel: stop this batch here and retry from the same position."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<defer>"


_DEFER = _Defer()
