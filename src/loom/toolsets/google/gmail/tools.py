"""Gmail steps, for use inside LOOM workflows.

    from loom.toolsets.google.gmail.tools import gmail_search_messages

    unread = await ctx.step(gmail_search_messages, "is:unread newer_than:1d", 10)

Credentials come from the environment on first call — see
``loom.toolsets.google.auth``. Importing this module needs none.
"""

from __future__ import annotations

from pydantic import BaseModel

from loom import Retry, step
from loom.blobs.attachment import Attachment
from loom.toolsets.google.gmail.models import (
    AttachmentRef,
    EmailMessage,
    EmailThread,
    GmailDraft,
    GmailLabel,
    GmailProfile,
    SentMessage,
)
from loom.toolsets.pagination import Results

# Imported at runtime, not under TYPE_CHECKING: `gmail_send_message` annotates
# a parameter with it, and `build_parameter_schema` builds a pydantic model
# from that signature. Under TYPE_CHECKING the name does not exist when
# pydantic resolves the annotation, so the model could not be built at all and
# `resolve_tools(["gmail"])` raised — an agent handed this toolset failed
# before its first turn.

__all__ = [
    "GMAIL_TOOL_DOCS",
    "gmail_archive_message",
    "gmail_batch_modify_labels",
    "gmail_create_draft",
    "gmail_create_label",
    "gmail_delete_draft",
    "gmail_delete_label",
    "gmail_find_label",
    "gmail_forward_message",
    "gmail_get_attachment",
    "gmail_get_message",
    "gmail_get_profile",
    "gmail_get_thread",
    "gmail_list_drafts",
    "gmail_list_labels",
    "gmail_list_threads",
    "gmail_mark_read",
    "gmail_modify_labels",
    "gmail_modify_thread_labels",
    "gmail_rename_label",
    "gmail_reply_to_message",
    "gmail_search_messages",
    "gmail_send_draft",
    "gmail_send_message",
    "gmail_trash_message",
    "gmail_trash_thread",
    "gmail_untrash_message",
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

#: Creating a label has no idempotency key. A retry after a timeout that
#: the service actually accepted leaves two labels with the same name.
_CREATE = Retry(max_attempts=1)


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
    attachments: list[Attachment] | None = None,
) -> SentMessage:
    """Send an email. Not retried automatically — a retry can double-send.

    Args:
        to: Recipient address, or a list of them.
        subject: Subject line.
        body: Message body, plain text unless ``html`` is true.
        cc: Optional carbon-copy recipients.
        bcc: Optional blind carbon-copy recipients.
        html: Send ``body`` as HTML with a plain-text alternative.
        attachments: LOOM Attachments to attach. They must hold their bytes
            inline — an offloaded one raises rather than sending an empty file.

    Returns:
        SentMessage with id, thread_id, and url.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().send_message(
        to, subject, body, cc=cc, bcc=bcc, html=html, attachments=attachments
    )


@step(retry=_SEND)
async def gmail_forward_message(
    message_id: str,
    to: list[str] | str,
    comment: str = "",
    html: bool = False,
) -> SentMessage:
    """Forward a message, quoting the original. Not retried — see send.

    Sends a new message rather than adding a recipient to the thread, which
    would silently deliver the whole prior conversation to someone who was
    never part of it.

    Args:
        message_id: The message to forward.
        to: Recipient address, or a list of them.
        comment: Note placed above the quoted original.
        html: Send as HTML.

    Returns:
        SentMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().forward_message(
        message_id, to, comment=comment, html=html
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
async def gmail_batch_modify_labels(
    message_ids: list[str],
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> int:
    """Label many messages in one request.

    Gmail's quota is charged per request, so archiving 200 messages this way is
    one unit rather than 200 — the difference between a triage workflow that
    runs and one that exhausts its per-minute budget.

    Args:
        message_ids: Gmail message ids, up to 1000 per call.
        add: Label ids to apply to all of them.
        remove: Label ids to strip from all of them.

    Returns:
        How many messages were modified. Gmail answers with no body and no
        per-id outcome, so this is the count asked for, not a confirmation
        that each one existed.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().batch_modify_labels(message_ids, add, remove)


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


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_untrash_message(message_id: str) -> EmailMessage:
    """Take a message back out of the trash.

    Args:
        message_id: Gmail message id.

    Returns:
        The restored EmailMessage.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().untrash_message(message_id)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def gmail_list_threads(
    query: str = "",
    max_results: int = 20,
    label_ids: list[str] | None = None,
) -> Results[EmailThread]:
    """Search conversations — ids and snippets only, one request per page.

    Much cheaper than ``gmail_search_messages``, which costs one request per
    hit. Triage on these, then fetch the ones that matter.

    Args:
        query: Gmail search query, e.g. ``"is:unread newer_than:2d"``.
        max_results: Maximum threads to return (default 20).
        label_ids: Restrict to threads carrying all of these labels.

    Returns:
        Results[EmailThread] with id and snippet. ``messages`` is empty and
        ``message_count`` is 0 — a list response carries neither. Call
        ``gmail_get_thread`` for the conversation itself.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().list_threads(query, max_results, label_ids)


@step(retry=_READ)
async def gmail_get_thread(thread_id: str) -> EmailThread:
    """Fetch a whole conversation, every message flattened.

    Args:
        thread_id: Gmail thread id, from a message's ``thread_id`` or a
            thread listing.

    Returns:
        EmailThread with messages oldest-first. ``.subject`` is the first
        message's, ``.latest`` is the one a reply would answer.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().get_thread(thread_id)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_modify_thread_labels(
    thread_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> EmailThread:
    """Label an entire conversation.

    Usually the right unit for triage: Gmail's UI groups by thread, so
    labelling one message of a conversation looks like nothing happened.

    Args:
        thread_id: Gmail thread id.
        add: Label ids to apply.
        remove: Label ids to strip. Removing ``INBOX`` archives the thread.

    Returns:
        The updated EmailThread.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().modify_thread_labels(thread_id, add, remove)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_trash_thread(thread_id: str) -> EmailThread:
    """Move a whole conversation to the trash. Recoverable for 30 days.

    Args:
        thread_id: Gmail thread id.

    Returns:
        The trashed EmailThread.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().trash_thread(thread_id)


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_create_draft(
    to: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    html: bool = False,
    thread_id: str = "",
    attachments: list[Attachment] | None = None,
) -> GmailDraft:
    """Compose an email without sending it.

    The safe half of sending, and the basis of a draft-then-approve workflow:
    an agent writes the mail, ``ctx.wait_for_approval()`` parks the run, and a
    human calls ``gmail_send_draft`` — or does not. Retried, unlike sending,
    because a duplicate draft delivers nothing to anyone.

    Args:
        to: Recipient address, or a list of them.
        subject: Subject line.
        body: Message body, plain text unless ``html`` is true.
        cc: Optional carbon-copy recipients.
        bcc: Optional blind carbon-copy recipients.
        html: Compose ``body`` as HTML.
        thread_id: Make the draft a reply within this conversation.
        attachments: LOOM Attachments holding their bytes inline.

    Returns:
        GmailDraft with the id that ``gmail_send_draft`` takes.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().create_draft(
        to,
        subject,
        body,
        cc=cc,
        bcc=bcc,
        html=html,
        thread_id=thread_id,
        attachments=attachments,
    )


@step(retry=_READ)
async def gmail_list_drafts(max_results: int = 20) -> Results[GmailDraft]:
    """List unsent drafts.

    Args:
        max_results: Maximum drafts to return (default 20).

    Returns:
        Results[GmailDraft] with id and thread_id. Subject and recipients are
        empty — a draft listing carries neither.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().list_drafts(max_results)


@step(retry=_SEND)
async def gmail_send_draft(draft_id: str) -> SentMessage:
    """Send an existing draft. Not retried — a retry can double-send.

    Args:
        draft_id: Draft id from ``gmail_create_draft``.

    Returns:
        SentMessage with id, thread_id, and url.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().send_draft(draft_id)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_delete_draft(draft_id: str) -> str:
    """Discard a draft. It was never delivered, so nothing is recalled.

    Args:
        draft_id: Draft id.

    Returns:
        The discarded draft id.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    await get_default_client().delete_draft(draft_id)
    return draft_id


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
async def gmail_find_label(label_name: str) -> GmailLabel | None:
    """Resolve a label name to the label id that labelling actually takes.

    ``gmail_modify_labels`` takes ids like ``"Label_7"``. Passing the *name* a
    person used is not an error — Gmail accepts the call and applies nothing —
    so resolve once here and pass ``label.id``.

    Args:
        label_name: Exact label name. Use the full path for a nested label,
            e.g. ``"Clients/Acme"``. Named ``label_name`` because ``ctx.step``
            reserves ``name``.

    Returns:
        The GmailLabel, or None when no label has that name. None means create
        it with ``gmail_create_label``, or report it — do not guess an id.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().find_label(label_name)


@step(retry=_CREATE)
async def gmail_create_label(name: str) -> GmailLabel:
    """Create a user label.

    Args:
        name: Label name. A ``/`` nests it — ``"Clients/Acme"`` puts Acme under
            Clients, and the parent label must already exist.

    Returns:
        The created GmailLabel, including the id every labelling tool takes.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().create_label(name)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_rename_label(label_id: str, name: str) -> GmailLabel:
    """Rename a user label. System labels cannot be renamed.

    Args:
        label_id: Label id from ``gmail_list_labels``.
        name: New name.

    Returns:
        The updated GmailLabel.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    return await get_default_client().update_label(label_id, name)


@step(retry=_IDEMPOTENT_WRITE)
async def gmail_delete_label(label_id: str) -> str:
    """Delete a user label, removing it from every message that carried it.

    The messages survive — this deletes the label, not the mail.

    Args:
        label_id: Label id from ``gmail_list_labels``.

    Returns:
        The deleted label id.
    """
    from loom.toolsets.google.gmail.client import get_default_client

    await get_default_client().delete_label(label_id)
    return label_id


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
    def fields(model: type[BaseModel]) -> str:
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

### Messages

gmail_search_messages(query="", max_results=20) -> Results[EmailMessage]
  Gmail search syntax. Costs one request PER HIT — keep max_results small,
  or triage with gmail_list_threads first.
  EmailMessage fields: {fields(EmailMessage)}
    unread = await ctx.step(gmail_search_messages, "is:unread", 10)
    recent = await ctx.step(gmail_search_messages,
                            "from:alerts@corp.com newer_than:2d")
    files  = await ctx.step(gmail_search_messages, "has:attachment", 5)

gmail_get_message(message_id) -> EmailMessage
    msg = await ctx.step(gmail_get_message, "18c…")
    print(msg.subject, msg.sender, msg.body)

gmail_send_message(to, subject, body, cc=None, bcc=None, html=False,
                   attachments=None) -> SentMessage
  NOT retried automatically — a retry could send the mail twice.
  SentMessage fields: {fields(SentMessage)}
    sent = await ctx.step(gmail_send_message, "a@b.com", "Hi", "Body text")
    sent = await ctx.step(gmail_send_message, ["a@b.com", "c@d.com"],
                          "Report", "<h1>Done</h1>", None, None, True)
  attachments take LOOM Attachments holding bytes inline (not offloaded).

gmail_reply_to_message(message_id, body, reply_all=False, html=False)
    -> SentMessage
  Threads correctly (In-Reply-To + References). NOT retried.
    await ctx.step(gmail_reply_to_message, msg.id, "Thanks — on it.")

gmail_forward_message(message_id, to, comment="", html=False) -> SentMessage
  Sends a NEW message quoting the original. NOT retried.

gmail_modify_labels(message_id, add=None, remove=None) -> EmailMessage
  Labels model state: UNREAD, STARRED, IMPORTANT, INBOX, SPAM, user ids.
    await ctx.step(gmail_modify_labels, msg.id, ["STARRED"], ["UNREAD"])

gmail_batch_modify_labels(message_ids, add=None, remove=None) -> int
  One request for up to 1000 messages. Gmail charges quota per REQUEST, so
  prefer this over a loop when handling more than a handful.

gmail_mark_read(message_id) -> EmailMessage
gmail_archive_message(message_id) -> EmailMessage
gmail_trash_message(message_id) -> EmailMessage      (recoverable 30 days)
gmail_untrash_message(message_id) -> EmailMessage
  Permanent delete is deliberately NOT exposed: it needs Google's
  restricted full-mailbox scope, and trash already means "delete".

gmail_get_attachment(message_id, attachment_id, filename="attachment")
    -> Attachment
  Ids come from message.attachments. AttachmentRef fields:
  {fields(AttachmentRef)}
    for ref in msg.attachments:
        att = await ctx.step(gmail_get_attachment, msg.id,
                             ref.attachment_id, ref.filename)

### Threads (conversations)

gmail_list_threads(query="", max_results=20, label_ids=None)
    -> Results[EmailThread]
  ONE request per page, not one per hit — the cheap way to triage an inbox.
  Returns ids and snippets only; messages is empty until you fetch the thread.
  EmailThread fields: {fields(EmailThread)}

gmail_get_thread(thread_id) -> EmailThread
  thread.subject is the first message's; thread.latest is the newest.

gmail_modify_thread_labels(thread_id, add=None, remove=None) -> EmailThread
  Usually the RIGHT unit for triage — Gmail's UI groups by thread, so
  labelling one message of a conversation looks like nothing happened.

gmail_trash_thread(thread_id) -> EmailThread

### Drafts

gmail_create_draft(to, subject, body, cc=None, bcc=None, html=False,
                   thread_id="", attachments=None) -> GmailDraft
  Composes WITHOUT sending. Retried, unlike sending — a duplicate draft
  reaches nobody. GmailDraft fields: {fields(GmailDraft)}
    draft = await ctx.step(gmail_create_draft, "a@b.com", "Offer", text)
    await ctx.wait_for_approval("send-offer")
    await ctx.step(gmail_send_draft, draft.id)

gmail_list_drafts(max_results=20) -> Results[GmailDraft]
gmail_send_draft(draft_id) -> SentMessage       (NOT retried)
gmail_delete_draft(draft_id) -> str

### Labels and profile

gmail_list_labels() -> list[GmailLabel]
  GmailLabel fields: {fields(GmailLabel)}
gmail_find_label(label_name) -> GmailLabel | None
  THE label resolver. modify_labels takes IDS ("Label_7"); passing the
  name a person used applies nothing and reports no error.
gmail_create_label(name) -> GmailLabel      ("Clients/Acme" nests it)
gmail_rename_label(label_id, name) -> GmailLabel
gmail_delete_label(label_id) -> str         (the label, not the messages)

gmail_get_profile() -> GmailProfile
  GmailProfile fields: {fields(GmailProfile)}

### Notes

- Search relative to *now* with Gmail's own operators (newer_than:1d), not a
  computed timestamp — a workflow body must not call datetime.now(). If you
  need an explicit instant, take it from ctx.now().
- The search and list tools are paged and return Results — check .complete
  before reporting a count, and use .summary() to phrase it honestly.
- Sending is the one operation that cannot be undone. Park on
  ctx.wait_for_approval() before sending anything a human should see first,
  or write a draft and let the human send it.
"""


GMAIL_TOOL_DOCS: str = _build_tool_docs()
