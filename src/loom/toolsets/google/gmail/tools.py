"""Gmail steps, for use inside LOOM workflows.

    from loom.toolsets.google.gmail.tools import gmail_search_messages

    unread = await ctx.step(gmail_search_messages, "is:unread newer_than:1d", 10)

Credentials come from the environment on first call — see
``loom.toolsets.google.auth``. Importing this module needs none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loom import Retry, step
from loom.toolsets.google.gmail.models import (
    AttachmentRef,
    EmailMessage,
    GmailLabel,
    GmailProfile,
    SentMessage,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

__all__ = [
    "GMAIL_TOOL_DOCS",
    "gmail_archive_message",
    "gmail_get_attachment",
    "gmail_get_message",
    "gmail_get_profile",
    "gmail_list_labels",
    "gmail_mark_read",
    "gmail_modify_labels",
    "gmail_reply_to_message",
    "gmail_search_messages",
    "gmail_send_message",
    "gmail_trash_message",
]

#: Reads are safe to repeat, so they get ordinary backoff. Google's 4xx errors
#: are raised as ``NonRetryableError`` subclasses, so this policy stops on a bad
#: query instead of sleeping through three attempts at it.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Writes that a repeat would merely re-apply — labels are a set, so adding
#: ``STARRED`` twice is indistinguishable from adding it once.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)

#: Sending is not idempotent and Gmail offers no idempotency key. If the request
#: times out *after* delivery, an automatic retry sends the mail twice and there
#: is no way to tell from the client side which happened. So: one attempt, and a
#: failure that surfaces to the workflow, which can decide with a human in the
#: loop. Journaling already prevents a *replay* from re-sending.
_SEND = Retry(max_attempts=1)


@step(retry=_READ)
async def gmail_search_messages(
    query: str = "",
    max_results: int = 20,
) -> Results[EmailMessage]:
    """Search the mailbox with Gmail search syntax.

    Costs one request per hit on top of the search, so keep max_results small.

    Args:
        query: Gmail search query, e.g. ``"is:unread from:boss@corp.com"``,
            ``"has:attachment newer_than:7d"``, ``"subject:invoice"``.
        max_results: Maximum messages to return (default 20).

    Returns:
        List of EmailMessage with id, subject, sender, to, date, body,
        label_ids, attachments, url.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().search_messages(query, max_results)


@step(retry=_READ)
async def gmail_get_message(message_id: str) -> EmailMessage:
    """Fetch one message by id, with its body and attachment metadata.

    Args:
        message_id: Gmail message id, as returned by a search.

    Returns:
        EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().get_message(message_id)


@step(retry=_SEND)
async def gmail_send_message(
    to: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    html: bool = False,
) -> SentMessage:
    """Send an email. Not retried automatically — a retry can double-send.

    Args:
        to: Recipient address, or a list of them.
        subject: Subject line.
        body: Message body, plain text unless ``html`` is true.
        cc: Optional carbon-copy recipients.
        bcc: Optional blind carbon-copy recipients.
        html: Send ``body`` as HTML with a plain-text alternative.

    Returns:
        SentMessage with id, thread_id, and url.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().send_message(
        to, subject, body, cc=cc, bcc=bcc, html=html
    )


@step(retry=_SEND)
async def gmail_reply_to_message(
    message_id: str,
    body: str,
    reply_all: bool = False,
    html: bool = False,
) -> SentMessage:
    """Reply to a message in its own thread. Not retried — see send.

    Sets In-Reply-To and References as well as the thread, so the reply threads
    correctly in clients other than Gmail.

    Args:
        message_id: The message being replied to.
        body: Reply body.
        reply_all: Include the original's To and Cc recipients.
        html: Send ``body`` as HTML.

    Returns:
        SentMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().reply_to_message(
        message_id, body, html=html, reply_all=reply_all
    )


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_modify_labels(
    message_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> EmailMessage:
    """Add and/or remove labels on a message.

    Labels are how Gmail models state: ``UNREAD``, ``STARRED``, ``IMPORTANT``,
    ``INBOX`` (removing it archives), ``SPAM``, plus any user label id from
    ``gmail_list_labels``.

    Args:
        message_id: Gmail message id.
        add: Label ids to apply.
        remove: Label ids to strip.

    Returns:
        The updated EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().modify_labels(message_id, add, remove)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_mark_read(message_id: str) -> EmailMessage:
    """Mark a message read, by removing the UNREAD label.

    Args:
        message_id: Gmail message id.

    Returns:
        The updated EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().modify_labels(message_id, None, ["UNREAD"])


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_archive_message(message_id: str) -> EmailMessage:
    """Archive a message, by removing the INBOX label.

    Args:
        message_id: Gmail message id.

    Returns:
        The updated EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().modify_labels(message_id, None, ["INBOX"])


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_trash_message(message_id: str) -> EmailMessage:
    """Move a message to the trash, where it is recoverable for 30 days.

    Args:
        message_id: Gmail message id.

    Returns:
        The trashed EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().trash_message(message_id)


@step(retry=_READ)
async def gmail_get_attachment(
    message_id: str,
    attachment_id: str,
    filename: str = "attachment",
) -> Attachment:
    """Download an attachment as a LOOM Attachment.

    The ids and filename come from ``message.attachments``. With a BlobService
    on the Runtime, a large payload offloads out of the journal automatically.

    Args:
        message_id: The message the attachment belongs to.
        attachment_id: From ``AttachmentRef.attachment_id``.
        filename: Name to record on the attachment.

    Returns:
        Attachment with filename, mime, size, sha256, and bytes.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().get_attachment(
        message_id, attachment_id, filename
    )


