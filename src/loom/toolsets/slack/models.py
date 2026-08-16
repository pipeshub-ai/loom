"""Typed response models for the Slack toolset.

Slack returns deeply optional objects — a message may or may not have a user, a
bot id, files, reactions, a thread parent — and the same field means different
things depending on which of those it has. These models flatten that into what
a workflow actually branches on.

One field deserves attention before you write anything: **``ts`` is an
identifier, not a number.** Slack's ``"1718280000.123456"`` is both the
message's timestamp and its primary key — it is what ``chat.update`` and
``chat.delete`` take, and what a thread's replies join on. It is kept as a
string here because parsing it to a float loses precision on the microsecond
component, and a re-serialised ``1718280000.123456`` that comes back as
``1718280000.1234560`` matches no message at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PostedMessage",
    "SlackChannel",
    "SlackFileRef",
    "SlackMessage",
    "SlackUser",
]


class SlackFileRef(BaseModel):
    """A file shared in a message, without its bytes."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    mimetype: str = ""
    size: int = 0
    permalink: str = ""
    url_private: str = ""
    """Downloadable only with the token that can see the file — it is not a
    public URL, and fetching it needs the ``Authorization`` header."""


class SlackMessage(BaseModel):
    """One message in a channel or thread."""

    model_config = ConfigDict(frozen=True)

    ts: str
    """Slack's message id *and* its timestamp. Always a string — see the module
    docstring for why parsing it is a mistake."""
    channel: str = ""
    user: str = ""
    """Author's user id, empty for a message posted by an app."""
    bot_id: str = ""
    """Set instead of ``user`` when an app posted it. Checking this is how a
    workflow avoids replying to its own messages and looping forever."""
    text: str = ""
    thread_ts: str = ""
    """The parent's ``ts`` when this is a threaded reply. Equal to ``ts`` on a
    parent that has replies, which is how Slack marks a thread root."""
    reply_count: int = 0
    subtype: str = ""
    """``channel_join``, ``bot_message``, ``message_changed``, ... Empty for an
    ordinary message. Worth filtering on: a channel's history is full of join
    notices that are not conversation."""
    permalink: str = ""
    files: list[SlackFileRef] = Field(default_factory=list)
    reactions: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.ts

    @property
    def from_app(self) -> bool:
        return bool(self.bot_id)


class SlackChannel(BaseModel):
    """A channel, group DM, or DM — Slack calls them all conversations."""

    model_config = ConfigDict(frozen=True)

    id: str
    """``C...`` for a channel, ``D...`` for a DM, ``G...`` for a private group.
    This is what every other call takes; the name is not."""
    name: str = ""
    is_private: bool = False
    is_archived: bool = False
    is_member: bool = False
    """Whether *this app* is in the channel. Posting to a public channel it has
    not joined fails with ``not_in_channel``, which is the most common Slack
    integration failure there is."""
    is_im: bool = False
    topic: str = ""
    purpose: str = ""
    num_members: int = 0
    created: int = 0


class SlackUser(BaseModel):
    """A workspace member."""

    model_config = ConfigDict(frozen=True)

    id: str
    """``U...``. What a mention (``<@U123>``) and a DM open both take."""
    name: str = ""
    """The handle, without the ``@``."""
    real_name: str = ""
    display_name: str = ""
    email: str = ""
    """Only present with the ``users:read.email`` scope, which is separate from
    ``users:read`` — so this is commonly empty on an otherwise working token."""
    is_bot: bool = False
    is_admin: bool = False
    deleted: bool = False
    """A deactivated account. Still returned by ``users.list``, and *not*
    findable by ``users.lookupByEmail``, which reports it as not found."""
    timezone: str = ""
    title: str = ""


class PostedMessage(BaseModel):
    """The receipt from posting, updating, or scheduling a message."""

    model_config = ConfigDict(frozen=True)

    ts: str
    """The new message's id — what ``slack_update_message`` and
    ``slack_delete_message`` take."""
    channel: str = ""
    text: str = ""
    scheduled_message_id: str = ""
    """Set only by a scheduled post, and it is *not* a ``ts``: a scheduled
    message has no ``ts`` until it is actually sent, and cancelling one takes
    this id instead."""
