"""Slack steps, for use inside LOOM workflows.

    from loom.toolsets.slack.tools import slack_find_channel, slack_post_message

    channel = await ctx.step(slack_find_channel, "incidents")
    await ctx.step(slack_post_message, channel.id, "Deploy finished.")

**Everything takes an id, never a name.** ``#incidents`` and ``@ada`` are what
people type; ``C024BE91L`` and ``U023BECGF`` are what Slack accepts. Resolve
once at the top of a workflow with ``slack_find_channel`` /
``slack_find_user_by_email``, then pass the id — a name passed where an id
belongs is a ``channel_not_found``, and a *guessed* id posts somewhere else
entirely.

Credentials come from `loom connect slack`, or `$SLACK_BOT_TOKEN`. Importing
this module needs neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.pagination import Results
from loom.toolsets.slack.client import SlackClient
from loom.toolsets.slack.models import (
    PostedMessage,
    SlackChannel,
    SlackFileRef,
    SlackMessage,
    SlackUser,
)

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

__all__ = [
    "SLACK_TOOL_DOCS",
    "slack_add_reaction",
    "slack_archive_channel",
    "slack_create_channel",
    "slack_delete_message",
    "slack_download_file",
    "slack_find_channel",
    "slack_find_user_by_email",
    "slack_get_channel",
    "slack_get_permalink",
    "slack_get_thread",
    "slack_get_user",
    "slack_invite_to_channel",
    "slack_join_channel",
    "slack_list_channel_members",
    "slack_list_channels",
    "slack_list_users",
    "slack_post_ephemeral",
    "slack_post_message",
    "slack_read_channel",
    "slack_reply_in_thread",
    "slack_schedule_message",
    "slack_set_channel_topic",
    "slack_update_message",
    "slack_upload_file",
]

#: Reads are safe to repeat. Slack's permanent failures raise
#: ``NonRetryableError`` subclasses, so this stops on a bad channel id rather
#: than sleeping through three attempts at it.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Posting has no idempotency key. A timeout after Slack accepted the message
#: is indistinguishable from a failure, and a retry posts it twice — visibly,
#: to everyone in the channel. One attempt, and a failure the workflow can
#: decide about with a human. Journaling already prevents a *replay* from
#: reposting.
_POST = Retry(max_attempts=1)

#: Writes a repeat would merely re-apply: a reaction is a set, a topic is a
#: value, joining a channel you are in is a no-op.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def slack_list_channels(
    types: str = "public_channel",
    max_results: int = 200,
    exclude_archived: bool = True,
) -> Results[SlackChannel]:
    """List channels the workspace has.

    Args:
        types: Comma-separated: ``"public_channel"``, ``"private_channel"``,
            ``"mpim"``, ``"im"``. Private channels appear only where this app
            is already a member — that is not an error, and an empty result
            does not mean the workspace has none.
        max_results: Maximum channels to return (default 200).
        exclude_archived: Leave archived channels out (default True).

    Returns:
        Results[SlackChannel] with id, name, is_member, num_members. Check
        ``.complete`` — a large workspace returns a page.
    """
    from loom.toolsets.factory import client_for


    return await (await client_for("slack", SlackClient)).list_channels(
        types=types, max_results=max_results, exclude_archived=exclude_archived
    )


@step(retry=_READ)
async def slack_find_channel(channel_name: str) -> SlackChannel | None:
    """Resolve a channel name to its id — exact match only.

    Call this once, then pass ``channel.id`` everywhere. Slack has no
    lookup-by-name, so this pages the channel list; matching exactly rather
    than by prefix is deliberate, since ``#eng`` would otherwise return
    ``#eng-alerts`` and the workflow would post to the wrong room.

    Args:
        channel_name: Channel name, with or without the leading ``#``.
            Named ``channel_name`` rather than ``name`` because ``ctx.step``
            reserves ``name`` — a parameter called that cannot be passed by
            keyword.

    Returns:
        The SlackChannel, or None when no channel has exactly that name.
        None means not found — create it, or report it; do not guess an id.
        Check ``.is_member``: posting to a public channel this app has not
        joined fails, and ``slack_join_channel`` is the fix.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).find_channel(channel_name)


