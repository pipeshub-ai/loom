"""Accepting a delivery: verify, expand, append.

The producer side of the log. It is short on purpose — everything provider-
specific is behind :class:`~loom.events.sources.EventSource`, and everything
subscriber-specific is behind the dispatcher, so what is left is the part that
is the same for every provider:

1. resolve the source, and refuse an unknown one *before* reading the body;
2. **verify**, then answer a handshake — never the other way round;
3. derive the event id from the provider's delivery id;
4. append.

**The append is the accept.** There is no durable-versus-direct sink choice
here, because the log removed the question: once the records are appended the
delivery is safe, and until they are it is not. That is why this returns only
after the append and why a provider gets a 5xx if it fails — a 2xx we cannot
back up is a lost event that nobody will resend.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.events.models import EventRecord
from loom.events.source_registry import EventSourceRegistry
from loom.events.sources import (
    Challenge,
    EventSource,
    InboundEvent,
    MalformedDelivery,
    SourceContext,
    SourceState,
)

if TYPE_CHECKING:
    from loom.events.log import EventLog
    from loom.runtime.engine import Runtime

logger = logging.getLogger("workflow.events")

__all__ = ["Delivery", "IngressResult", "WebhookIngress", "topic_for"]

#: Prefix distinguishing what the outside world said from what LOOM said.
#:
#: ``ctx.publish`` writes to the same log, and a workflow that can be triggered
#: by its own output is a loop nobody intended. Keeping the namespaces apart
#: makes that visible in the topic name rather than discoverable at 3am.
APP_PREFIX = "app"


def topic_for(event_type: str) -> str:
    """The topic an inbound ``{source}.{event_type}`` lands on.

    One topic per source-and-type, which is the load-bearing middle of the
    three filter placements: ``app.slack.message`` rather than ``app.slack``
    means a workflow interested only in messages never reads a single reaction.
    Whether that becomes a Kafka topic, a Redis stream or a column value is the
    adapter's business — the core always uses the fine-grained name.
    """
    return f"{APP_PREFIX}.{event_type}"


@dataclass(frozen=True, slots=True)
class Delivery:
    """One inbound HTTP request, as the ingress sees it."""

    source_id: str
    headers: Mapping[str, str]
    body: bytes

    def lowered(self) -> dict[str, str]:
        """Headers with lower-cased names.

        HTTP header names are case-insensitive and every provider's docs
        capitalise them differently from every proxy; a source indexing
        ``headers["X-Slack-Signature"]`` works behind one gateway and 401s
        behind another.
        """
        return {str(k).lower(): v for k, v in self.headers.items()}


@dataclass
class IngressResult:
    """What one delivery did."""

    source_id: str
    accepted: bool = True
    challenge: Challenge | None = None
    """Set when the delivery was a registration handshake. Nothing was
    appended, and that is success."""
    event_ids: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    duplicate: bool = False
    """The provider redelivered something already in the log. Also success —
    the append deduplicated it, which is the point of deriving the event id
    from the delivery id."""
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.event_ids)


class WebhookIngress:
    """Accepts provider deliveries and appends them to the log.

    Transport-free by construction: it takes headers and bytes and returns a
    result. The FastAPI routes in ``loom.server`` are thirty lines over this,
    and a host with its own gateway — Lambda, Cloud Run, a Django view — calls
    :meth:`receive` directly and gets identical behaviour. A verification bug
    fixed in one place is fixed in all of them.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        log: EventLog | None = None,
        sources: EventSourceRegistry | None = None,
    ) -> None:
        from loom.core.exceptions import ConfigurationError

        resolved = log if log is not None else getattr(runtime, "events", None)
        if resolved is None:
            raise ConfigurationError(
                "WebhookIngress needs an EventLog to append to. Pass log=..., "
                "or construct the Runtime with "
                "events=StoreBackedEventLog(store)."
            )
        self._runtime = runtime
        self._log = resolved
        self._sources = sources or getattr(runtime, "sources", None) or (
            EventSourceRegistry(parent=_global_sources())
        )

    @property
    def sources(self) -> EventSourceRegistry:
        return self._sources

    async def receive(
        self,
        source_id: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> IngressResult:
        """Take one delivery. Raises only what the caller must turn into a 4xx.

        :raises ConfigurationError: no such source — 404, and never retried.
        :raises VerificationFailed: not from the provider — 401.
        :raises MalformedDelivery: authentic but unreadable — 400.

        Anything else escaping is a genuine server failure and must reach the
        provider as a 5xx, because that is what makes it resend.
        """
        delivery = Delivery(source_id, headers, body)
        source = self._sources.require(source_id)
        lowered = delivery.lowered()

        # Verify first, always. A handshake answered before verification is an
        # oracle: anyone who can guess the URL gets a signed-looking reply and,
        # for Slack, can complete somebody else's endpoint registration.
        source.verify(lowered, body)

        handshake = source.challenge(lowered, body)
        if handshake is not None:
            return IngressResult(
                source_id, challenge=handshake, reason="registration handshake"
            )

        payload = _decode(body)
        delivery_id = source.delivery_id(lowered, payload) or _body_digest(body)

        ctx = SourceContext(
            source_id=source_id,
            state=SourceState(
                getattr(self._runtime, "cache", None) or self._runtime.store,
                source_id,
            ),
            runtime=self._runtime,
            headers=lowered,
        )
        events = list(await source.expand(payload, ctx))
        if not events:
            # A sub-type nobody models. Accepted rather than errored: the
            # provider is owed a 2xx either way, and a 4xx here teaches it to
            # disable the endpoint.
            return IngressResult(source_id, reason="no events in delivery")

        return await self._append(source_id, delivery_id, events)

    async def _append(
        self, source_id: str, delivery_id: str, events: Sequence[InboundEvent]
    ) -> IngressResult:
        """Group by topic and append, one call per topic.

        Grouped because ``append`` takes the topic's lock once per call, and a
        delivery expanding into forty events across two topics should cost two
        acquisitions rather than forty.
        """
        result = IngressResult(source_id)
        by_topic: dict[str, list[EventRecord]] = {}

        for index, event in enumerate(events):
            topic = topic_for(event.type)
            suffix = event.dedupe_suffix or (str(index) if len(events) > 1 else "")
            event_id = _event_id(topic, source_id, delivery_id, suffix)
            by_topic.setdefault(topic, []).append(
                EventRecord(
                    event_id=event_id,
                    type=event.type,
                    payload=dict(event.payload),
                    key=event.key,
                    source=source_id,
                    occurred_at=event.occurred_at,
                )
            )
            result.event_ids.append(event_id)

        for topic, records in by_topic.items():
            positions = await self._log.append(topic, records)
            result.topics.append(topic)
            logger.debug(
                "ingress: %d event(s) from '%s' -> %s at %s",
                len(records),
                source_id,
                topic,
                list(positions),
            )
        return result


def _event_id(topic: str, source_id: str, delivery_id: str, suffix: str) -> str:
    """``{topic}/{source}:{delivery}`` — and the topic is not decoration.

    Two topics can legitimately carry the same delivery (Slack's
    ``app_mention`` is also a ``message``), and an id without the topic makes
    those two the same event. Whichever landed second would deduplicate away,
    and the workflow subscribing to the other one would simply never run.
    """
    base = f"{topic}/{source_id}:{delivery_id}"
    return f"{base}#{suffix}" if suffix else base


def _body_digest(body: bytes) -> str:
    """A stand-in delivery id for a provider that publishes none.

    Content-addressed, so an identical redelivery still deduplicates. What it
    cannot do is tell two genuinely identical events apart — which is a real
    limitation, and why a source that *has* an id should always read it. Jira
    is the shipped example: it sends no stable delivery id, and its own docs
    say webhooks may be delivered more than once.
    """
    return "sha256:" + hashlib.sha256(body).hexdigest()[:32]


def _decode(body: bytes) -> Any:
    """JSON, or a form body, or the raw text — in that order.

    Form encoding is not hypothetical: Slack's interactive components and slash
    commands post ``application/x-www-form-urlencoded`` with a JSON document in
    a ``payload`` field, and a source that only reads JSON sees an empty dict
    and expands to nothing.
    """
    if not body:
        return {}
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        from urllib.parse import parse_qs

        text = body.decode("utf-8")
        if "=" in text:
            pairs = {k: v[0] for k, v in parse_qs(text).items() if v}
            if "payload" in pairs:
                try:
                    return json.loads(pairs["payload"])
                except ValueError:
                    return pairs
            return pairs
        return {"body": text}
    except UnicodeDecodeError as exc:
        raise MalformedDelivery(
            "delivery body is neither JSON, form-encoded, nor UTF-8 text"
        ) from exc


def _global_sources() -> EventSourceRegistry:
    from loom.events.source_registry import get_source_catalog

    return get_source_catalog()


# Re-exported so a host writing a source imports one module.
__all__ += ["EventSource", "MalformedDelivery"]
