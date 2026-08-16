"""Outlook mail step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.outlook.mail.tools import outlook_list_messages

    inbox = await ctx.step(outlook_list_messages, folder_id="inbox", limit=25)

**Whose mailbox.** Under delegated credentials these act on the signed-in
person's mailbox. Under an app-only token there is no signed-in person, so set
``MS_OUTLOOK_USER`` or pass a mailbox explicitly; the tools refuse with that
instruction rather than letting Graph answer a confusing 400.

**Bodies come back as text.** Graph returns HTML unless asked otherwise, so
these tools send ``Prefer: outlook.body-content-type="text"`` by default. Pass
``body_as_html=True`` when you need the markup.

**Filtering and sorting have an ordering contract.** If you pass both
``filter_query`` and ``order_by``, every property you sort by must also appear
in the filter, in the same order, before any property you do not sort by.
Otherwise Graph answers ``InefficientFilter`` — "The restriction or sort order
is too complex for this operation" — naming neither the filter nor the sort.

Retries are per operation. Reads retry; **sending, replying and forwarding do
not**, because Graph has no idempotency key for a send and a retry after a
timeout mails everybody twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom import Retry, step
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.outlook.models import MailFolder, OutlookMessage
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def outlook_whoami() -> MicrosoftUser:
    """Return the person these credentials authenticate as.

    Fails under an app-only token, which has no signed-in user.

    Returns:
        The signed-in user's id, display name, email, and userPrincipalName.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().whoami()


@step(retry=_READ)
async def outlook_list_folders(limit: int = 100) -> Results[MailFolder]:
    """List the mailbox's folders.

    Resolve a folder here before filtering by one. Well-known names —
    ``"inbox"``, ``"sentitems"``, ``"drafts"``, ``"deleteditems"`` — also work
    as ids anywhere a folder id is taken, so a listing is only needed for
    user-created folders.

    Args:
        limit: Maximum folders. Defaults to 100.

    Returns:
        Results of MailFolder with item and unread counts.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().list_folders(limit=limit)


@step(retry=_READ)
async def outlook_list_messages(
    folder_id: str = "",
    limit: int = 50,
    filter_query: str = "",
    order_by: str = "receivedDateTime desc",
    body_as_html: bool = False,
) -> Results[OutlookMessage]:
    """List messages, newest first.

    Args:
        folder_id: Restrict to one folder. Accepts a well-known name such as
            ``"inbox"``. Omit to list the whole mailbox, Deleted Items
            included.
        limit: Maximum messages across pages. Defaults to 50.
        filter_query: OData ``$filter``, e.g.
            ``"isRead eq false and importance eq 'high'"``.
        order_by: OData ``$orderby``. Defaults to newest received first. If you
            also pass ``filter_query``, every sorted property must appear in
            the filter first — see the module docstring — or Graph returns
            ``InefficientFilter``.
        body_as_html: Return HTML bodies instead of text.

    Returns:
        Results of OutlookMessage. Bodies are omitted from a listing to keep
        the page small; use ``outlook_get_message`` for one message's body, or
        read ``body_preview``, which is always present.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().list_messages(
        folder_id=folder_id,
        limit=limit,
        filter_query=filter_query,
        order_by=order_by,
        body_as_html=body_as_html,
    )


@step(retry=_READ)
async def outlook_search_messages(
    query: str, limit: int = 50, body_as_html: bool = False
) -> Results[OutlookMessage]:
    """Search the mailbox by free text, including attachment contents.

    No ordering argument: Graph ranks search results by relevance rather than
    sorting them, so a sort passed here would be believed and not applied.

    Args:
        query: Text to search for. Also accepts Outlook's field syntax, e.g.
            ``"subject:invoice"`` or ``"from:ada@example.com"``.
        limit: Maximum messages across pages. Defaults to 50.
        body_as_html: Return HTML bodies instead of text.

    Returns:
        Results of OutlookMessage, ranked by relevance.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().search_messages(
        query, limit=limit, body_as_html=body_as_html
    )


@step(retry=_READ)
async def outlook_get_message(
    message_id: str, body_as_html: bool = False
) -> OutlookMessage:
    """Fetch one message, including its body.

    Args:
        message_id: The message's id.
        body_as_html: Return the HTML body instead of text.

    Returns:
        OutlookMessage with body, recipients, and ``conversation_id`` — Outlook
        groups by conversation, so acting on one message of a thread can look
        like nothing happened.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().get_message(
        message_id, body_as_html=body_as_html
    )


@step(retry=_READ)
async def outlook_list_attachments(message_id: str) -> list[dict[str, Any]]:
    """List a message's attachments, metadata only.

    Bytes are not fetched here: an attachment can be tens of megabytes, and a
    listing that inlined them would put all of it in the journal.

    Args:
        message_id: The message whose attachments to list.

    Returns:
        A list of dicts with ``id``, ``name``, ``content_type``, ``size``, and
        ``is_inline``. Inline attachments are embedded images, not documents.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().list_attachments(message_id)


@step(retry=_READ)
async def outlook_get_attachment(message_id: str, attachment_id: str) -> Attachment:
    """Download one attachment as a LOOM Attachment.

    Fails on an item or reference attachment — an attached mail, or a link to a
    file — which carries no bytes; the error says which.

    Args:
        message_id: The message the attachment is on.
        attachment_id: The attachment id from ``outlook_list_attachments``.

    Returns:
        Attachment with filename, mime, size, and content. Pass it to
        ``ctx.put_artifact``, or ``att.offload(blobs)`` to keep the bytes out
        of the journal.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().get_attachment(message_id, attachment_id)