@step(retry=_READ)
async def slack_get_channel(channel: str) -> SlackChannel:
    """Fetch one channel by id.

    Args:
        channel: Channel id, e.g. ``"C024BE91L"``.

    Returns:
        SlackChannel.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).get_channel(channel)


@step(retry=_READ)
async def slack_read_channel(
    channel: str,
    max_results: int = 100,
    oldest: str = "",
    latest: str = "",
    include_joins: bool = False,
) -> Results[SlackMessage]:
    """Read a channel's recent messages, newest first.

    Args:
        channel: Channel id.
        max_results: Maximum messages to return (default 100).
        oldest: Slack ``ts`` lower bound, e.g. ``"1718280000.000000"``. Derive
            it from ``ctx.now()``, never ``datetime.now()``.
        latest: Slack ``ts`` upper bound.
        include_joins: Keep "X joined the channel" notices, which are dropped
            by default — they are most of a busy channel's history and none of
            its conversation.

    Returns:
        Results[SlackMessage] with ts, user, text, thread_ts, reply_count.
        ``ts`` is the message id that update, delete, and reply all take.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).history(
        channel,
        max_results=max_results,
        oldest=oldest,
        latest=latest,
        include_joins=include_joins,
    )


@step(retry=_READ)
async def slack_get_thread(
    channel: str, thread_ts: str, max_results: int = 100
) -> Results[SlackMessage]:
    """Read every message in one thread, parent first.

    Args:
        channel: Channel id.
        thread_ts: The parent message's ``ts``.
        max_results: Maximum messages to return (default 100).

    Returns:
        Results[SlackMessage].
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).replies(
        channel, thread_ts, max_results=max_results
    )


@step(retry=_READ)
async def slack_list_channel_members(
    channel: str, max_results: int = 500
) -> Results[str]:
    """List the user ids in a channel.

    Args:
        channel: Channel id.
        max_results: Maximum members to return (default 500).

    Returns:
        Results[str] of user ids. Hydrate with ``slack_get_user`` — this
        endpoint returns ids only.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).members(channel, max_results=max_results)


@step(retry=_IDEMPOTENT_WRITE)
async def slack_join_channel(channel: str) -> SlackChannel:
    """Join a public channel.

    The fix for ``not_in_channel``, which is the most common Slack integration
    failure: an app can *see* a public channel it has not joined but cannot
    post to it.

    Args:
        channel: Channel id.

    Returns:
        The SlackChannel, now with ``is_member`` True.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).join_channel(channel)


@step(retry=_POST)
async def slack_create_channel(
    channel_name: str, is_private: bool = False
) -> SlackChannel:
    """Create a channel.

    Not retried: Slack has no idempotency key here, and a retry after a
    timeout answers ``name_taken`` for the channel it just made.

    Args:
        channel_name: Channel name — lowercase, no spaces or dots, up to 80
            chars. Not ``name``, which ``ctx.step`` reserves.
        is_private: Create it private.

    Returns:
        The created SlackChannel.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).create_channel(
        channel_name, is_private=is_private
    )


@step(retry=_IDEMPOTENT_WRITE)
async def slack_invite_to_channel(channel: str, users: list[str]) -> SlackChannel:
    """Invite users to a channel.

    Args:
        channel: Channel id.
        users: User *ids*, not names. Resolve with
            ``slack_find_user_by_email``.

    Returns:
        The updated SlackChannel.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).invite(channel, users)


@step(retry=_IDEMPOTENT_WRITE)
async def slack_set_channel_topic(channel: str, topic: str) -> SlackChannel:
    """Set a channel's topic.

    Args:
        channel: Channel id.
        topic: New topic text.

    Returns:
        The updated SlackChannel.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).set_topic(channel, topic)


@step(retry=_IDEMPOTENT_WRITE)
async def slack_archive_channel(channel: str) -> str:
    """Archive a channel.

    Hides it and stops new messages. Only a workspace admin can unarchive, so
    this is worth a ``ctx.wait_for_approval()`` when an agent chose the id.

    Args:
        channel: Channel id.

    Returns:
        The archived channel id.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).archive_channel(channel)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@step(retry=_POST)
async def slack_post_message(
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    unfurl_links: bool = True,
) -> PostedMessage:
    """Post a message to a channel. Not retried — a retry posts it twice.

    Args:
        channel: Channel id, or a user id to send a DM.
        text: Message text. Slack mrkdwn: ``*bold*``, ``_italic_``,
            ``<@U123>`` to mention someone, ``<#C123>`` for a channel,
            ``<https://x|label>`` for a link.
        blocks: Optional Block Kit payload for rich layout. ``text`` is still
            required alongside it — that is what the push notification says.
        unfurl_links: Expand link previews (default True).

    Returns:
        PostedMessage with ts — the id that update, delete, and reply take.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).post_message(
        channel, text, blocks=blocks, unfurl_links=unfurl_links
    )


