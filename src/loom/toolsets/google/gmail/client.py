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

from loom.toolsets.google.gmail.models import (
    AttachmentRef,
    EmailMessage,
    EmailThread,
    GmailDraft,
    GmailLabel,
    GmailProfile,
    MessageRef,
    SentMessage,
)
from loom.toolsets.google.http import DEFAULT_TIMEOUT, GoogleSession
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment
    from loom.toolsets.google.auth import GoogleAuth

__all__ = ["GmailClient", "build_mime", "flatten_message", "get_default_client"]

API_BASE = "https://gmail.googleapis.com/gmail/v1"

#: Gmail's ceiling for one ``messages.batchModify`` call.
BATCH_LIMIT = 1000
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
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        from loom.toolsets.google.auth import get_default_auth

        self._user = user_id
        self._session = GoogleSession(
            auth or get_default_auth(SCOPES), API_BASE,
            transport=transport, timeout=timeout,
        )

    # -- messages ------------------------------------------------------------

    async def list_message_ids(
        self,
        query: str = "",
        max_results: int = 20,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> Results[MessageRef]:
        """Search, returning ids only — which is all the list endpoint gives.

        ``Results`` rather than ``list``: it pages, and :meth:`search_messages`
        reads the coverage off it to carry through to its own answer.
        """
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
        attachments: list[Attachment] | None = None,
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
            attachments=attachments,
        )
        payload: dict[str, Any] = {"raw": mime}
        if thread_id:
            payload["threadId"] = thread_id

        data = await self._session.post(f"users/{self._user}/messages/send", payload)
        return _sent(data)

    async def forward_message(
        self,
        message_id: str,
        to: list[str] | str,
        *,
        comment: str = "",
        html: bool = False,
    ) -> SentMessage:
        """Forward a message, quoting the original beneath any comment.

        Sends a *new* message rather than adding a recipient to the existing
        thread: adding one would deliver the whole prior conversation to
        someone who was never part of it, and would do so silently.
        """
        original = await self.get_message(message_id)
        quoted = (
            f"{comment}\n\n" if comment else ""
        ) + (
            "---------- Forwarded message ----------\n"
            f"From: {original.sender}\n"
            f"Date: {original.date}\n"
            f"Subject: {original.subject}\n"
            f"To: {', '.join(original.to)}\n\n"
            f"{original.body}"
        )
        subject = original.subject
        if not subject.lower().startswith("fwd:"):
            subject = f"Fwd: {subject}"
        return await self.send_message(to, subject, quoted, html=html)

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

    async def batch_modify_labels(
        self,
        message_ids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> int:
        """Label many messages in one request.

        Gmail's quota is per *request*, not per message, so archiving 200
        messages one call at a time is 200 units against a per-minute budget
        and this is one. The endpoint answers 204 with no body, so the count is
        what was asked for — Gmail reports no per-id outcome.
        """
        if not message_ids:
            return 0
        if len(message_ids) > BATCH_LIMIT:
            # Gmail answers 400 for more, which surfaces as "Precondition
            # check failed" and names neither the limit nor the argument.
            raise ValueError(
                f"batch_modify_labels takes at most {BATCH_LIMIT} ids at a "
                f"time, got {len(message_ids)}. Chunk the list — each call is "
                "one quota unit, so the chunking is cheap."
            )
        await self._session.post(
            f"users/{self._user}/messages/batchModify",
            {
                "ids": message_ids,
                "addLabelIds": add or [],
                "removeLabelIds": remove or [],
            },
        )
        return len(message_ids)

    async def trash_message(self, message_id: str) -> EmailMessage:
        """Move to trash. Recoverable for 30 days."""
        data = await self._session.post(
            f"users/{self._user}/messages/{message_id}/trash"
        )
        return flatten_message(data)

    async def untrash_message(self, message_id: str) -> EmailMessage:
        """Take a message back out of the trash."""
        data = await self._session.post(
            f"users/{self._user}/messages/{message_id}/untrash"
        )
        return flatten_message(data)

    # Permanent delete is deliberately not exposed. Gmail's ``messages.delete``
    # requires ``https://mail.google.com/`` — a *restricted* scope granting full
    # mailbox access — so shipping one unrecoverable operation would widen what
    # every Gmail workflow has to be granted, and put the toolset into Google's
    # restricted-scope verification. Trash is recoverable for 30 days and is
    # what a workflow means by "delete"; ``untrash_message`` undoes it.

    # -- threads -------------------------------------------------------------

    async def list_threads(
        self,
        query: str = "",
        max_results: int = 20,
        label_ids: list[str] | None = None,
    ) -> Results[EmailThread]:
        """Search conversations, returning ids and snippets only.

        Cheap, unlike :meth:`search_messages`: one request per page rather than
        one per hit. Fetch the ones that matter with :meth:`get_thread`.
        """
        params: dict[str, Any] = {"q": query or None}
        if label_ids:
            params["labelIds"] = label_ids

        raw = await self._session.paginate(
            f"users/{self._user}/threads",
            items_key="threads",
            limit=max_results,
            params=params,
        )
        return raw.mapped(
            lambda item: EmailThread(
                id=item.get("id", ""),
                snippet=item.get("snippet", ""),
                # A list response carries no messages and no count; leaving
                # message_count at 0 says "unknown" rather than "empty thread".
            )
        )

    async def get_thread(self, thread_id: str) -> EmailThread:
        """Fetch a whole conversation, every message flattened."""
        data = await self._session.get(
            f"users/{self._user}/threads/{thread_id}", format="full"
        )
        return _thread(data)

    async def modify_thread_labels(
        self,
        thread_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> EmailThread:
        """Label an entire conversation.

        What inbox triage almost always wants: labelling one message of a
        thread leaves the conversation looking untouched in the Gmail UI, which
        groups by thread.
        """
        data = await self._session.post(
            f"users/{self._user}/threads/{thread_id}/modify",
            {"addLabelIds": add or [], "removeLabelIds": remove or []},
        )
        return _thread(data)

    async def trash_thread(self, thread_id: str) -> EmailThread:
        """Move a whole conversation to the trash. Recoverable for 30 days."""
        data = await self._session.post(
            f"users/{self._user}/threads/{thread_id}/trash"
        )
        return _thread(data)

    # -- drafts --------------------------------------------------------------

    async def create_draft(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        *,
        cc: list[str] | str | None = None,
        bcc: list[str] | str | None = None,
        html: bool = False,
        thread_id: str = "",
        attachments: list[Attachment] | None = None,
    ) -> GmailDraft:
        """Compose without sending.

        The safe half of sending, and what makes a draft-then-approve workflow
        possible: an agent writes the mail, ``ctx.wait_for_approval()`` parks
        the run, and a human sends it — or does not.
        """
        mime = build_mime(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=attachments,
        )
        message: dict[str, Any] = {"raw": mime}
        if thread_id:
            message["threadId"] = thread_id

        data = await self._session.post(
            f"users/{self._user}/drafts", {"message": message}
        )
        return _draft(data, to=to, subject=subject)

    async def list_drafts(self, max_results: int = 20) -> Results[GmailDraft]:
        """List unsent drafts, following every page."""
        raw = await self._session.paginate(
            f"users/{self._user}/drafts", items_key="drafts", limit=max_results
        )
        return raw.mapped(lambda item: _draft(item))

    async def send_draft(self, draft_id: str) -> SentMessage:
        """Send an existing draft. The half a human approves."""
        data = await self._session.post(
            f"users/{self._user}/drafts/send", {"id": draft_id}
        )
        return _sent(data)

    async def delete_draft(self, draft_id: str) -> None:
        """Discard a draft. It was never delivered, so nothing is recalled."""
        await self._session.delete(f"users/{self._user}/drafts/{draft_id}")

    async def get_attachment(
        self, message_id: str, attachment_id: str, filename: str = "attachment"
    ) -> Attachment:
        """Download one attachment as a LOOM :class:`Attachment`."""
        from loom.blobs.attachment import Attachment

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
        return [_label(item) for item in (data or {}).get("labels", [])]

    async def find_label(self, name: str) -> GmailLabel | None:
        """The label with exactly this name, or ``None``.

        Gmail has no lookup-by-name, so this lists and matches. Exactly, and
        case-insensitively: labelling takes ``Label_7``, a person says
        "Urgent", and ``add=["Urgent"]`` is not an error — Gmail accepts it and
        applies nothing, which is the silent no-op this resolver exists to
        prevent. Nested labels are matched on their full path
        (``"Clients/Acme"``), because that is what the id refers to.
        """
        wanted = name.strip().lower()
        for label in await self.list_labels():
            if label.name.lower() == wanted:
                return label
        return None

    async def create_label(
        self,
        name: str,
        *,
        label_list_visibility: str = "labelShow",
        message_list_visibility: str = "show",
    ) -> GmailLabel:
        """Create a user label.

        Nested labels are a naming convention, not a structure: ``"Clients/Acme"``
        creates ``Acme`` under ``Clients``, and the parent must already exist.
        """
        data = await self._session.post(
            f"users/{self._user}/labels",
            {
                "name": name,
                "labelListVisibility": label_list_visibility,
                "messageListVisibility": message_list_visibility,
            },
        )
        return _label(data)

    async def update_label(self, label_id: str, name: str) -> GmailLabel:
        """Rename a user label. System labels cannot be renamed."""
        data = await self._session.patch(
            f"users/{self._user}/labels/{label_id}", {"name": name}
        )
        return _label(data)

    async def delete_label(self, label_id: str) -> None:
        """Delete a user label, removing it from every message that had it.

        The messages themselves survive — this deletes the label, not the mail.
        """
        await self._session.delete(f"users/{self._user}/labels/{label_id}")

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
    attachments: list[Attachment] | None = None,
) -> str:
    """Build an RFC 2822 message, base64url-encoded as the API wants it.

    ``email.message.EmailMessage`` handles the parts that are easy to get subtly
    wrong by hand: non-ASCII subjects, header folding, quoting a display name
    that contains a comma, and base64-encoding a binary attachment into a part
    with the right transfer encoding.

    An **offloaded** attachment raises rather than sending an empty file: its
    bytes live in blob storage and this function has no blob service, so the
    caller must ``await attachment.read(blobs)`` first. Silently attaching zero
    bytes would produce a mail that looks sent and delivers nothing.
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

    for attachment in attachments or []:
        if attachment.data is None:
            raise ValueError(
                f"attachment {attachment.filename!r} has no inline bytes "
                f"(it is stored at {attachment.ref}). Call "
                "`await attachment.read(blobs)` and rebuild it with "
                "Attachment.from_bytes before sending."
            )
        main, _, sub = (attachment.mime or "application/octet-stream").partition("/")
        message.add_attachment(
            attachment.data,
            maintype=main or "application",
            subtype=sub or "octet-stream",
            filename=attachment.filename,
        )

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


def _thread(raw: dict[str, Any]) -> EmailThread:
    """Flatten a thread resource, messages and all."""
    messages = [flatten_message(item) for item in (raw or {}).get("messages") or []]
    labels: list[str] = []
    for message in messages:
        # The union across the conversation, which is what Gmail shows on it —
        # a thread is unread if any single message in it is.
        labels.extend(label for label in message.label_ids if label not in labels)

    return EmailThread(
        id=raw.get("id", ""),
        snippet=raw.get("snippet", ""),
        messages=messages,
        message_count=len(messages),
        label_ids=labels,
    )


def _draft(
    raw: dict[str, Any], *, to: list[str] | str | None = None, subject: str = ""
) -> GmailDraft:
    """Flatten a draft resource.

    A create response carries only ids, so ``to`` and ``subject`` are taken
    from the call that made it — echoing back what was asked for rather than
    leaving the caller with a draft it cannot identify. A *list* response has
    neither, and both stay empty rather than being guessed at.
    """
    message = raw.get("message") or {}
    return GmailDraft(
        id=raw.get("id", ""),
        message_id=message.get("id", ""),
        thread_id=message.get("threadId", ""),
        subject=subject,
        to=[to] if isinstance(to, str) else list(to or []),
        snippet=message.get("snippet", ""),
    )


def _label(raw: dict[str, Any]) -> GmailLabel:
    return GmailLabel(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        type=raw.get("type", ""),
        messages_total=int(raw.get("messagesTotal", 0) or 0),
        messages_unread=int(raw.get("messagesUnread", 0) or 0),
    )


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
