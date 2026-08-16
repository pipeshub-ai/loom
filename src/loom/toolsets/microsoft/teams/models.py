"""Typed shapes for Microsoft Teams.

Flattened, as everywhere else in this package: Graph wraps every actor in an
``identitySet`` and every message body in an ``itemBody``, and a model reasoning
about "who said what" should not have to walk two levels to find out.

One field is deliberately *not* flattened away. ``ChatMessage.body_html`` keeps
the original markup beside the extracted text, because a Teams message's HTML
carries mentions (``<at id="0">``), attachment placeholders, and hosted-image
references that a workflow may need to preserve when replying.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from loom.toolsets.microsoft.models import _person

__all__ = ["Channel", "ChatMessage", "Team", "TeamsChat", "TeamsMember"]

#: Matches an HTML tag. Deliberately simple: this is used to render a rough
#: plain-text view beside the original markup, never to sanitise anything.
_TAG = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    """A rough plain-text rendering of a Teams message body.

    Teams sends most message bodies as ``contentType: html`` even when the
    person typed plain text, so a workflow that reads ``content`` directly gets
    ``<div>ok</div>`` where it expected ``ok``. The original stays available on
    ``body_html`` for anything that needs the markup.
    """
    if not html:
        return ""
    text = _TAG.sub("", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return " ".join(text.split())


class Team(BaseModel):
    """A team. In Graph a team is also a group, and shares the group's id."""

    id: str
    display_name: str = ""
    description: str = ""
    web_url: str = ""
    visibility: str = ""
    """``public``, ``private``, or ``hiddenMembership``."""
    is_archived: bool = False
    """An archived team is read-only; posting to it fails."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Team:
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            description=raw.get("description") or "",
            web_url=raw.get("webUrl") or "",
            visibility=raw.get("visibility") or "",
            is_archived=bool(raw.get("isArchived", False)),
        )


class Channel(BaseModel):
    """A channel within a team."""

    id: str
    """Looks like ``19:...@thread.tacv2`` — a colon and an ``@`` inside what is
    used as a URL path segment, which is why the client escapes it."""
    display_name: str = ""
    description: str = ""
    web_url: str = ""
    membership_type: str = ""
    """``standard``, ``private``, or ``shared``. A private channel's messages
    are not visible through the parent team's permissions."""
    email: str = ""
    created: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Channel:
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            description=raw.get("description") or "",
            web_url=raw.get("webUrl") or "",
            membership_type=raw.get("membershipType") or "",
            email=raw.get("email") or "",
            created=raw.get("createdDateTime") or "",
        )


class ChatMessage(BaseModel):
    """One message in a channel or a chat."""

    id: str
    text: str = ""
    """A plain-text rendering. Teams marks most bodies ``html`` even when the
    author typed prose, so reading the raw content yields markup."""
    body_html: str = ""
    """The original body, kept because mentions and attachment placeholders
    live in the markup and are lost by the text rendering."""
    subject: str = ""
    """Channel messages may have one; chat messages never do."""
    from_name: str = ""
    from_id: str = ""
    message_type: str = "message"
    """``message``, or ``systemEventMessage`` for a join/leave/rename record.
    Worth filtering on: a channel's history is full of the latter."""
    created: str = ""
    last_modified: str = ""
    deleted: bool = False
    importance: str = ""
    web_url: str = ""
    reply_to_id: str = ""
    """Set when this message is a reply. Empty for a thread root."""
    channel_id: str = ""
    chat_id: str = ""
    mentions: list[str] = Field(default_factory=list)
    attachment_names: list[str] = Field(default_factory=list)
    reply_count: int = 0
    """Only populated when replies were expanded; 0 otherwise, which is not
    the same as "no replies"."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ChatMessage:
        body = raw.get("body") or {}
        content = str(body.get("content") or "")
        identity = raw.get("from") or {}
        user = identity.get("user") if isinstance(identity, dict) else None
        channel = raw.get("channelIdentity") or {}
        return cls(
            id=str(raw.get("id", "")),
            text=_plain(content) if body.get("contentType") == "html" else content,
            body_html=content,
            subject=raw.get("subject") or "",
            from_name=_person(identity),
            from_id=str((user or {}).get("id", "") or ""),
            message_type=raw.get("messageType") or "message",
            created=raw.get("createdDateTime") or "",
            last_modified=raw.get("lastModifiedDateTime") or "",
            deleted=raw.get("deletedDateTime") is not None,
            importance=raw.get("importance") or "",
            web_url=raw.get("webUrl") or "",
            reply_to_id=str(raw.get("replyToId") or "" or ""),
            channel_id=str(channel.get("channelId") or ""),
            chat_id=str(raw.get("chatId") or "" or ""),
            mentions=[
                str(m.get("mentionText") or "")
                for m in (raw.get("mentions") or [])
                if isinstance(m, dict)
            ],
            attachment_names=[
                str(a.get("name") or "")
                for a in (raw.get("attachments") or [])
                if isinstance(a, dict) and a.get("name")
            ],
            reply_count=int(raw.get("replies@odata.count") or 0),
        )


class TeamsChat(BaseModel):
    """A chat — one-on-one, group, or the chat attached to a meeting."""

    id: str
    topic: str = ""
    """Group chats may be named; one-on-one chats never are, so this is empty
    for most chats and the members are what identify them."""
    chat_type: str = ""
    """``oneOnOne``, ``group``, or ``meeting``."""
    created: str = ""
    last_updated: str = ""
    web_url: str = ""
    member_names: list[str] = Field(default_factory=list)
    """Populated only when members were expanded — and Graph caps that at 25
    however large a page was asked for, so a long group chat's list is short
    with nothing marking it. Use the members operation when it matters."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TeamsChat:
        members = raw.get("members")
        return cls(
            id=str(raw.get("id", "")),
            topic=raw.get("topic") or "",
            chat_type=raw.get("chatType") or "",
            created=raw.get("createdDateTime") or "",
            last_updated=raw.get("lastUpdatedDateTime") or "",
            web_url=raw.get("webUrl") or "",
            member_names=[
                str(m.get("displayName") or "")
                for m in (members or [])
                if isinstance(m, dict)
            ],
        )


class TeamsMember(BaseModel):
    """Somebody's membership of a team or chat."""

    id: str
    """The *membership* id, not the user id. Graph's own note: "The membership
    IDs returned by the server must be treated as opaque strings.\""""
    display_name: str = ""
    email: str = ""
    user_id: str = ""
    """The directory id — this is what a mention or an assignment needs."""
    roles: list[str] = Field(default_factory=list)
    """``owner`` for a team owner; empty for an ordinary member."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TeamsMember:
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            email=raw.get("email") or "",
            user_id=str(raw.get("userId", "") or ""),
            roles=[str(r) for r in (raw.get("roles") or [])],
        )