@step(retry=_POST)
async def slack_reply_in_thread(
    channel: str, thread_ts: str, text: str, also_send_to_channel: bool = False
) -> PostedMessage:
    """Reply inside a thread. Not retried — see post.

    Args:
        channel: Channel id.
        thread_ts: The parent message's ``ts``.
        text: Reply text.
        also_send_to_channel: Additionally show the reply in the main channel.

    Returns:
        PostedMessage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).post_message(
        channel, text, thread_ts=thread_ts, reply_broadcast=also_send_to_channel
    )


@step(retry=_POST)
async def slack_post_ephemeral(channel: str, user: str, text: str) -> str:
    """Post a message only one person sees. Not retried — see post.

    Leaves nothing in the channel history, so it cannot be updated or deleted
    afterwards.

    Args:
        channel: Channel id.
        user: User id who will see it.
        text: Message text.

    Returns:
        The message_ts. Not a durable id — an ephemeral message has none.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).post_ephemeral(channel, user, text)


@step(retry=_IDEMPOTENT_WRITE)
async def slack_update_message(
    channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> PostedMessage:
    """Edit a message this app posted.

    Args:
        channel: Channel id.
        ts: The message's ``ts``.
        text: Replacement text.
        blocks: Replacement Block Kit payload.

    Returns:
        PostedMessage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).update_message(
        channel, ts, text, blocks=blocks
    )


@step(retry=_IDEMPOTENT_WRITE)
async def slack_delete_message(channel: str, ts: str) -> str:
    """Delete a message. Not recoverable.

    Args:
        channel: Channel id.
        ts: The message's ``ts``.

    Returns:
        The deleted ``ts``, so the journal records what was removed.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).delete_message(channel, ts)


@step(retry=_POST)
async def slack_schedule_message(
    channel: str, text: str, post_at: int, thread_ts: str = ""
) -> PostedMessage:
    """Post a message at a future time. Not retried — see post.

    Args:
        channel: Channel id.
        text: Message text.
        post_at: Unix timestamp, up to 120 days out. Build it from
            ``ctx.now()``, never ``datetime.now()``.
        thread_ts: Post it into this thread.

    Returns:
        PostedMessage with scheduled_message_id — *not* a ``ts``. A scheduled
        message has no ``ts`` until it is actually sent.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).schedule_message(
        channel, text, post_at, thread_ts=thread_ts
    )


@step(retry=_IDEMPOTENT_WRITE)
async def slack_add_reaction(channel: str, ts: str, emoji: str) -> str:
    """React to a message.

    Args:
        channel: Channel id.
        ts: The message's ``ts``.
        emoji: Emoji name without colons, e.g. ``"white_check_mark"``.

    Returns:
        The emoji name.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).add_reaction(channel, ts, emoji)


@step(retry=_READ)
async def slack_get_permalink(channel: str, ts: str) -> str:
    """A stable link to one message — what to put in an email or a ticket.

    Args:
        channel: Channel id.
        ts: The message's ``ts``.

    Returns:
        The permalink URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).permalink(channel, ts)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def slack_list_users(max_results: int = 200) -> Results[SlackUser]:
    """List workspace members.

    Args:
        max_results: Maximum users to return (default 200).

    Returns:
        Results[SlackUser]. Includes deactivated accounts — check ``.deleted``.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).list_users(max_results=max_results)


@step(retry=_READ)
async def slack_get_user(user: str) -> SlackUser:
    """Fetch one user by id.

    Args:
        user: User id, e.g. ``"U023BECGF"``.

    Returns:
        SlackUser. ``email`` is empty unless the app holds the
        ``users:read.email`` scope, which is separate from ``users:read``.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).get_user(user)


@step(retry=_READ)
async def slack_find_user_by_email(email: str) -> SlackUser | None:
    """Resolve an email address to a Slack user id.

    Call this once and pass the id: mentions (``<@U123>``), DMs, and invites
    all take ids, and an email or a display name passed where an id belongs
    silently addresses nobody.

    Needs the ``users:read.email`` scope.

    Args:
        email: The person's email address.

    Returns:
        The SlackUser, or None when nobody matches. Slack reports a
        *deactivated* account the same way, so None does not prove the person
        never existed.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).find_user_by_email(email)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@step(retry=_POST)
