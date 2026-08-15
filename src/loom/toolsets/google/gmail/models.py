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


EmailMessage.model_rebuild()
