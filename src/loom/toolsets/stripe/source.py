"""Stripe as an event source — shape A, push-data.

Stripe posts the whole event, so nothing has to be fetched back. What it
requires is a signature check with three parts that all fail in ways that look
like something else.

**The signature is over the raw body.** ``{timestamp}.{body}``, HMAC-SHA256
with the endpoint's signing secret. Re-serialising parsed JSON produces
different bytes — different key order, different spacing — and the comparison
then fails for every legitimate delivery, which reads as a wrong secret.

**The header carries several signatures, not one.** ``Stripe-Signature`` is a
comma-separated list: ``t=1699…,v1=abc…,v1=def…``. More than one ``v1`` appears
while a secret is being rotated, and *any* of them matching is a valid
delivery — checking only the first breaks every delivery during a rollover,
which is exactly when nobody wants to be debugging a webhook.

**The timestamp is part of the check.** A signature does not expire on its own,
so a captured delivery replays forever without a tolerance window. Stripe's own
guidance is five minutes.

There is no handshake: Stripe verifies an endpoint by sending a real event, so
:meth:`challenge` returns ``None``. The ``evt_…`` id in the body is stable
across every retry — Stripe retries for up to three days with exponential
backoff — which is why it is the delivery id and why a redelivery costs
nothing.

**A verified delivery is still only what arrived.** For anything that moves
money, read the event back with ``stripe_get_event`` and act on Stripe's copy.
The signature proves the bytes came from Stripe; re-reading proves the *state*
has not changed since, which matters when a redelivery lands three days late.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from loom.events.sources import (
    Challenge,
    InboundEvent,
    MalformedDelivery,
    SourceContext,
    VerificationFailed,
)

logger = logging.getLogger("workflow.events")

__all__ = ["StripeSource"]

#: Stripe's own recommended tolerance. Beyond it a captured delivery is a
#: replay, and a signature alone cannot tell the difference.
DEFAULT_MAX_AGE_SECONDS = 60 * 5

#: The signature scheme Stripe currently sends. Named rather than assumed, so a
#: future ``v2`` is a visible change here rather than a silent acceptance.
SCHEME = "v1"


class StripeSource:
    """Accepts Stripe webhook deliveries.

    ``STRIPE_WEBHOOK_SECRET`` by default, or pass ``signing_secret=``. The
    secret is ``whsec_…``, issued **per endpoint** in the dashboard — it is not
    the API key, and using the API key here fails every signature with no
    indication why.
    """

    id = "stripe"

    def __init__(
        self,
        signing_secret: str | None = None,
        *,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        require_signature: bool = True,
    ) -> None:
        self._secret: str = signing_secret or os.getenv("STRIPE_WEBHOOK_SECRET") or ""
        self._max_age = max_age_seconds
        self._require = require_signature

    # -- verification --------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        header = headers.get("stripe-signature", "")

        if not self._secret:
            if self._require:
                raise VerificationFailed(
                    "no Stripe webhook secret configured, so this delivery "
                    "cannot be verified. Set STRIPE_WEBHOOK_SECRET (the "
                    "whsec_… value shown against the endpoint in the "
                    "dashboard, not the API key), or construct "
                    "StripeSource(require_signature=False) if the endpoint is "
                    "already authenticated by a gateway in front of it."
                )
            logger.warning(
                "accepting an unverified Stripe delivery: require_signature=False"
            )
            return

        if not header:
            raise VerificationFailed(
                "Stripe delivery is missing the Stripe-Signature header. It is "
                "always sent; a proxy that strips unknown headers is the usual "
                "cause."
            )

        timestamp, signatures = parse_signature_header(header)
        if not timestamp or not signatures:
            raise VerificationFailed(
                f"Stripe-Signature carried no {SCHEME} signature or no "
                f"timestamp: {header!r}"
            )

        self._check_age(timestamp)

        expected = self.signature_for(timestamp, body)
        # Any listed signature matching is a valid delivery. During a secret
        # rotation Stripe sends one per active secret, and checking only the
        # first fails every delivery for the length of the rollover.
        if not any(hmac.compare_digest(expected, sent) for sent in signatures):
            raise VerificationFailed(
                "Stripe signature mismatch. The signature covers the raw "
                "request body, so a gateway that re-encodes JSON, or a handler "
                "that passes a re-serialised dict, breaks it for every "
                "delivery — check the body reaches this unmodified before "
                "suspecting the secret. Note the secret is per endpoint: two "
                "endpoints on one account have different ones."
            )

    def _check_age(self, timestamp: str) -> None:
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise VerificationFailed(
                f"Stripe-Signature timestamp is not an integer: {timestamp!r}"
            ) from exc
        age = abs(time.time() - sent_at)
        if age > self._max_age:
            raise VerificationFailed(
                f"Stripe delivery is {int(age)}s old, over the {self._max_age}s "
                "tolerance, so it is treated as a replay. A signature does not "
                "expire on its own; this is what bounds it. Persistent "
                "failures here usually mean the host clock has drifted — and "
                "note Stripe retries for up to three days, so a genuinely old "
                "redelivery is refused by design."
            )

    def signature_for(self, timestamp: str, body: bytes) -> str:
        """The signature Stripe should have sent. Exposed for tests."""
        base = timestamp.encode() + b"." + body
        return hmac.new(self._secret.encode(), base, hashlib.sha256).hexdigest()

    # -- handshake -----------------------------------------------------------

    def challenge(self, headers: Mapping[str, str], body: bytes) -> Challenge | None:
        """None — Stripe has no handshake.

        An endpoint is verified by sending it a real event, so there is nothing
        to echo back. Returning ``None`` is the whole implementation, which is
        the point of ``EventSource`` being four small methods rather than one
        ``handle()``.
        """
        return None

    # -- identity and expansion ---------------------------------------------

    def delivery_id(self, headers: Mapping[str, str], payload: Any) -> str | None:
        """``evt_…`` from the body — stable across every retry.

        Stripe retries a failed delivery for up to three days, and the id does
        not change, so this is what makes a redelivery free.
        """
        if isinstance(payload, dict):
            candidate = payload.get("id")
            if isinstance(candidate, str) and candidate.startswith("evt_"):
                return candidate
        return None

    async def expand(self, payload: Any, ctx: SourceContext) -> Sequence[InboundEvent]:
        if not isinstance(payload, dict):
            raise MalformedDelivery("Stripe delivery was not a JSON object")

        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise MalformedDelivery("Stripe delivery had no `type`")

        data = payload.get("data")
        obj = (data or {}).get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            raise MalformedDelivery(
                f"Stripe {event_type} delivery had no data.object"
            )

        normalised = dict(obj)
        normalised["event_id"] = payload.get("id")
        normalised["event_type"] = event_type
        # Carried deliberately: a test-mode delivery reaching a production
        # workflow is the failure this makes filterable, and it is otherwise
        # invisible in the object itself.
        normalised["livemode"] = bool(payload.get("livemode"))

        return [
            InboundEvent(
                type=f"stripe.{event_type}",
                payload=normalised,
                # Per object: two events about one payment must not be
                # processed out of order, and nothing wider is a promise a
                # partitioned backend could keep.
                key=str(obj.get("id") or ""),
                occurred_at=_moment(payload.get("created")),
            )
        ]


def parse_signature_header(header: str) -> tuple[str, list[str]]:
    """Split ``t=…,v1=…,v1=…`` into its timestamp and every ``v1`` signature.

    Returns every match rather than the first, because Stripe sends one per
    active secret while a rotation is in progress.
    """
    timestamp = ""
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            timestamp = value
        elif name == SCHEME and value:
            signatures.append(value)
    return timestamp, signatures


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
