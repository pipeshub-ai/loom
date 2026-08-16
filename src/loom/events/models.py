"""What an event is, on the way in and on the way out.

Two types, deliberately not one. :class:`EventRecord` is what a producer hands
to :meth:`~loom.events.log.EventLog.append` — it has no position, because a
record has no position until the log gives it one. :class:`StoredEvent` is what a
subscriber reads back, and it carries the position it was assigned.

Collapsing them into one type with an optional ``position`` would make every
reader check whether the field is set, and make it possible to construct a record
that claims a position nothing assigned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "Checkpoint",
    "EventRecord",
    "Position",
    "RetentionPolicy",
    "StoredEvent",
]

Position = str
"""Where a record sits in a log — **opaque, and not totally ordered**.

A caller may store one and hand it back to ``read(after=…)``. A caller may not
parse it, compare it, or do arithmetic on it.

Both restrictions exist to keep partitioned backends implementable. A Kafka
topic's position is a set of per-partition offsets with no meaningful total
order; requiring one would force every such backend down to a single partition,
capping throughput permanently in exchange for an ordering nobody asked for. The
only promise is the one resume actually needs: ``read(after=P)`` returns records
this reader has not seen.
"""


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One event, as a producer hands it over."""

    event_id: str
    """Stable identity, and the unit of deduplication.

    Derived, never random: ``{topic}/{source}:{provider_delivery_id}``, or a
    content hash where the provider issues no id. Appending the same
    ``event_id`` twice is a no-op that returns the original position, which is
    what makes a producer safe to retry after a crash.

    It includes the topic on purpose. Provider delivery ids are unique per
    *install*, not globally, so two tenants each with their own Slack app can
    emit the same ``event_id`` — and a key without the topic would silently drop
    the second tenant's copy as a duplicate. Cross-tenant event loss, with no
    error anywhere.
    """
    type: str
    """What happened, namespaced: ``slack.message``, ``jira:issue_created``."""
    payload: Mapping[str, Any] = field(default_factory=dict)
    key: str = ""
    """The ordering group. Records sharing a key are read back in append order;
    records with different keys carry no ordering promise.

    Per record rather than per batch because that is what a partitioned backend
    actually does — a Kafka producer keys each record, and a batch with mixed
    keys spans partitions. Promising batch-level ordering would be a promise
    only the single-partition implementations could keep.

    Usually the id of the thing the event is about, so that two edits to one
    Jira issue cannot be processed out of order.
    """
    source: str = ""
    """Which adapter produced it. Diagnostic; not part of identity."""
    occurred_at: datetime | None = None
    """When it happened *at the provider*, when the provider says. Distinct from
    when it was appended, which is :attr:`StoredEvent.appended_at` — a redelivery
    hours later has the original ``occurred_at`` and a fresh ``appended_at``."""
    chain_depth: int = 0
    """How many workflow-to-workflow hops produced this event.

    A workflow can publish an event that triggers a workflow that publishes…
    Capped by the dispatcher, because the first cycle anyone writes would
    otherwise take the system down — and unifying ``ctx.publish`` with external
    events makes that cycle easy to write by accident.
    """


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One event as a subscriber reads it back."""

    position: Position
    record: EventRecord
    appended_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def event_id(self) -> str:
        return self.record.event_id

    @property
    def type(self) -> str:
        return self.record.type

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.record.payload


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """How far one subscriber has read one topic."""

    subscriber: str
    topic: str
    position: Position
    updated_at: datetime

    def is_stale(self, *, now: datetime, ttl_seconds: float) -> bool:
        """Whether this checkpoint has stopped moving.

        A subscriber that has not committed within its TTL is *quarantined*, not
        retired: retention is allowed to pass it, and resuming it later raises
        rather than silently skipping whatever was discarded. See
        :class:`RetentionPolicy`.
        """
        return (now - self.updated_at).total_seconds() > ttl_seconds


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """What a log may discard.

    A *policy* rather than a position, because only the adapter knows what it
    can enforce — a Kafka adapter honours the broker's own retention setting and
    should say so rather than pretend to control it.

    Two clocks, and both matter:

    ``max_age_seconds``
        The floor. Nothing younger is discarded.
    ``subscriber_ttl_seconds``
        Which checkpoints still count. Retention never passes the slowest
        *active* subscriber, because discarding records a live reader has not
        seen is silent, permanent loss that surfaces much later as "that
        workflow just never ran". But a subscriber abandoned months ago must not
        pin the log forever either — past this TTL it is quarantined and
        retention proceeds.
    """

    max_age_seconds: float = 7 * 24 * 3600.0
    subscriber_ttl_seconds: float = 7 * 24 * 3600.0
    max_records: int | None = None
    """A hard ceiling per topic, for a backend that can enforce one cheaply.
    ``None`` means unbounded, which is the honest default: a size cap that
    silently drops unread records is the failure this whole policy exists to
    prevent."""
