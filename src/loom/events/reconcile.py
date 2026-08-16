"""Turning a pointer into events — the other half of shape B.

A push-pointer provider (Gmail, Graph delta) tells you *that* something changed
and where you are, not *what* changed. A reconciler closes that: it subscribes to
the pointer topic like any other consumer, asks the provider what happened
between its cursor and the new position, and appends the resulting data events
back to the log.

The consequence worth stating plainly: **downstream subscribers never learn that
the provider is different.** A workflow reads ``app.gmail.message`` exactly as it
reads ``app.slack.message``, and switching a provider from push-data to
push-pointer is a source change, not a workflow change.

It is a subscriber, not a special case. It has a checkpoint, it resumes where it
stopped, one bad pointer dead-letters rather than stalling it, and it is driven
by the same :class:`~loom.events.dispatcher.EventDispatcher` loop. That is what
keeps the durability story to one mechanism rather than two.

**The gap is the important part.** A cursor the provider will no longer honour
means *we do not know what we missed*. Jumping quietly to the current position is
the failure where "nothing arrived today" and "we lost a day" are
indistinguishable — so instead the cursor resets, a ``*.gap`` event is appended,
and a workflow can subscribe to lost visibility and act on it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loom.events.ingress import topic_for
from loom.events.models import EventRecord
from loom.events.sources import InboundEvent, SourceState

if TYPE_CHECKING:
    from loom.events.log import Checkpoints, EventLog
    from loom.events.models import StoredEvent

logger = logging.getLogger("workflow.events")

__all__ = ["CursorExpired", "Expansion", "PointerReconciler", "Reconciler"]


class CursorExpired(Exception):  # noqa: N818 - names the state, not an error class
    """The provider will no longer honour the cursor we hold.

    Raised by an :class:`Reconciler` when a position is older than the provider
    retains — Gmail's ~week of history, Salesforce's 72 hours. Its own type
    because the recovery is not a retry and not a skip: reset to now, **and**
    record that a stretch of time cannot be accounted for.
    """

    def __init__(self, message: str, *, cursor: str = "") -> None:
        super().__init__(message)
        self.cursor = cursor


@dataclass(frozen=True, slots=True)
class Expansion:
    """What a provider said had changed."""

    events: Sequence[InboundEvent] = field(default_factory=tuple)
    cursor: str = ""
    """Where the next read starts. Persisted **after** the events are appended,
    never before — the same rule as every other marker in this system."""
    complete: bool = True
    """``False`` when the provider had more than one read's worth. The cursor
    still advances to what *was* read, so the next pass continues rather than
    re-reading; what must not happen is advancing past unread changes."""


@runtime_checkable
class Reconciler(Protocol):
    """Expands one pointer into the events it stands for."""

    id: str
    """Matches the source's id — ``gmail`` — so a topic and a reconciler can be
    paired without a second name to keep in step."""

    async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
        """Ask the provider what changed between *cursor* and *pointer*.

        *cursor* is what this reconciler last persisted, or ``""`` the first
        time. A first run with no cursor should return no events and adopt the
        pointer's position: a reconciler that back-fills on first sight would
        replay a mailbox into a workflow that replies.

        :raises CursorExpired: the provider no longer holds that position.
        """
        ...


class PointerReconciler:
    """Drives a :class:`Reconciler` over a pointer topic.

    Wired as a subscriber, so it inherits everything the dispatcher already
    guarantees, and adds only the two things a pointer shape needs: a
    provider-side cursor, and a gap event when that cursor dies.

        rt = Runtime(store=store, events=StoreBackedEventLog(store))
        reconciler = PointerReconciler(GmailReconciler(client), log=rt.events,
                                       checkpoints=StoreBackedCheckpoints(store),
                                       state=SourceState(store, "gmail"))
        await reconciler.drain()      # or hand it to a scheduler

    The cursor lives in :class:`~loom.events.sources.SourceState` rather than in
    a checkpoint, and the distinction is real: the checkpoint says how far this
    reconciler has read *our* log, the cursor says how far it has read *the
    provider's*. Conflating them means a replay of our log re-reads the
    provider, and a provider outage rewinds our log.
    """

    def __init__(
        self,
        reconciler: Reconciler,
        *,
        log: EventLog,
        checkpoints: Checkpoints,
        state: SourceState | None = None,
        pointer_topic: str = "",
        subscriber: str = "",
        batch_size: int = 50,
        on_gap: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._log = log
        self._marks = checkpoints
        self._state = state
        self._topic = pointer_topic or topic_for(f"{reconciler.id}.push")
        self._subscriber = subscriber or f"{reconciler.id}-reconciler"
        self._batch = batch_size
        self._on_gap = on_gap

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def subscriber(self) -> str:
        return self._subscriber

    async def drain(self) -> int:
        """One pass. Returns how many data events were appended.

        Ordering, again, and for the last time in this system: read, expand,
        **append**, persist the provider cursor, *then* commit the checkpoint.
        Committing the checkpoint before the append would drop a pointer whose
        changes were never fetched, and no provider resends a Pub/Sub message
        already acked.
        """
        after = await self._marks.load(self._subscriber, self._topic)
        batch = await self._log.read(self._topic, after=after, limit=self._batch)
        if not batch:
            return 0

        appended = 0
        highest: str | None = None

        for pointer in batch:
            try:
                appended += await self._reconcile_one(pointer)
            except CursorExpired as exc:
                # Not an error to retry: the position is gone and asking again
                # produces the same 404 forever.
                await self._record_gap(pointer, exc)
            except Exception:
                logger.exception(
                    "reconciling %s failed; its checkpoint is unchanged, so it "
                    "will be retried",
                    pointer.event_id,
                )
                break
            highest = pointer.position

        if highest is not None:
            await self._marks.commit(self._subscriber, self._topic, highest)
        return appended

    async def _reconcile_one(self, pointer: StoredEvent) -> int:
        cursor = await self._load_cursor()
        expansion = await self._reconciler.expand(dict(pointer.payload), cursor)

        appended = 0
        for topic, records in _group(expansion.events, pointer).items():
            await self._log.append(topic, records)
            appended += len(records)

        # After the append, always. The events are durable; a crash here costs
        # one repeated read, which the data events' own ids deduplicate away.
        if expansion.cursor:
            await self._save_cursor(expansion.cursor)
        if not expansion.complete:
            logger.info(
                "%s: provider had more than one read's worth; the cursor "
                "advanced to what was read and the next pass continues",
                self._subscriber,
            )
        return appended

    async def _record_gap(self, pointer: StoredEvent, exc: CursorExpired) -> None:
        """Append a ``*.gap`` event and reset to the pointer's own position.

        A gap is an *event*, not an exception, which falls straight out of
        having a log and would be awkward in any other shape: a workflow can
        subscribe to "we lost visibility on this mailbox" and re-scan, alert, or
        page — none of which is possible if the only trace is a log line.
        """
        payload = dict(pointer.payload)
        gap_type = f"{self._reconciler.id}.gap"
        record = EventRecord(
            event_id=f"{topic_for(gap_type)}/{pointer.event_id}",
            type=gap_type,
            payload={
                "reason": str(exc),
                "expired_cursor": exc.cursor,
                "resumed_at": payload.get("historyId") or payload.get("cursor") or "",
                "pointer": payload,
            },
            key=pointer.record.key,
            source=self._reconciler.id,
        )
        logger.error(
            "%s: cursor %s is no longer held by the provider; recording a gap "
            "rather than skipping silently to now",
            self._subscriber,
            exc.cursor,
        )
        with contextlib.suppress(Exception):
            await self._log.append(topic_for(gap_type), [record])

        # Reset forward. Not doing so would leave every subsequent pass raising
        # the same expiry, so nothing would ever be read again.
        resumed = str(payload.get("historyId") or payload.get("cursor") or "")
        if resumed:
            await self._save_cursor(resumed)
        if self._on_gap is not None:
            with contextlib.suppress(Exception):
                await self._on_gap(exc.cursor, resumed)

    async def _load_cursor(self) -> str:
        if self._state is None:
            return ""
        return str(await self._state.get("cursor", "") or "")

    async def _save_cursor(self, cursor: str) -> None:
        if self._state is not None:
            await self._state.set("cursor", cursor)


def _group(
    events: Sequence[InboundEvent], pointer: StoredEvent
) -> dict[str, list[EventRecord]]:
    """Data events, keyed by topic, with ids that survive an overlapping read.

    The id comes from the **event's own** dedupe suffix rather than the
    pointer's, and that is what makes an out-of-order or duplicated pointer
    harmless: two overlapping history reads produce the same message ids, so
    the second append is a no-op. Deriving it from the pointer instead would
    make every re-read a fresh set of events.
    """
    grouped: dict[str, list[EventRecord]] = {}
    for index, event in enumerate(events):
        topic = topic_for(event.type)
        suffix = event.dedupe_suffix or f"{pointer.event_id}#{index}"
        grouped.setdefault(topic, []).append(
            EventRecord(
                event_id=f"{topic}/{suffix}",
                type=event.type,
                payload=dict(event.payload),
                key=event.key,
                source=pointer.record.source,
                occurred_at=event.occurred_at,
            )
        )
    return grouped