@step(retry=_UNSAFE_WRITE)
async def outlook_send_message(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    body_type: str = "text",
    save_to_sent: bool = True,
) -> bool:
    """Send an email.

    Not retried: Graph has no idempotency key for a send, so a retry after a
    timeout mails everyone a second copy.

    Args:
        to: Recipient addresses. At least one is required.
        subject: Subject line.
        body: Message body.
        cc: Carbon-copy addresses.
        bcc: Blind carbon-copy addresses.
        body_type: ``"text"`` (default) or ``"html"``.
        save_to_sent: False sends without keeping a copy in Sent Items.

    Returns:
        True meaning Graph **accepted** the message (HTTP 202). That is not a
        delivery confirmation — Graph's own note is that acceptance "doesn't
        indicate that the request processing has completed".
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().send_message(
        to,
        subject,
        body,
        cc=cc,
        bcc=bcc,
        body_type=body_type,
        save_to_sent=save_to_sent,
    )


@step(retry=_UNSAFE_WRITE)
async def outlook_reply_to_message(
    message_id: str, comment: str, reply_all: bool = False
) -> bool:
    """Reply to a message, keeping it in the same conversation.

    Not retried: a retry sends the reply twice.

    Args:
        message_id: The message to reply to.
        comment: The reply text, added above the quoted original.
        reply_all: True replies to every recipient, not just the sender.

    Returns:
        True when Graph accepted the reply.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().reply(message_id, comment, reply_all=reply_all)


@step(retry=_UNSAFE_WRITE)
async def outlook_forward_message(
    message_id: str, to: list[str], comment: str = ""
) -> bool:
    """Forward a message.

    Not retried: a retry forwards it twice.

    Args:
        message_id: The message to forward.
        to: Addresses to forward it to.
        comment: Text added above the forwarded content.

    Returns:
        True when Graph accepted the forward.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().forward(message_id, to, comment=comment)


@step(retry=_UNSAFE_WRITE)
async def outlook_create_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    body_type: str = "text",
) -> OutlookMessage:
    """Create a draft without sending it.

    The safe half of sending: an agent writes the draft, ``ctx.wait_for_approval()``
    parks the run, and a person sends it from Outlook.

    Not retried: a retry leaves two drafts.

    Args:
        to: Recipient addresses.
        subject: Subject line.
        body: Message body.
        cc: Carbon-copy addresses.
        bcc: Blind carbon-copy addresses.
        body_type: ``"text"`` (default) or ``"html"``.

    Returns:
        The created draft as an OutlookMessage, with a ``web_link`` a person
        can open.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().create_draft(
        to, subject, body, cc=cc, bcc=bcc, body_type=body_type
    )


@step(retry=_UNSAFE_WRITE)
async def outlook_send_draft(message_id: str) -> bool:
    """Send a draft that already exists.

    The other half of ``outlook_create_draft``, and what completes the
    human-approval pattern: an agent writes the draft, ``ctx.wait_for_approval()``
    parks the run, a person reads it, and this sends it.

    Not retried: a retry after a timeout can send the mail twice.

    Args:
        message_id: The draft's id, from ``outlook_create_draft``.

    Returns:
        True meaning Graph **accepted** the send. As with
        ``outlook_send_message``, acceptance is not a delivery confirmation.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().send_draft(message_id)


@step(retry=_IDEMPOTENT_WRITE)
async def outlook_update_message(
    message_id: str,
    is_read: bool | None = None,
    categories: list[str] | None = None,
    importance: str = "",
) -> OutlookMessage:
    """Mark a message read or unread, categorise it, or set its importance.

    Retried once: setting the same flags twice leaves the same message.

    Args:
        message_id: The message to change.
        is_read: True marks it read, False unread. None leaves it alone.
        categories: Replace the message's categories. Pass ``[]`` to clear.
        importance: ``"low"``, ``"normal"``, or ``"high"``.

    Returns:
        The updated OutlookMessage.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().update_message(
        message_id, is_read=is_read, categories=categories, importance=importance
    )


@step(retry=_IDEMPOTENT_WRITE)
async def outlook_move_message(message_id: str, folder_id: str) -> OutlookMessage:
    """Move a message to another folder.

    Retried once: moving a message already in the destination is the same
    request with the same outcome.

    Args:
        message_id: The message to move.
        folder_id: Destination folder id, or a well-known name such as
            ``"archive"`` or ``"deleteditems"``.

    Returns:
        The moved message. **Its id changes** — Outlook reissues an id on move,
        so a caller holding the old one must use the returned message.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().move_message(message_id, folder_id)


@step(retry=_IDEMPOTENT_WRITE)
async def outlook_delete_message(message_id: str) -> bool:
    """Move a message to Deleted Items.

    Recoverable, like Gmail's trash — this is not a permanent delete.

    Retried once: deleting an already-deleted message is a 404, not a second
    deletion.

    Args:
        message_id: The message to delete.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.outlook.mail.client import get_default_client

    return await get_default_client().delete_message(message_id)
