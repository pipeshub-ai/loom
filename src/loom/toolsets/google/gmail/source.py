"""Gmail as an event source — shape B, push-pointer.

Gmail is the reason this architecture has a reconciler at all. Everything else
shipped here posts the event; Gmail posts a **position** and expects you to ask
what changed:

    {"emailAddress": "a@b.com", "historyId": 9876543}

So the ingress appends one *pointer* event, and :class:`GmailReconciler` — an
ordinary subscriber, with an ordinary checkpoint — consumes it, calls
``history.list``, and appends the resulting **data** events back to the log.
Downstream subscribers read ``app.gmail.message`` and never learn that Gmail is
different, which is the whole claim of §2.3 of the design.

Three failure modes are specific to this shape and none of them raise on their
own:

**The watch expires after seven days.** Google stops sending; nothing errors. An
inbox that has gone quiet is indistinguishable from one that is broken.
:class:`~loom.events.watch.WatchRenewer` re-registers daily — a fraction of the
lifetime, so several consecutive failures are survivable.

**The history id expires.** Gmail keeps roughly a week, less on a busy mailbox,
then answers 404. Jumping silently to *now* is the failure where "no email
arrived today" and "we lost a day of email" look identical. Instead a
``gmail.gap`` event is appended, and a workflow can subscribe to *we lost
visibility* and go and do something about it.

**Pub/Sub redelivers, aggressively and out of order.** Its own docs say
at-least-once and that ordering is not guaranteed. The pointer's dedupe uses the
Pub/Sub ``messageId``; the *data* events dedupe on the Gmail message id, which is
what makes an out-of-order pointer harmless — two overlapping history reads
produce the same message ids and the second append is a no-op.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from loom.events.reconcile import CursorExpired, Expansion
from loom.events.sources import (
    Challenge,
    InboundEvent,
    MalformedDelivery,
    SourceContext,
    VerificationFailed,
)
from loom.events.watch import WatchRegistration

logger = logging.getLogger("workflow.events")

__all__ = [
    "GAP_EVENT",
    "PUSH_EVENT",
    "GmailReconciler",
    "GmailSource",
    "GmailWatcher",
    "decode_push",
]

#: What a Gmail push notification becomes. Deliberately not ``gmail.message``:
#: it is a *pointer*, nothing has been fetched, and a workflow subscribing to it
#: expecting an email would get a history id and no sender.
PUSH_EVENT = "gmail.push"

#: Appended when history could not be read. A real event on a real topic, so a
#: workflow can react to lost visibility rather than a human noticing later.
GAP_EVENT = "gmail.gap"


class GmailSource:
    """Accepts Gmail push notifications delivered through Pub/Sub.

    Pub/Sub push posts an OIDC token in ``Authorization: Bearer …`` when the
    subscription is configured with a service account, which is Google's
    recommended shape and the one this verifies. Verification needs
    ``google-auth`` (the ``[google]`` extra); without it, or without an
    *audience* to check against, construction is refused rather than silently
    accepting anything — an endpoint that appends to your event log on any POST
    is not a thing to arrive at by omission.

    ``GmailSource(require_token=False)`` is the explicit escape hatch for a
    deployment where a gateway, VPC ingress, or Cloud Run IAM already
    authenticates the caller.
    """

    id = "gmail"

    def __init__(
        self,
        *,
        audience: str | None = None,
        service_account_email: str | None = None,
        require_token: bool = True,
    ) -> None:
        self._audience = audience or os.getenv("GMAIL_PUSH_AUDIENCE", "")
        self._issuer_email = service_account_email or os.getenv(
            "GMAIL_PUSH_SERVICE_ACCOUNT", ""
        )
        self._require = require_token

    # -- verification --------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        token = _bearer(headers.get("authorization", ""))

        if not self._require:
            logger.warning(
                "accepting an unverified Gmail push: require_token=False"
            )
            return

        if not self._audience:
            raise VerificationFailed(
                "no Gmail push audience configured, so an OIDC token cannot be "
                "checked and this delivery cannot be verified. Set "
                "GMAIL_PUSH_AUDIENCE to the `audience` on the Pub/Sub push "
                "subscription, or construct GmailSource(require_token=False) "
                "if the endpoint is already authenticated in front of LOOM."
            )
        if not token:
            raise VerificationFailed(
                "Gmail push carried no Authorization bearer token. Configure "
                "the Pub/Sub push subscription with a service account so it "
                "signs an OIDC token; an unauthenticated push subscription "
                "posts to this endpoint with nothing to check."
            )

        claims = self._decode(token)

        if claims.get("aud") != self._audience:
            raise VerificationFailed(
                f"Gmail push token audience {claims.get('aud')!r} is not the "
                f"configured {self._audience!r}. A token minted for another "
                "service is a valid Google token and would otherwise pass."
            )
        if self._issuer_email and claims.get("email") != self._issuer_email:
            raise VerificationFailed(
                f"Gmail push token was issued to {claims.get('email')!r}, not "
                f"the expected {self._issuer_email!r}."
            )

    def _decode(self, token: str) -> Mapping[str, Any]:
        """Verify the OIDC token's signature against Google's public keys.

        Signature verification is the whole point, so a missing ``google-auth``
        is a refusal rather than a decode-without-checking. Reading the claims
        unverified would accept a token anybody can construct — which is worse
        than no verification at all, because it looks like verification.
        """
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise VerificationFailed(
                "verifying a Gmail push token needs `google-auth` "
                "(pip install 'loomsdk[google]'). Without it the signature "
                "cannot be checked, and reading the claims anyway would accept "
                "a token anyone can mint."
            ) from exc

        try:
            return dict(
                id_token.verify_oauth2_token(
                    token, google_requests.Request(), self._audience
                )
            )
        except Exception as exc:
            raise VerificationFailed(
                f"Gmail push token failed verification: {exc}"
            ) from exc

    # -- handshake and identity ---------------------------------------------

    def challenge(
        self, headers: Mapping[str, str], body: bytes
    ) -> Challenge | None:
        """None. Pub/Sub validates a push endpoint by domain ownership, not by
        a challenge round trip."""
        return None

    def delivery_id(self, headers: Mapping[str, str], payload: Any) -> str | None:
        """The Pub/Sub ``messageId``, which is stable across its redeliveries.

        Not the ``historyId``: Pub/Sub delivers at least once and may reorder,
        so two *different* pushes can legitimately carry history ids that
        overlap, and keying on one would drop a genuine notification.
        """
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, dict):
                value = message.get("messageId") or message.get("message_id")
                if value:
                    return str(value)
        return None

    async def expand(
        self, payload: Any, ctx: SourceContext
    ) -> Sequence[InboundEvent]:
        """One pointer event. Nothing is fetched here, deliberately.

        Gmail's history read needs credentials, is rate-limited, and can page —
        doing it inline would put an API round trip inside the three-second
        budget Pub/Sub allows before it retries, and a slow mailbox would then
        produce duplicate pushes as well as a slow one.
        """
        notification = decode_push(payload)
        return [
            InboundEvent(
                type=PUSH_EVENT,
                payload=notification,
                # Per mailbox: two pointers for one inbox must be reconciled in
                # order, or the later history id is consumed first and the
                # earlier read returns nothing.
                key=str(notification.get("emailAddress") or ""),
            )
        ]


def decode_push(payload: Any) -> dict[str, Any]:
    """Unwrap the Pub/Sub envelope around a Gmail notification.

    The envelope is ``{"message": {"data": <base64>, ...}, "subscription": …}``
    and the interesting part is base64 *inside* it — a handler reading
    ``payload["historyId"]`` finds nothing and reports an empty mailbox.
    """
    if not isinstance(payload, dict):
        raise MalformedDelivery("Gmail push was not a JSON object")

    message = payload.get("message")
    if not isinstance(message, dict):
        raise MalformedDelivery(
            "Gmail push had no `message` envelope. Pub/Sub always sends one; "
            "a bare Gmail notification means something is posting directly."
        )

    data = message.get("data")
    if not data:
        raise MalformedDelivery("Gmail push carried no `message.data`")

    try:
        decoded = json.loads(base64.b64decode(data))
    except (ValueError, TypeError) as exc:
        raise MalformedDelivery(
            f"Gmail push `message.data` is not base64-encoded JSON: {exc}"
        ) from exc

    if not isinstance(decoded, dict) or "historyId" not in decoded:
        raise MalformedDelivery(
            "Gmail notification carried no historyId, so there is no position "
            "to reconcile from"
        )

    return {
        "emailAddress": decoded.get("emailAddress", ""),
        # Str, because Gmail sends it as a number here and as a string
        # everywhere else, and a cursor compared across the two is never equal.
        "historyId": str(decoded["historyId"]),
        "publishTime": message.get("publishTime", ""),
        "subscription": payload.get("subscription", ""),
    }


def _bearer(header: str) -> str:
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return ""


class GmailReconciler:
    """Turns a Gmail push pointer into the messages it stands for.

    Implements :class:`~loom.events.reconcile.Reconciler`; hand it to a
    :class:`~loom.events.reconcile.PointerReconciler` and downstream workflows
    see ``app.gmail.message`` with no idea a history read happened.

    ``hydrate=True`` fetches each message so the event carries sender, subject
    and body — one API call per message, which is what a triage workflow needs
    and what a high-volume mailbox cannot afford. ``hydrate=False`` emits ids
    and lets a step fetch what it actually uses.
    """

    id = "gmail"

    def __init__(
        self,
        client: Any = None,
        *,
        hydrate: bool = True,
        max_messages: int = 100,
        history_types: list[str] | None = None,
    ) -> None:
        self._client = client
        self._hydrate = hydrate
        self._max = max_messages
        # `messageAdded` only, by default: a label change is a history record
        # too, and a triage workflow woken by its own labelling is the loop
        # that this shape makes easiest to write by accident.
        self._types = history_types or ["messageAdded"]

    async def _resolve(self) -> Any:
        """The client this source reads history with.

        Async because building one is: it resolves credentials through the
        run's `ToolsetSession` rather than reading a process-wide singleton
        that was built from whatever environment the first caller saw. Its one
        caller is already inside `expand`, so nothing else changes shape.
        """
        if self._client is not None:
            return self._client
        from loom.toolsets.factory import client_for
        from loom.toolsets.google.gmail.client import GmailClient

        return await client_for("gmail", GmailClient)

    async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
        from loom.toolsets.google.errors import GmailHistoryExpired

        position = str(pointer.get("historyId") or "")

        if not cursor:
            # First sight of this mailbox. Adopt the position and emit nothing:
            # back-filling here would replay a mailbox into a workflow that
            # replies, and the dispatch key does not protect against it because
            # every one of those messages is genuinely new to this subscriber.
            logger.info(
                "gmail: first pointer for %s; adopting historyId %s without "
                "back-filling",
                pointer.get("emailAddress", "?"),
                position,
            )
            return Expansion(cursor=position)

        client = await self._resolve()
        try:
            history = await client.list_history(
                cursor, max_results=self._max, history_types=self._types
            )
        except GmailHistoryExpired as exc:
            raise CursorExpired(str(exc), cursor=cursor) from exc

        mailbox = str(pointer.get("emailAddress") or "")
        events = await self._to_events(client, history, mailbox)
        return Expansion(
            events=events,
            # Gmail's own reported position, not the pointer's: the pointer may
            # be behind (Pub/Sub reorders) and rewinding the cursor to it would
            # re-read the same history on every notification.
            cursor=history.history_id or position,
            complete=history.complete,
        )

    async def _to_events(
        self, client: Any, history: Any, mailbox: str
    ) -> list[InboundEvent]:
        ids = history.message_ids[: self._max]
        if not self._hydrate:
            return [
                InboundEvent(
                    type="gmail.message",
                    payload={"id": message_id, "emailAddress": mailbox},
                    key=mailbox,
                    dedupe_suffix=message_id,
                )
                for message_id in ids
            ]

        import asyncio

        fetched = await asyncio.gather(
            *(client.get_message(message_id) for message_id in ids),
            return_exceptions=True,
        )
        events: list[InboundEvent] = []
        for message_id, result in zip(ids, fetched, strict=True):
            if isinstance(result, BaseException):
                # A message deleted between the history read and the fetch is
                # normal, not an error — and one unreadable message must not
                # cost the other ninety-nine.
                logger.debug(
                    "gmail: could not hydrate %s: %s", message_id, result
                )
                continue
            events.append(
                InboundEvent(
                    type="gmail.message",
                    payload=result.model_dump()
                    if hasattr(result, "model_dump")
                    else dict(result),
                    key=mailbox,
                    # The Gmail message id, so two overlapping history reads
                    # produce identical event ids and the second append is a
                    # no-op. Deriving it from the pointer would make every
                    # redelivered notification a fresh set of events.
                    dedupe_suffix=message_id,
                )
            )
        return events


class GmailWatcher:
    """Keeps a mailbox's push registration alive.

    Implements :class:`~loom.events.watch.Watch`. ``users.watch`` is idempotent
    for one mailbox — calling it again extends the existing registration rather
    than creating a second — which is what makes a daily renewal safe.

    The Pub/Sub topic is the full resource name
    (``projects/{project}/topics/{topic}``) and the Gmail service account must
    hold Publish on it. Without that grant the call fails naming the topic, not
    the missing permission, so it reads as a wrong topic name.
    """

    id = "gmail"

    #: Google's documented ceiling. Declared rather than inferred, because
    #: `WatchRegistration.due` otherwise assumes a week — which here happens to
    #: be right and for Graph would be badly wrong.
    LIFETIME_SECONDS = 7 * 24 * 3600

    def __init__(
        self,
        topic_name: str,
        *,
        client_for: Any = None,
        label_ids: list[str] | None = None,
    ) -> None:
        self._topic = topic_name
        self._client_for = client_for
        self._label_ids = label_ids

    def _client(self, resource: str) -> Any:
        if self._client_for is not None:
            return self._client_for(resource)
        from loom.toolsets.google.gmail.client import GmailClient

        # `resource` is the mailbox address; Gmail's own `me` alias only works
        # for the authenticated user, so a service account watching several
        # mailboxes has to name each one.
        return GmailClient(user_id=resource)

    async def register(self, resource: str) -> WatchRegistration:
        from loom.events.watch import lifetime_hint

        watch = await self._client(resource).watch(
            self._topic, label_ids=self._label_ids
        )
        return WatchRegistration(
            resource=resource,
            expires_at=watch.expiration,
            # A watch established now says nothing about what came before it,
            # so this is the position to adopt rather than to back-fill from.
            cursor=watch.history_id,
            metadata=lifetime_hint(self.LIFETIME_SECONDS),
        )

    async def stop(self, resource: str) -> None:
        await self._client(resource).stop_watch()
