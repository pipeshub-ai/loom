"""Gmail REST API v1 client.

Pure httpx — no ``google-api-python-client``. Two pieces carry the weight:
:func:`flatten_message`, which walks the MIME tree the API returns into the flat
model a workflow wants, and :func:`build_mime`, which goes the other way for
sending.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage as MIMEMessage
from email.utils import getaddresses
from typing import TYPE_CHECKING, Any

from workflow_builder.toolsets.google.gmail.models import (
    AttachmentRef,
    EmailMessage,
    GmailLabel,
    GmailProfile,
    MessageRef,
    SentMessage,
)
from workflow_builder.toolsets.google.http import GoogleSession
from workflow_builder.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from workflow_builder.storage.attachment import Attachment
    from workflow_builder.toolsets.google.auth import GoogleAuth

__all__ = ["GmailClient", "build_mime", "flatten_message", "get_default_client"]

API_BASE = "https://gmail.googleapis.com/gmail/v1"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    """Async Gmail client returning typed models."""

    def __init__(
        self,
        auth: GoogleAuth | None = None,
        *,
        user_id: str = "me",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        from workflow_builder.toolsets.google.auth import get_default_auth

        self._user = user_id
        self._session = GoogleSession(
            auth or get_default_auth(SCOPES), API_BASE, transport=transport
        )

    # -- messages ------------------------------------------------------------

    async def list_message_ids(
        self,
        query: str = "",
        max_results: int = 20,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> list[MessageRef]:
        """Search, returning ids only — which is all the list endpoint gives."""
        params: dict[str, Any] = {"q": query or None}
        if label_ids:
            params["labelIds"] = label_ids
        if include_spam_trash:
            params["includeSpamTrash"] = "true"

        raw = await self._session.paginate(
            f"users/{self._user}/messages",
            items_key="messages",
            limit=max_results,
            params=params,
        )
        return raw.mapped(
            lambda item: MessageRef(
                id=item.get("id", ""), thread_id=item.get("threadId", "")
            )
        )

    async def get_message(self, message_id: str, *, format: str = "full") -> EmailMessage:
        """Fetch one message and flatten it."""
        data = await self._session.get(
            f"users/{self._user}/messages/{message_id}", format=format
        )
        return flatten_message(data)

    async def search_messages(
        self,
        query: str = "",
        max_results: int = 20,
        label_ids: list[str] | None = None,
    ) -> Results[EmailMessage]:
        """Search and hydrate each hit.

        The list endpoint returns bare ids, so this is 1 + N requests. That cost
        is why ``max_results`` defaults low and is worth keeping low: asking for
        500 messages is 501 API calls against a per-minute quota.
        """
        import asyncio

        refs = await self.list_message_ids(query, max_results, label_ids)
        if not refs:
            return refs.mapped(lambda ref: ref)
        hydrated = await asyncio.gather(*(self.get_message(ref.id) for ref in refs))
        # Rebuilt from the refs, so the coverage of the *search* carries over —
        # hydration is one request per hit and changes how many there are not
        # at all.
        return Results(
            list(hydrated),
            complete=refs.complete,
            total=refs.total,
            cursor=refs.cursor,
        )

    async def send_message(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        *,
        cc: list[str] | str | None = None,
        bcc: list[str] | str | None = None,
        html: bool = False,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> SentMessage:
        """Send a message. Returns the receipt, not the stored message."""
        mime = build_mime(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            in_reply_to=in_reply_to,
            references=references,
        )
        payload: dict[str, Any] = {"raw": mime}
        if thread_id:
            payload["threadId"] = thread_id

        data = await self._session.post(f"users/{self._user}/messages/send", payload)
        return _sent(data)

    async def reply_to_message(self, message_id: str, body: str, *, html: bool = False,
                               reply_all: bool = False) -> SentMessage:
        """Reply in-thread, preserving the headers that make it a reply.

        Setting ``threadId`` alone is not enough — Gmail will file it in the
        thread, but every other mail client keys off ``In-Reply-To`` and
        ``References``, so a reply without them starts a new conversation for
        the person receiving it.
        """
        original = await self._session.get(
            f"users/{self._user}/messages/{message_id}", format="full"
        )
        headers = _headers(original)
        flat = flatten_message(original)

        message_ref = headers.get("message-id", "")
        recipients = [flat.sender] if flat.sender else []
        if reply_all:
            recipients += [a for a in flat.to if a]
        subject = flat.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        return await self.send_message(
            to=recipients,
            subject=subject,
            body=body,
            cc=flat.cc if reply_all else None,
            html=html,
            thread_id=flat.thread_id,
            in_reply_to=message_ref,
            references=" ".join(
                part for part in (headers.get("references", ""), message_ref) if part
            ),
        )

    async def modify_labels(
        self,
        message_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> EmailMessage:
        """Add and/or remove labels. Archiving is removing ``INBOX``."""
        data = await self._session.post(
            f"users/{self._user}/messages/{message_id}/modify",
            {"addLabelIds": add or [], "removeLabelIds": remove or []},
        )
        return flatten_message(data)

    async def trash_message(self, message_id: str) -> EmailMessage:
        """Move to trash. Recoverable for 30 days; ``delete`` is not exposed."""
        data = await self._session.post(
            f"users/{self._user}/messages/{message_id}/trash"
        )
        return flatten_message(data)

    async def get_attachment(
        self, message_id: str, attachment_id: str, filename: str = "attachment"
    ) -> Attachment:
        """Download one attachment as a LOOM :class:`Attachment`."""
        from workflow_builder.storage.attachment import Attachment

        data = await self._session.get(
            f"users/{self._user}/messages/{message_id}/attachments/{attachment_id}"
        )
        return Attachment.from_bytes(
            filename,
            _b64url_decode(data.get("data", "")),
            message_id=message_id,
        )

    # -- labels and profile --------------------------------------------------

    async def list_labels(self) -> list[GmailLabel]:
        data = await self._session.get(f"users/{self._user}/labels")
        return [
            GmailLabel(
                id=item.get("id", ""),
                name=item.get("name", ""),
                type=item.get("type", ""),
                messages_total=int(item.get("messagesTotal", 0) or 0),
                messages_unread=int(item.get("messagesUnread", 0) or 0),
            )
            for item in (data or {}).get("labels", [])
        ]

    async def get_profile(self) -> GmailProfile:
        data = await self._session.get(f"users/{self._user}/profile")
        return GmailProfile(
            email_address=data.get("emailAddress", ""),
            messages_total=int(data.get("messagesTotal", 0) or 0),
            threads_total=int(data.get("threadsTotal", 0) or 0),
        )


# ---------------------------------------------------------------------------
# MIME
# ---------------------------------------------------------------------------


def build_mime(
    *,
    to: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    html: bool = False,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """Build an RFC 2822 message, base64url-encoded as the API wants it.

    ``email.message.EmailMessage`` handles the parts that are easy to get subtly
    wrong by hand: non-ASCII subjects, header folding, and quoting a display
    name that contains a comma.
    """
    message = MIMEMessage()
    message["To"] = _joined(to)
    message["Subject"] = subject
    if cc:
        message["Cc"] = _joined(cc)
    if bcc:
        message["Bcc"] = _joined(bcc)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    if html:
        message.set_content("", subtype="plain")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)

    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def flatten_message(raw: dict[str, Any]) -> EmailMessage:
    """Flatten a Gmail message resource into :class:`EmailMessage`."""
    headers = _headers(raw)
    payload = raw.get("payload") or {}
    body, attachments = _walk(payload)

    message_id = raw.get("id", "")
    return EmailMessage(
        id=message_id,
        thread_id=raw.get("threadId", ""),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        to=_addresses(headers.get("to", "")),
        cc=_addresses(headers.get("cc", "")),
        date=headers.get("date", ""),
        snippet=raw.get("snippet", ""),
        body=body,
        label_ids=list(raw.get("labelIds") or []),
        attachments=attachments,
        url=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
    )


def _walk(part: dict[str, Any]) -> tuple[str, list[AttachmentRef]]:
    """Depth-first walk of a MIME tree, collecting the text and attachments.

    Prefers ``text/plain``; falls back to ``text/html`` only when no plain part
    exists anywhere in the tree, since a multipart/alternative carries both and
    the plain one is what a model should read.
    """
    plain: list[str] = []
    html: list[str] = []
    found: list[AttachmentRef] = []

    stack = [part]
    while stack:
        node = stack.pop(0)
        mime = node.get("mimeType", "")
        body = node.get("body") or {}
        filename = node.get("filename") or ""

        if body.get("attachmentId"):
            found.append(
                AttachmentRef(
                    attachment_id=body["attachmentId"],
                    filename=filename or "attachment",
                    mime=mime or "application/octet-stream",
                    size=int(body.get("size", 0) or 0),
                )
            )
        elif body.get("data"):
            text = _b64url_decode(body["data"]).decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)

        stack.extend(node.get("parts") or [])

    if plain:
        return "\n".join(plain).strip(), found
    return _strip_html("\n".join(html)).strip(), found


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Header name (lowercased) → value."""
    payload = raw.get("payload") or {}
    return {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in payload.get("headers") or []
    }


