"""Jira as an event source — shape A, push-data, with two sharp edges.

**Jira sends no stable delivery id.** There is no ``X-Jira-Delivery`` and no id
in the body that survives a redelivery, while Atlassian's own documentation
states webhooks may be delivered more than once and are not guaranteed in order.
So the ingress falls back to hashing the body, which deduplicates an identical
redelivery and — honestly — cannot tell two genuinely identical events apart.
That is why ``key`` is the issue key: ordering per issue is the promise that
matters, and it is one the log can actually keep.

**Verification is opt-in at the Jira end.** A webhook registered through the
REST API with a ``secret`` is signed ``X-Hub-Signature: sha256=…`` over the raw
body; one registered by an administrator through the UI is not signed at all.
Both exist in the wild, so this refuses an unsigned delivery **by default** and
makes accepting one an explicit argument — the reverse default would mean an
endpoint anybody on the internet can post issue events to, silently.

The event type is in the body (``webhookEvent``), colon-namespaced
(``jira:issue_created``), and several event types are *not* prefixed at all
(``comment_created``). Both are normalised to ``jira.<something>`` so that a
subscriber writes one form and topics stay uniform.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
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

__all__ = ["JiraSource"]


class JiraSource:
    """Accepts Jira Cloud (and Data Center) webhook deliveries.

    ``JIRA_WEBHOOK_SECRET`` by default, or pass ``secret=``. This is the value
    given as ``secret`` when the webhook is registered through
    ``/rest/api/3/webhook``; a webhook created in the admin UI has none, and for
    that case ``JiraSource(require_signature=False)`` says so out loud.
    """

    id = "jira"

    def __init__(
        self,
        secret: str | None = None,
        *,
        require_signature: bool = True,
    ) -> None:
        self._secret = secret or os.getenv("JIRA_WEBHOOK_SECRET", "")
        self._require = require_signature

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        signature = headers.get("x-hub-signature", "")

        if not self._secret:
            if self._require:
                raise VerificationFailed(
                    "no Jira webhook secret configured, so this delivery "
                    "cannot be verified. Set JIRA_WEBHOOK_SECRET to the "
                    "`secret` the webhook was registered with, or construct "
                    "JiraSource(require_signature=False) if the webhook was "
                    "created in the admin UI — which cannot sign — and the "
                    "endpoint is protected some other way."
                )
            logger.warning(
                "accepting an unverified Jira delivery: require_signature=False"
            )
            return

        if not signature:
            raise VerificationFailed(
                "Jira delivery carried no X-Hub-Signature. A webhook "
                "registered without a `secret` never sends one, so a secret "
                "configured here and not there fails every delivery."
            )

        expected = self.signature_for(body)
        if not hmac.compare_digest(expected, signature):
            raise VerificationFailed(
                "Jira signature mismatch. It covers the raw request body, so "
                "any re-encoding between Jira and here breaks it for every "
                "delivery rather than for some."
            )

    def signature_for(self, body: bytes) -> str:
        """The ``sha256=…`` value Jira should have sent. Exposed for tests."""
        digest = hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def challenge(
        self, headers: Mapping[str, str], body: bytes
    ) -> Challenge | None:
        """None. Jira has no registration handshake — it posts events or nothing."""
        return None

    def delivery_id(self, headers: Mapping[str, str], payload: Any) -> str | None:
        """``None``, deliberately, so the ingress hashes the body.

        Jira publishes no delivery id. The tempting substitutes are all worse
        than the hash: ``timestamp`` is the *event* time and is identical across
        a redelivery *and* across two events in the same millisecond, and
        ``issue.id`` is the same for every update to one issue — using it would
        collapse an issue's entire history into one event.
        """
        return None

    async def expand(
        self, payload: Any, ctx: SourceContext
    ) -> Sequence[InboundEvent]:
        if not isinstance(payload, dict):
            raise MalformedDelivery("Jira delivery was not a JSON object")

        raw_type = payload.get("webhookEvent")
        if not isinstance(raw_type, str) or not raw_type:
            # Jira's connect-app lifecycle callbacks (installed/uninstalled)
            # come through the same endpoint shape with no webhookEvent.
            logger.debug("jira: delivery with no webhookEvent, ignored")
            return []

        return [
            InboundEvent(
                type=normalise_event_type(raw_type),
                payload=payload,
                # Per issue: two updates to one issue must not be reordered,
                # and Atlassian states ordering is not guaranteed on the wire,
                # so this is where it gets restored.
                key=issue_key(payload),
                occurred_at=_jira_time(payload.get("timestamp")),
            )
        ]


def normalise_event_type(raw: str) -> str:
    """``jira:issue_created`` and ``comment_created`` both become ``jira.…``.

    Jira prefixes issue and project events with ``jira:`` and leaves comment,
    worklog and sprint events bare. Left alone, a subscriber would have to know
    which family an event belongs to just to name its topic — and would get it
    wrong once, silently, because a topic nobody publishes to simply stays
    empty.
    """
    body = raw.split(":", 1)[1] if raw.startswith("jira:") else raw
    return f"jira.{body}"


def issue_key(payload: Mapping[str, Any]) -> str:
    """The human issue key (``ENG-4``), falling back to the numeric id.

    The key rather than the id because it is what appears in every filter a
    person writes, and because it is what the ordering promise is *about*. It
    can be reassigned when an issue moves project, which is why the id is the
    fallback rather than the other way round: a moved issue changing ordering
    group is better than an unkeyed one having no ordering at all.
    """
    issue = payload.get("issue")
    if isinstance(issue, Mapping):
        for field_name in ("key", "id"):
            value = issue.get(field_name)
            if value:
                return str(value)
    comment_parent = payload.get("comment")
    if isinstance(comment_parent, Mapping):
        # A comment event carries the issue under `issue`; when it does not,
        # the comment's own id at least keeps one comment's edits together.
        value = comment_parent.get("id")
        if value:
            return f"comment:{value}"
    return ""


def _jira_time(value: Any) -> datetime | None:
    """Jira's ``timestamp`` is epoch **milliseconds**, not seconds.

    Read as seconds it lands in the year 55000 — far enough out that it looks
    obviously wrong, which is the only lucky thing about it.
    """
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
