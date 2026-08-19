"""Slack as an event source — shape A, push-data.

Slack's Events API posts the whole event, so nothing has to be fetched back.
What it *does* require is three things that are all easy to get subtly wrong and
all fail in ways that look like something else:

**The signature is over the raw body.** ``v0:{timestamp}:{body}``, HMAC-SHA256
with the signing secret, compared against ``X-Slack-Signature``. Re-serialising
the parsed JSON produces a different byte string — different key order, different
spacing — and the comparison then fails for every legitimate delivery, which
reads as a wrong secret.

**The timestamp is part of the check.** Slack's own guidance is to reject
anything more than five minutes old, because a signature stays valid forever
otherwise and a captured delivery can be replayed at will.

**The handshake has a three-second budget.** When an endpoint is first saved
Slack posts ``{"type": "url_verification", "challenge": …}`` and will not enable
the endpoint until that value comes back. It is signed like anything else, so it
is verified first — answering it before verifying would let anyone who guesses
the URL complete somebody else's registration.

Retries are the fourth: Slack redelivers three times on any non-2xx or any
response slower than three seconds, with the same ``X-Slack-Retry-Num``. The
event id in the body (``Ev…``) is stable across those, which is why it is the
delivery id and why a redelivery costs nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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

__all__ = ["SlackSource"]

#: Slack's own recommendation. Beyond it a captured delivery is a replay, and a
#: signature alone cannot tell the difference — it stays valid forever.
DEFAULT_MAX_AGE_SECONDS = 60 * 5


class SlackSource:
    """Accepts Slack Events API deliveries.

    ``SLACK_SIGNING_SECRET`` by default, or pass ``signing_secret=``. The secret
    is *not* the bot token: it is issued per app under Basic Information, and
    using the token here fails every signature with no indication why.
    """

    id = "slack"

    def __init__(
        self,
        signing_secret: str | None = None,
        *,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        require_signature: bool = True,
    ) -> None:
        # Annotated: `a or b` with an optional `a` infers `str | None` even though
        # the getenv default makes None unreachable. Stating the invariant beats
        # narrowing it again at every use.
        self._secret: str = signing_secret or os.getenv("SLACK_SIGNING_SECRET") or ""
        self._max_age = max_age_seconds
        self._require = require_signature

    # -- verification --------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        signature = headers.get("x-slack-signature", "")
        timestamp = headers.get("x-slack-request-timestamp", "")

        if not self._secret:
            if self._require:
                raise VerificationFailed(
                    "no Slack signing secret configured, so this delivery "
                    "cannot be verified. Set SLACK_SIGNING_SECRET (App "
                    "credentials → Signing Secret, not the bot token), or "
                    "construct SlackSource(require_signature=False) if the "
                    "endpoint is already authenticated by a gateway in front "
                    "of it."
                )
            # Deliberate and visible: someone said the gateway does this.
            logger.warning(
                "accepting an unverified Slack delivery: "
                "require_signature=False"
            )
            return

        if not signature or not timestamp:
            raise VerificationFailed(
                "Slack delivery is missing X-Slack-Signature or "
                "X-Slack-Request-Timestamp. Both are always sent; a proxy that "
                "strips unknown headers is the usual cause."
            )

        self._check_age(timestamp)

        expected = self.signature_for(timestamp, body)
        # Constant-time: a byte-by-byte comparison leaks the prefix length
        # through timing, and a signature is exactly the kind of secret that
        # can be recovered one byte at a time.
        if not hmac.compare_digest(expected, signature):
            raise VerificationFailed(
                "Slack signature mismatch. The signature covers the raw "
                "request body, so a gateway that re-encodes JSON, or a handler "
                "that passes a re-serialised dict, breaks it for every "
                "delivery — check the body reaches this unmodified before "
                "suspecting the secret."
            )

    def _check_age(self, timestamp: str) -> None:
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise VerificationFailed(
                f"X-Slack-Request-Timestamp is not an integer: {timestamp!r}"
            ) from exc
        age = abs(time.time() - sent_at)
        if age > self._max_age:
            raise VerificationFailed(
                f"Slack delivery is {int(age)}s old, over the {self._max_age}s "
                "limit, so it is treated as a replay. A signature does not "
                "expire on its own; this is what bounds it. Persistent "
                "failures here usually mean the host clock has drifted."
            )

    def signature_for(self, timestamp: str, body: bytes) -> str:
        """The ``v0=…`` signature Slack should have sent. Exposed for tests."""
        base = b"v0:" + timestamp.encode() + b":" + body
        digest = hmac.new(self._secret.encode(), base, hashlib.sha256).hexdigest()
        return f"v0={digest}"

    # -- handshake -----------------------------------------------------------

    def challenge(
        self, headers: Mapping[str, str], body: bytes
    ) -> Challenge | None:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("type") == "url_verification":
            value = payload.get("challenge", "")
            if not isinstance(value, str):
                raise MalformedDelivery(
                    "Slack url_verification carried a non-string challenge"
                )
            return Challenge(body=value)
        return None

    # -- identity and expansion ---------------------------------------------

    def delivery_id(self, headers: Mapping[str, str], payload: Any) -> str | None:
        """``event_id`` from the body — ``Ev…``, stable across all three retries.

        Not ``X-Slack-Retry-Num``, which is what distinguishes the attempts and
        would therefore defeat the dedupe it looks like it supports.
        """
        if isinstance(payload, dict):
            candidate = payload.get("event_id")
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    async def expand(
        self, payload: Any, ctx: SourceContext
    ) -> Sequence[InboundEvent]:
        if not isinstance(payload, dict):
            raise MalformedDelivery("Slack delivery was not a JSON object")

        outer_type = payload.get("type")
        if outer_type == "event_callback":
            return self._from_event_callback(payload)
        if outer_type in {"block_actions", "view_submission", "shortcut"}:
            # Interactive components arrive form-encoded with a JSON `payload`
            # field, which the ingress has already unwrapped.
            return [
                InboundEvent(
                    type=f"slack.{outer_type}",
                    payload=payload,
                    key=str(_dig(payload, "user", "id") or ""),
                )
            ]
        if "command" in payload:
            return [
                InboundEvent(
                    type="slack.slash_command",
                    payload=payload,
                    key=str(payload.get("channel_id") or ""),
                )
            ]
        # A type nobody models here. Dropped, not errored: the alternative
        # teaches Slack to disable the endpoint after enough 4xxs.
        logger.debug("slack: ignoring delivery of type %r", outer_type)
        return []

    def _from_event_callback(self, payload: dict[str, Any]) -> list[InboundEvent]:
        event = payload.get("event")
        if not isinstance(event, dict):
            raise MalformedDelivery(
                "Slack event_callback had no `event` object"
            )
        subtype = event.get("subtype")
        event_type = str(event.get("type") or "unknown")

        # Bot messages carry no `user`, and a triage workflow that replies to
        # them talks to itself. Kept — dropping them here would hide the
        # decision — but flagged, so a filter can say `bot: false` and mean it.
        normalised = dict(event)
        normalised["bot"] = bool(event.get("bot_id")) or subtype == "bot_message"
        normalised["team_id"] = payload.get("team_id")
        normalised["api_app_id"] = payload.get("api_app_id")

        return [
            InboundEvent(
                type=f"slack.{event_type}",
                payload=normalised,
                # Per channel: two edits to one conversation must not be
                # processed out of order, and nothing wider than that is a
                # promise a partitioned backend could keep.
                key=str(event.get("channel") or event.get("channel_id") or ""),
                occurred_at=_slack_time(event.get("event_ts") or event.get("ts")),
            )
        ]


def _dig(payload: Mapping[str, Any], *path: str) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _slack_time(value: Any) -> datetime | None:
    """Slack timestamps are ``"1701234567.000200"`` — seconds with a counter.

    The fractional part is a per-channel disambiguator, not microseconds, but it
    is monotonic within a channel, so reading it as a float orders correctly and
    is off by less than a second in absolute terms.
    """
    if not isinstance(value, str | int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
