"""The event backbone — a durable log, and where each subscriber has read to.

Design notes and the reasoning behind every decision here are in
``docs/design/event-backbone.md``. The short version:

**A log, not a bus.** A bus makes *delivery* the durable act, so a subscriber
that was down missed those events permanently and a subscriber added tomorrow
sees nothing from today. A log makes *the record* the durable act and delivery a
resumable read — which is the only shape that survives being killed and started
again, and the only one where many workflows can independently consume the same
event.

**The package ships no broker.** These protocols, one reference implementation
built on capabilities every LOOM store already has, and a conformance kit
(:mod:`loom.testing.conformance`) so a host can prove its own Kafka, Redis
Streams, or Postgres adapter correct. Shipping adapters means owning their
integration tests forever.

    from loom.events import EventRecord, StoreBackedCheckpoints, StoreBackedEventLog

    log = StoreBackedEventLog(store)
    checkpoints = StoreBackedCheckpoints(store)

    await log.append("app.slack.message", [
        EventRecord(event_id="app.slack.message/slack:Ev123", type="slack.message",
                    payload={"text": "deploy finished"}, key="C024BE91L"),
    ])

    after = await checkpoints.load("triage", "app.slack.message")
    batch = await log.read("app.slack.message", after=after, limit=100)
    ...                                   # do the durable, idempotent work
    await checkpoints.commit("triage", "app.slack.message", batch[-1].position)

That last ordering is the whole correctness argument, and it is the same rule at
every hop: **do the durable idempotent thing first, advance the marker last.**
Committing early loses events permanently; committing late costs rework that the
dispatch idempotency key already absorbs.
"""

from __future__ import annotations

from loom.events.dispatcher import (
    CHAIN_DEPTH_CAP,
    DispatchReport,
    EventDispatcher,
)
from loom.events.ingress import Delivery, IngressResult, WebhookIngress, topic_for
from loom.events.log import (
    Checkpoints,
    EventLog,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    positions_of,
)
from loom.events.models import (
    Checkpoint,
    EventRecord,
    Position,
    RetentionPolicy,
    StoredEvent,
)
from loom.events.reconcile import (
    CursorExpired,
    Expansion,
    PointerReconciler,
    Reconciler,
)
from loom.events.source_registry import (
    BUILTIN_SOURCES,
    EventSourceRegistry,
    discover_source_entry_points,
    get_source_catalog,
    register_event_source,
    unregister_event_source,
)
from loom.events.sources import (
    Challenge,
    EventSource,
    InboundEvent,
    MalformedDelivery,
    SourceContext,
    SourceState,
    VerificationFailed,
)
from loom.events.subscription import StartAt, Subscription
from loom.events.watch import (
    Heartbeat,
    Watch,
    WatchRegistration,
    WatchRenewer,
    WatchStatus,
    lifetime_hint,
)

__all__ = [
    "BUILTIN_SOURCES",
    "CHAIN_DEPTH_CAP",
    "Challenge",
    "Checkpoint",
    "Checkpoints",
    "CursorExpired",
    "Delivery",
    "DispatchReport",
    "EventDispatcher",
    "EventLog",
    "EventRecord",
    "EventSource",
    "EventSourceRegistry",
    "Expansion",
    "Heartbeat",
    "InboundEvent",
    "IngressResult",
    "MalformedDelivery",
    "PointerReconciler",
    "Position",
    "Reconciler",
    "RetentionPolicy",
    "SourceContext",
    "SourceState",
    "StartAt",
    "StoreBackedCheckpoints",
    "StoreBackedEventLog",
    "StoredEvent",
    "Subscription",
    "VerificationFailed",
    "Watch",
    "WatchRegistration",
    "WatchRenewer",
    "WatchStatus",
    "WebhookIngress",
    "discover_source_entry_points",
    "get_source_catalog",
    "lifetime_hint",
    "positions_of",
    "register_event_source",
    "topic_for",
    "unregister_event_source",
]
