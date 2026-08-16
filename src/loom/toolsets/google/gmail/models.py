"""Typed response models for the Gmail toolset.

The Gmail API returns a message as a recursive MIME tree with headers in a list
of name/value pairs and bodies base64url-encoded several levels down. That shape
is faithful to MIME and miserable to write a workflow against — every caller
ends up reimplementing the same walk to answer "who sent this and what does it
say". These models are the flattened form, and the client does the walk once.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AttachmentRef",
    "EmailMessage",
    "EmailThread",
    "GmailDraft",
    "GmailLabel",
    "GmailProfile",
    "MessageRef",
    "SentMessage",
]


class EmailMessage(BaseModel):
    """One message, flattened out of its MIME tree."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    """The ``From`` header, e.g. ``'Ada <ada@example.com>'``."""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    date: str = ""
    """The ``Date`` header, as sent. Unparsed — senders disagree about format."""
    snippet: str = ""
    body: str = ""
    """The ``text/plain`` part where there is one, else the HTML stripped to text."""
    label_ids: list[str] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    """Attachment metadata only. Fetch content with ``gmail_get_attachment``."""
    url: str = ""

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.label_ids


class AttachmentRef(BaseModel):
    """An attachment's identity and size, without its bytes.

    Kept separate from the content so listing a mailbox does not download every
    file in it; ``size`` is here so a caller can decide before paying for it.
    """

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    filename: str
    mime: str = "application/octet-stream"
    size: int = 0


class MessageRef(BaseModel):
    """A message id from a list response, before it has been hydrated."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str = ""


class SentMessage(BaseModel):
    """The receipt from sending or replying."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str = ""
    label_ids: list[str] = Field(default_factory=list)
    url: str = ""


class GmailLabel(BaseModel):
    """A label, system (``INBOX``, ``UNREAD``) or user-created."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    type: str = ""
    """``system`` or ``user``."""
    messages_total: int = 0
    messages_unread: int = 0


class GmailProfile(BaseModel):
    """The authenticated mailbox."""

    model_config = ConfigDict(frozen=True)

    email_address: str
    messages_total: int = 0
    threads_total: int = 0


class EmailThread(BaseModel):
    """A conversation — every message sharing one thread id.

    The unit a human sees in the inbox, and the right unit for triage: a
    message-level workflow that labels one reply leaves the rest of the
    conversation unlabelled, which in the Gmail UI looks like nothing happened.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    snippet: str = ""
    messages: list[EmailMessage] = Field(default_factory=list)
    """Oldest first, as Gmail returns them. Empty in a *list* response, which
    carries ids and snippets only — fetch the thread to get its messages."""
    message_count: int = 0
    """Set even when ``messages`` is empty, so a list response can still say
    how long the conversation is."""
    label_ids: list[str] = Field(default_factory=list)
    """The union across every message, which is what Gmail shows on the
    conversation. A thread is unread if any one message in it is."""

    @property
    def subject(self) -> str:
        """The first message's subject — the conversation's title."""
        return self.messages[0].subject if self.messages else ""

    @property
    def latest(self) -> EmailMessage | None:
        """The most recent message, which is what a reply responds to."""
        return self.messages[-1] if self.messages else None


class GmailDraft(BaseModel):
    """An unsent message.

    Kept distinct from :class:`SentMessage` because the difference is the whole
    point: a draft has an id and has been delivered to nobody. Creating one is
    the safe half of sending, which is what makes a draft-then-approve workflow
    possible.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    message_id: str = ""
    """Id of the message the draft holds. Changes each time the draft is
    edited; the draft id does not."""
    thread_id: str = ""
    subject: str = ""
    to: list[str] = Field(default_factory=list)
    snippet: str = ""


EmailMessage.model_rebuild()
EmailThread.model_rebuild()