def _addresses(value: str) -> list[str]:
    """Split an address header, keeping display names intact."""
    if not value:
        return []
    return [
        f"{name} <{addr}>" if name else addr
        for name, addr in getaddresses([value])
        if addr or name
    ]


def _joined(value: list[str] | str) -> str:
    return value if isinstance(value, str) else ", ".join(value)


def _b64url_decode(data: str) -> bytes:
    """Decode base64url, restoring the padding Gmail strips."""
    if not data:
        return b""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _strip_html(html: str) -> str:
    """Crude tag strip, used only when a message has no plain-text part."""
    import re

    without_blocks = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I
    )
    text = re.sub(r"<br\s*/?>|</p>", "\n", without_blocks, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    return re.sub(r"[ \t]{2,}", " ", text)


def _sent(data: dict[str, Any]) -> SentMessage:
    message_id = data.get("id", "")
    return SentMessage(
        id=message_id,
        thread_id=data.get("threadId", ""),
        label_ids=list(data.get("labelIds") or []),
        url=f"https://mail.google.com/mail/u/0/#sent/{message_id}",
    )


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default_client: GmailClient | None = None


def get_default_client() -> GmailClient:
    """Return (or build) the module-level client from environment credentials."""
    global _default_client
    if _default_client is None:
        _default_client = GmailClient()
    return _default_client


def reset_default_client() -> None:
    """Drop the cached client. For tests, and after a credential change."""
    global _default_client
    _default_client = None
