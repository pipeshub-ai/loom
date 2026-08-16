"""Where events come from — the provider-facing half of the backbone.

An :class:`EventSource` answers exactly one question: *is this really from the
provider, and what happened?* It does not decide who cares — that is the
dispatcher's, and a class holding both a Slack signature and a workflow's filter
would leave neither testable without the other.

Four small methods rather than one ``handle()``, so that a provider with no
handshake writes ``return None`` instead of re-implementing a dispatch loop:

``verify``
    Reject anything that did not come from the provider. Raises; a return value
    would be ignorable, and a check whose failure is ignorable is not a check.
``challenge``
    Answer a registration handshake, if this request is one.
``delivery_id``
    The provider's own id for this delivery. It becomes part of the event id,
    which is what makes a redelivery free.
``expand``
    Turn one delivery into the events it represents — one, several, or in the
    pointer case (Gmail, Graph delta) a single event carrying a *position* that
    a reconciler will expand later.

Adding a provider costs a verifier and a normaliser. If it ever costs more than
that, the seam is in the wrong place.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loom.core.exceptions import TriggerError

if TYPE_CHECKING:
    from loom.runtime.engine import Runtime

__all__ = [
    "Challenge",
    "EventSource",
    "InboundEvent",
    "MalformedDelivery",
    "SourceContext",
    "SourceState",
    "VerificationFailed",
]


class VerificationFailed(TriggerError):  # noqa: N818 - names the outcome
    """A delivery did not prove it came from the provider.

    Its own type because the response differs in kind from every other ingress
    failure: this one is answered 401 and *not* retried, and it is the one that
    should page somebody if it starts happening in volume.
    """


class MalformedDelivery(TriggerError):  # noqa: N818 - names the delivery
    """A delivery was authentic but could not be read.

    Distinct from :class:`VerificationFailed` on purpose. A signature failure
    means somebody is lying about who they are; this means the provider changed
    a payload shape, or a proxy mangled one. Both are 4xx and neither is
    retryable, but conflating them turns "our parser is out of date" into "we
    are under attack" in whatever dashboard counts them.
    """


@dataclass(frozen=True, slots=True)
class Challenge:
    """A handshake response the provider expects, verbatim.

    Slack posts a ``url_verification`` payload when an endpoint is first saved
    and requires the ``challenge`` value echoed back within three seconds; the
    endpoint is not enabled until it is. Returning this instead of an
    :class:`InboundEvent` keeps a handshake from being recorded as a business
    event that some workflow then tries to triage.
    """

    body: str
    content_type: str = "text/plain"
    status: int = 200


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """One thing that happened, as the source understood it.

    *Not* yet an :class:`~loom.events.models.EventRecord`: the ingress supplies
    identity, topic and chain depth, because those are properties of the log
    rather than of the provider. A source that had to construct an event id
    would have to know the topic naming scheme, and every source would then
    encode it separately.
    """

    type: str
    """``{source}.{event_type}`` — ``slack.message``, ``jira.issue_created``.
    The topic is derived from it, so this is what a subscriber declares."""
    payload: dict[str, Any] = field(default_factory=dict)
    key: str = ""
    """Ordering key. Records sharing one are read back in append order; records
    with different keys carry no ordering promise, which is the only promise a
    partitioned backend can keep. A channel id, a mailbox, an issue key."""
    occurred_at: datetime | None = None
    """When the provider says it happened, when it says. Distinct from when we
    recorded it — a redelivery three days later must not look like a fresh
    event to a workflow that reads timestamps."""
    dedupe_suffix: str = ""
    """Distinguishes several events expanded from one delivery. Left empty for
    the common one-delivery-one-event case; the ingress supplies an index when
    a source returns several and does not say."""


class SourceState:
    """A durable, per-source scratchpad — cursors, watch expiries, tokens.

    Deliberately not the journal and not a checkpoint. A checkpoint says where a
    *subscriber* has read to in our log; this holds what a *source* needs to
    keep about the provider: Gmail's ``historyId``, a Graph subscription's
    expiry, a Salesforce replay id. Those survive restarts and belong to the
    source, not to any run.

    Rides ``CacheStore``, so it needs no new backend code — the same move
    ``ctx.state`` and the event log already made.
    """

    __slots__ = ("_cache", "_source_id")

    def __init__(self, cache: Any, source_id: str) -> None:
        self._cache = cache
        self._source_id = source_id

    def _key(self, name: str) -> str:
        return f"eventsource:{self._source_id}:{name}"

    async def get(self, name: str, default: Any = None) -> Any:
        raw = await self._cache.get(self._key(name))
        if raw is None:
            return default
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return raw

    async def set(self, name: str, value: Any) -> None:
        # ttl 0 is "no expiry" — a cursor that quietly evicted would reset the
        # source to now and lose whatever happened while it was gone, which is
        # the silent class this whole design exists to make loud.
        await self._cache.set(self._key(name), json.dumps(value), 0)

    async def delete(self, name: str) -> None:
        await self._cache.delete(self._key(name))


@dataclass
class SourceContext:
    """What a source is handed when it expands a delivery.

    Everything here is a *port*, not a concrete: a source that reaches past this
    into a Runtime internal is a source that cannot be tested without one, and
    that is exactly the seam ``tests/test_host_integration.py`` greps for.
    """

    source_id: str
    state: SourceState
    """Durable per-source storage. Cursors live here."""
    runtime: Runtime | None = None
    """For a source that must call the provider back — shape B. ``None`` in a
    unit test, which is why every shipped shape-A source ignores it."""
    headers: Mapping[str, str] = field(default_factory=dict)
    """The delivery's headers, lower-cased. Several providers put the event
    type in one (``x-shopify-topic``, ``x-github-event``) rather than in the
    body, so ``expand`` needs them and the signature would otherwise force
    every such source to stash them on itself between two calls."""

    @property
    def credentials(self) -> Any:
        return getattr(self.runtime, "credentials", None)


@runtime_checkable
class EventSource(Protocol):
    """A provider LOOM can accept deliveries from.

    Implemented by anyone, registered by name, discovered through the
    ``loom_event_source`` entry point — nothing in LOOM names a third party's
    provider. See :mod:`loom.events.source_registry`.
    """

    id: str
    """Stable, and it appears in the URL (``/hooks/{id}``) and in every event
    id this source produces. Changing it orphans checkpoints."""

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        """Raise :class:`VerificationFailed` if this is not from the provider.

        Given the **raw body**, because every scheme in use signs bytes and a
        re-serialised dict is a different byte string — a JSON round-trip that
        reorders keys or changes spacing breaks the signature with no way to
        tell that from an attack.

        A source with no verification available says so in a docstring rather
        than silently passing; ``verify`` that always returns is a decision, and
        it should be visible as one.
        """
        ...

    def challenge(
        self, headers: Mapping[str, str], body: bytes
    ) -> Challenge | None:
        """The handshake response, if this delivery is a handshake."""
        ...

    def delivery_id(
        self, headers: Mapping[str, str], payload: Any
    ) -> str | None:
        """The provider's own id for this delivery, or ``None`` if it has none.

        ``None`` is honest and is handled: the ingress hashes the body instead,
        so an identical redelivery still deduplicates. What it cannot do is
        distinguish two genuinely identical events, which is why a provider that
        offers an id should always have it read.
        """
        ...

    async def expand(
        self, payload: Any, ctx: SourceContext
    ) -> Sequence[InboundEvent]:
        """The events this delivery represents.

        Usually one. Several for a provider that batches (Stripe does not, Slack
        does not, Graph does). Exactly one *pointer* event for shape B, where
        the payload is a position rather than data and a reconciler expands it
        later — downstream subscribers never learn that Gmail is different.

        Returning ``[]`` drops the delivery: legitimate for a sub-type nobody
        models, and it is recorded as accepted, because the provider is owed a
        2xx either way.
        """
        ...
