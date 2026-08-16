"""Outlook mail over Microsoft Graph — pure httpx, no vendor SDK.

Four facts from the reference shape this client, and each one is a wrong answer
rather than an error if missed:

1. **Bodies are HTML unless you ask.** "Currently, this operation returns
   message bodies in only HTML format." So ``Prefer:
   outlook.body-content-type="text"`` is sent by default; a model handed
   ``<div>`` markup spends tokens on it and reads worse.
2. **``$filter`` and ``$orderby`` have an ordering contract.** Properties in
   the sort must also appear in the filter, in the same order, before any
   others — or Graph answers ``InefficientFilter`` with a message naming
   neither. The rule is documented on the tool.
3. **A big page can time out.** The reference warns a page of full messages
   "may trigger the gateway timeout (HTTP 504)" and recommends ``$select``, so
   a default projection is sent and the page size is 50, not the 1,000 maximum.
4. **``sendMail`` returns 202, which is acceptance and not delivery.**
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loom.toolsets.microsoft.auth import (
    GRAPH_BASE_URL,
    MicrosoftAuth,
    get_default_auth,
    graph_base_url,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.outlook.models import (
    MailFolder,
    OutlookMessage,
    Recipient,
)
from loom.toolsets.microsoft.scope import user_root
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment

__all__ = ["MESSAGE_FIELDS", "OutlookMailClient", "get_default_client", "reset_default_client"]

#: What every message read projects. Named explicitly rather than left to
#: Graph's default because the reference warns that returning full payloads at a
#: large page size can hit a gateway timeout — and because a message carries
#: fields (the full body on a listing, message headers) that no caller of a
#: *listing* wants.
MESSAGE_FIELDS = (
    "id,subject,bodyPreview,from,sender,toRecipients,ccRecipients,replyTo,"
    "receivedDateTime,sentDateTime,isRead,isDraft,hasAttachments,importance,"
    "conversationId,parentFolderId,webLink,categories,internetMessageId"
)

#: The same, plus the body — for reading one message rather than listing many.
MESSAGE_FIELDS_WITH_BODY = MESSAGE_FIELDS + ",body"


class OutlookMailClient:
    """Messages, folders, and sending, in an Exchange Online mailbox."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        user_id: str = "",
        # 60s rather than 30: an attachment arrives base64-encoded inside
        # the JSON body, so a large one is a transfer and not an API call.
        timeout: float = 60.0,
        prefer_text_bodies: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._user_id = user_id
        self._prefer_text = prefer_text_bodies
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    def _root(self) -> str:
        return user_root(
            self._auth,
            self._user_id,
            workload="a mailbox",
            env_hint="MS_OUTLOOK_USER",
        )

    def _prefer(self, body_as_html: bool) -> dict[str, str]:
        """The Prefer header that decides body format.

        Sent on every read rather than only when text is wanted, because the
        absence of the header means HTML — so "leave it off" is itself a
        choice, and a silent one.
        """
        if body_as_html or not self._prefer_text:
            return {}
        return {"Prefer": 'outlook.body-content-type="text"'}

    # -- identity ------------------------------------------------------------

    async def whoami(self) -> MicrosoftUser:
        root = user_root(self._auth, workload="a signed-in user")
        return MicrosoftUser.from_api(await self._session.get(root))

    # -- reading -------------------------------------------------------------

    async def list_messages(
        self,
        *,
        folder_id: str = "",
        limit: int = 50,
        filter_query: str = "",
        order_by: str = "receivedDateTime desc",
        body_as_html: bool = False,
    ) -> Results[OutlookMessage]:
        path = (
            f"{self._root()}/mailFolders/{quote(folder_id, safe='')}/messages"
            if folder_id
            else f"{self._root()}/messages"
        )
        params: dict[str, Any] = {"$select": MESSAGE_FIELDS}
        if filter_query:
            params["$filter"] = filter_query
        if order_by:
            params["$orderby"] = order_by
        return await self._session.paginate(
            path,
            limit=limit,
            params=params,
            page_size=50,
            row=OutlookMessage.from_api,
            headers=self._prefer(body_as_html),
        )

    async def search_messages(
        self, query: str, *, limit: int = 50, body_as_html: bool = False
    ) -> Results[OutlookMessage]:
        """Search the mailbox.

        No ordering argument, deliberately: Graph ranks ``$search`` results by
        relevance rather than sorting them, so accepting a sort would let a
        caller believe in an order that is not applied.
        """
        return await self._session.paginate(
            f"{self._root()}/messages",
            limit=limit,
            params={"$search": f'"{query}"', "$select": MESSAGE_FIELDS},
            page_size=50,
            row=OutlookMessage.from_api,
            headers=self._prefer(body_as_html),
        )

    async def get_message(
        self, message_id: str, *, body_as_html: bool = False
    ) -> OutlookMessage:
        raw = await self._session.request(
            "GET",
            f"{self._root()}/messages/{quote(message_id, safe='')}",
            params={"$select": MESSAGE_FIELDS_WITH_BODY},
            headers=self._prefer(body_as_html),
        )
        return OutlookMessage.from_api(raw or {})

    async def list_folders(self, *, limit: int = 100) -> Results[MailFolder]:
        return await self._session.paginate(
            f"{self._root()}/mailFolders",
            limit=limit,
            page_size=100,
            row=MailFolder.from_api,
        )

    async def list_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """List a message's attachments as metadata only.

        Bytes are deliberately not fetched here: an attachment can be tens of
        megabytes, and a listing that inlined them would put all of it into the
        journal. ``get_attachment`` fetches one.
        """
        body = await self._session.get(
            f"{self._root()}/messages/{quote(message_id, safe='')}/attachments",
            **{"$select": "id,name,contentType,size,isInline"},
        )
        return [
            {
                "id": str(item.get("id", "")),
                "name": item.get("name") or "",
                "content_type": item.get("contentType") or "",
                "size": int(item.get("size") or 0),
                "is_inline": bool(item.get("isInline", False)),
            }
            for item in (body or {}).get("value", []) or []
        ]

    async def get_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        from loom.blobs.attachment import Attachment

        raw = await self._session.get(
            f"{self._root()}/messages/{quote(message_id, safe='')}"
            f"/attachments/{quote(attachment_id, safe='')}"
        )
        raw = raw or {}
        encoded = raw.get("contentBytes")
        if encoded is None:
            raise GraphPermanentError(
                f"Attachment {raw.get('name') or attachment_id!r} carries no "
                "contentBytes — it is probably an item or reference attachment "
                "(a linked file or an attached mail) rather than a file.",
                status=0,
                code="notAFileAttachment",
            )
        import base64

        return Attachment.from_bytes(
            str(raw.get("name") or attachment_id),
            base64.b64decode(encoded),
            mime=str(raw.get("contentType") or "application/octet-stream"),
        )

    # -- writing -------------------------------------------------------------

    async def send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_type: str = "text",
        save_to_sent: bool = True,
        attachments: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Send a message. Returns True meaning *accepted*, not delivered.

        Graph answers ``202 Accepted``, and its own note is that this "doesn't
        indicate that the request processing has completed". Reporting "sent"
        would be a workflow claiming something it cannot know.
        """
        message = _compose(to, subject, body, cc, bcc, body_type, attachments)
        payload: dict[str, Any] = {"message": message}
        if not save_to_sent:
            payload["saveToSentItems"] = False
        await self._session.post(f"{self._root()}/sendMail", json=payload)
        return True

    async def reply(
        self, message_id: str, comment: str, *, reply_all: bool = False
    ) -> bool:
        action = "replyAll" if reply_all else "reply"
        await self._session.post(
            f"{self._root()}/messages/{quote(message_id, safe='')}/{action}",
            json={"comment": comment},
        )
        return True

    async def forward(
        self, message_id: str, to: list[str], *, comment: str = ""
    ) -> bool:
        await self._session.post(
            f"{self._root()}/messages/{quote(message_id, safe='')}/forward",
            json={
                "toRecipients": [Recipient(address=a).to_api() for a in to],
                "comment": comment,
            },
        )
        return True

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_type: str = "text",
    ) -> OutlookMessage:
        raw = await self._session.post(
            f"{self._root()}/messages",
            json=_compose(to, subject, body, cc, bcc, body_type, None),
        )
        return OutlookMessage.from_api(raw or {})

    async def send_draft(self, message_id: str) -> bool:
        """Send a draft that already exists.

        The other half of :meth:`create_draft`, and the reason both exist: an
        agent writes the draft, ``ctx.wait_for_approval()`` parks the run, a
        person reads it, and *this* sends it. Without this the approval pattern
        stops one step short and the mail has to be sent by hand.

        Like ``sendMail``, Graph answers ``202`` — accepted, not delivered.
        """
        await self._session.post(
            f"{self._root()}/messages/{quote(message_id, safe='')}/send"
        )
        return True

    async def update_message(
        self,
        message_id: str,
        *,
        is_read: bool | None = None,
        categories: list[str] | None = None,
        importance: str = "",
    ) -> OutlookMessage:
        changes: dict[str, Any] = {}
        if is_read is not None:
            changes["isRead"] = is_read
        if categories is not None:
            changes["categories"] = categories
        if importance:
            changes["importance"] = importance
        if not changes:
            raise GraphPermanentError(
                "update_message needs something to change; with no arguments "
                "it is a no-op PATCH that reads as a successful update.",
                status=0,
                code="nothingToDo",
            )
        raw = await self._session.patch(
            f"{self._root()}/messages/{quote(message_id, safe='')}", json=changes
        )
        return OutlookMessage.from_api(raw or {})

    async def move_message(self, message_id: str, folder_id: str) -> OutlookMessage:
        raw = await self._session.post(
            f"{self._root()}/messages/{quote(message_id, safe='')}/move",
            json={"destinationId": folder_id},
        )
        return OutlookMessage.from_api(raw or {})

    async def delete_message(self, message_id: str) -> bool:
        """Move a message to Deleted Items.

        Graph's ``DELETE`` on a message is a soft delete — the message lands in
        Deleted Items and is recoverable, the same shape as Gmail's trash.
        """
        await self._session.delete(
            f"{self._root()}/messages/{quote(message_id, safe='')}"
        )
        return True


def _compose(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    body_type: str,
    attachments: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build a ``message`` payload."""
    if body_type.lower() not in ("text", "html"):
        raise GraphPermanentError(
            f"body_type must be 'text' or 'html', not {body_type!r}.",
            status=0,
            code="invalidBodyType",
        )
    if not to:
        raise GraphPermanentError(
            "A message needs at least one recipient in 'to'.",
            status=0,
            code="noRecipients",
        )
    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": body_type.lower(), "content": body},
        "toRecipients": [Recipient(address=a).to_api() for a in to],
    }
    if cc:
        message["ccRecipients"] = [Recipient(address=a).to_api() for a in cc]
    if bcc:
        message["bccRecipients"] = [Recipient(address=a).to_api() for a in bcc]
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": item["name"],
                "contentType": item.get("content_type", "application/octet-stream"),
                "contentBytes": item["content_bytes"],
            }
            for item in attachments
        ]
    return message


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default: OutlookMailClient | None = None


def get_default_client() -> OutlookMailClient:
    """Return the process-wide client, building it on first use."""
    global _default
    if _default is None:
        import os

        _default = OutlookMailClient(            base_url=graph_base_url(),
user_id=os.environ.get("MS_OUTLOOK_USER", ""))
    return _default


def reset_default_client() -> None:
    """Drop the process-wide client. For tests, and for a credential rotation."""
    global _default
    _default = None