async def slack_upload_file(
    channel: str,
    content: bytes | str,
    filename: str,
    title: str = "",
    initial_comment: str = "",
    thread_ts: str = "",
) -> SlackFileRef:
    """Share a file in a channel. Not retried — a retry uploads it twice.

    Args:
        channel: Channel id to share it in.
        content: Bytes, or text which is encoded as UTF-8.
        filename: Filename, including an extension.
        title: Title shown in Slack. Defaults to the filename.
        initial_comment: Message posted alongside the file.
        thread_ts: Share it inside this thread.

    Returns:
        SlackFileRef with id, permalink, url_private.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).upload_file(
        channel,
        content,
        filename,
        title=title,
        initial_comment=initial_comment,
        thread_ts=thread_ts,
    )


@step(retry=_READ)
async def slack_download_file(url_private: str, filename: str) -> Attachment:
    """Download a file shared in Slack, as a LOOM Attachment.

    Args:
        url_private: From ``message.files[i].url_private``. Not a public URL —
            it needs the bot token, which is why a plain fetch of it returns
            an HTML sign-in page rather than the file.
        filename: Name to record on the attachment.

    Returns:
        Attachment with filename, mime, size, and the content.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("slack", SlackClient)).download_file(url_private, filename)


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type[BaseModel]) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Slack Tools

Import: from loom.toolsets.slack.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials: `loom connect slack`, or $SLACK_BOT_TOKEN ("xoxb-...").

### EVERYTHING TAKES AN ID, NEVER A NAME

  channel = await ctx.step(slack_find_channel, "incidents")   # -> C024BE91L
  if channel is None:
      raise ValueError("no #incidents channel")
  await ctx.step(slack_post_message, channel.id, "Deploy finished.")

"#incidents" and "@ada" are what people type; C024BE91L and U023BECGF are what
Slack accepts. Resolve ONCE at the top, then pass ids.

### Channels

slack_list_channels(types="public_channel", max_results=200,
                    exclude_archived=True) -> Results[SlackChannel]
  SlackChannel fields: {fields(SlackChannel)}
  Paged — check .complete before reporting a count.

slack_find_channel(channel_name) -> SlackChannel | None   (exact; None = none)
slack_get_channel(channel) -> SlackChannel
slack_read_channel(channel, max_results=100, oldest="", latest="",
                   include_joins=False) -> Results[SlackMessage]
  SlackMessage fields: {fields(SlackMessage)}
  Join/leave notices are dropped by default.
slack_get_thread(channel, thread_ts, max_results=100) -> Results[SlackMessage]
slack_list_channel_members(channel, max_results=500) -> Results[str]
slack_join_channel(channel) -> SlackChannel
  The fix for "not_in_channel" — an app can SEE a public channel it cannot
  post to. Check channel.is_member first.
slack_create_channel(channel_name, is_private=False) -> SlackChannel
slack_invite_to_channel(channel, users) -> SlackChannel     (user IDS)
slack_set_channel_topic(channel, topic) -> SlackChannel
slack_archive_channel(channel) -> str      (only an admin can undo it)

### Messages

slack_post_message(channel, text, blocks=None, unfurl_links=True)
    -> PostedMessage
  NOT retried — a retry posts twice, visibly, to everyone.
  PostedMessage fields: {fields(PostedMessage)}
  mrkdwn: *bold* _italic_ <@U123> <#C123> <https://x|label>

slack_reply_in_thread(channel, thread_ts, text,
                      also_send_to_channel=False) -> PostedMessage
slack_post_ephemeral(channel, user, text) -> str   (only that user sees it)
slack_update_message(channel, ts, text, blocks=None) -> PostedMessage
slack_delete_message(channel, ts) -> str           (not recoverable)
slack_schedule_message(channel, text, post_at, thread_ts="") -> PostedMessage
  post_at is a Unix timestamp — build it from ctx.now().
slack_add_reaction(channel, ts, emoji) -> str      (name, no colons)
slack_get_permalink(channel, ts) -> str

### Users

slack_list_users(max_results=200) -> Results[SlackUser]
  SlackUser fields: {fields(SlackUser)}
slack_get_user(user) -> SlackUser
slack_find_user_by_email(email) -> SlackUser | None
  THE resolver. Needs the users:read.email scope. None also means
  "deactivated", not only "never existed".

### Files

slack_upload_file(channel, content, filename, title="", initial_comment="",
                  thread_ts="") -> SlackFileRef
  SlackFileRef fields: {fields(SlackFileRef)}
slack_download_file(url_private, filename) -> Attachment
  url_private needs the bot token; a plain fetch returns a sign-in page.

### Notes

- A Slack failure is an HTTP 200 with ok:false. The client raises on it, so
  you never see a silent empty result — but that is why a bad channel id
  raises rather than returning [].
- ts is an ID as well as a timestamp, and is a STRING. Never parse it to a
  float: the precision loss stops it matching any message.
- Posting, replying, scheduling and uploading are not retried. Park on
  ctx.wait_for_approval() before anything a human should see first.
- Reading a busy channel is paged and rate-limited per minute — check
  .complete and use .summary() rather than implying you saw everything.
"""


SLACK_TOOL_DOCS: str = _build_tool_docs()