@step(retry=_READ)
async def gmail_list_labels() -> list[GmailLabel]:
    """List every label, system and user-created.

    Returns:
        List of GmailLabel with id, name, type, messages_total,
        messages_unread.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().list_labels()


@step(retry=_READ)
async def gmail_get_profile() -> GmailProfile:
    """Get the authenticated mailbox's profile.

    Returns:
        GmailProfile with email_address, messages_total, threads_total.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().get_profile()


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Gmail Tools

Import: from loom.toolsets.google.gmail.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  GOOGLE_ACCESS_TOKEN, or
  GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

All tools return typed Pydantic models. Use attribute access:
msg.subject, msg.sender, msg.body.

### Tools

gmail_search_messages(query: str = "", max_results: int = 20)
    -> list[EmailMessage]
  Gmail search syntax. EmailMessage fields: {fields(EmailMessage)}
    unread = await ctx.step(gmail_search_messages, "is:unread", 10)
    recent = await ctx.step(gmail_search_messages,
                            "from:alerts@corp.com newer_than:2d")
    files  = await ctx.step(gmail_search_messages, "has:attachment", 5)

gmail_get_message(message_id: str) -> EmailMessage
    msg = await ctx.step(gmail_get_message, "18c…")
    print(msg.subject, msg.sender, msg.body)

gmail_send_message(to, subject, body, cc=None, bcc=None, html=False)
    -> SentMessage
  NOT retried automatically — a retry could send the mail twice.
  SentMessage fields: {fields(SentMessage)}
    sent = await ctx.step(gmail_send_message, "a@b.com", "Hi", "Body text")
    sent = await ctx.step(gmail_send_message, ["a@b.com", "c@d.com"],
                          "Report", "<h1>Done</h1>", None, None, True)

gmail_reply_to_message(message_id, body, reply_all=False, html=False)
    -> SentMessage
  Threads correctly (In-Reply-To + References). NOT retried.
    await ctx.step(gmail_reply_to_message, msg.id, "Thanks — on it.")

gmail_modify_labels(message_id, add=None, remove=None) -> EmailMessage
  Labels model state: UNREAD, STARRED, IMPORTANT, INBOX, SPAM, user ids.
    await ctx.step(gmail_modify_labels, msg.id, ["STARRED"], ["UNREAD"])

gmail_mark_read(message_id) -> EmailMessage
gmail_archive_message(message_id) -> EmailMessage
gmail_trash_message(message_id) -> EmailMessage
  Convenience over modify_labels; trash is recoverable for 30 days.

gmail_get_attachment(message_id, attachment_id, filename="attachment")
    -> Attachment
  Ids come from message.attachments. AttachmentRef fields:
  {fields(AttachmentRef)}
    for ref in msg.attachments:
        att = await ctx.step(gmail_get_attachment, msg.id,
                             ref.attachment_id, ref.filename)

gmail_list_labels() -> list[GmailLabel]
  GmailLabel fields: {fields(GmailLabel)}

gmail_get_profile() -> GmailProfile
  GmailProfile fields: {fields(GmailProfile)}

### Notes

- Search relative to *now* with Gmail's own operators (newer_than:1d), not a
  computed timestamp — a workflow body must not call datetime.now(). If you
  need an explicit instant, take it from ctx.now().
- Sending is the one operation that cannot be undone. Park on
  ctx.wait_for_approval() before sending anything a human should see first.
"""


GMAIL_TOOL_DOCS: str = _build_tool_docs()
