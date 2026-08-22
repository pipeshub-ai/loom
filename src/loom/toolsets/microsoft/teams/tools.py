"""Microsoft Teams step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.teams.tools import teams_send_channel_message

    await ctx.step(teams_send_channel_message, team_id=t, channel_id=c,
                   content="Deploy finished.")

**Sending needs delegated credentials.** Graph does not support application
permissions for posting a Teams message — only ``Teamwork.Migrate.All``, and
only for migration. Under an app-only token a send returns 403. Reading works
app-only with the right permissions; set ``MS_TEAMS_USER`` to say whose teams
and chats to read.

**Do not poll.** Graph's Teams documentation is explicit that polling a resource
for changes more than once a day violates the Microsoft APIs Terms of Use and
may result in throttling or suspension. Use change notifications for anything
that needs to react quickly; these tools are for workflows that act on a
trigger, not for a loop that watches a channel.

**Channel messages support almost no query options.** Only paging. There is no
filter or sort argument on those tools because Graph ignores the parameters
rather than rejecting them, and a filter that silently does nothing returns a
wrong answer that looks right.

Retries are per operation. Reads retry; **sending and replying do not**,
because a retry posts the message twice, visibly, to everyone in the channel.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.teams.client import TeamsClient
from loom.toolsets.microsoft.teams.models import (
    Channel,
    ChatMessage,
    Team,
    TeamsChat,
    TeamsMember,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- identity ----------------------------------------------------------------


@step(retry=_READ)
async def teams_whoami() -> MicrosoftUser:
    """Return the person these credentials authenticate as.

    Fails under an app-only token, which has no signed-in user.

    Returns:
        The signed-in user's id, display name, email, and userPrincipalName.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).whoami()


# -- teams and channels ------------------------------------------------------


@step(retry=_READ)
async def teams_list_joined_teams(limit: int = 100) -> Results[Team]:
    """List the teams this user belongs to.

    Resolve a team here before working in it — every other tool takes the id
    this returns, and a team's display name is not addressable.

    Args:
        limit: Maximum teams. Defaults to 100.

    Returns:
        Results of Team. ``is_archived`` matters before posting: an archived
        team is read-only.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_joined_teams(limit=limit)


@step(retry=_READ)
async def teams_get_team(team_id: str) -> Team:
    """Fetch one team by id.

    Args:
        team_id: The team's id, from ``teams_list_joined_teams``.

    Returns:
        Team with display name, description, visibility, and archived state.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).get_team(team_id)


@step(retry=_READ)
async def teams_list_channels(team_id: str, limit: int = 100) -> Results[Channel]:
    """List a team's channels.

    Resolve a channel here before posting: channel ids look like
    ``19:...@thread.tacv2`` and are never something a person types.

    Args:
        team_id: The team to list channels in.
        limit: Maximum channels. Defaults to 100.

    Returns:
        Results of Channel. ``membership_type`` of ``"private"`` means the
        channel's messages are not readable through the parent team's
        permissions.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_channels(team_id, limit=limit)


@step(retry=_READ)
async def teams_get_channel(team_id: str, channel_id: str) -> Channel:
    """Fetch one channel by id.

    Args:
        team_id: The team the channel belongs to.
        channel_id: The channel's id.

    Returns:
        Channel with display name, description, and web URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).get_channel(team_id, channel_id)


@step(retry=_READ)
async def teams_list_team_members(
    team_id: str, limit: int = 100
) -> Results[TeamsMember]:
    """List a team's members.

    Resolve a person here before mentioning or assigning: a mention needs the
    directory ``user_id``, not the display name.

    Args:
        team_id: The team to list members of.
        limit: Maximum members. Defaults to 100.

    Returns:
        Results of TeamsMember. ``roles`` contains ``"owner"`` for owners and
        is empty for ordinary members.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_team_members(team_id, limit=limit)


# -- channel messages --------------------------------------------------------


@step(retry=_READ)
async def teams_list_channel_messages(
    team_id: str, channel_id: str, limit: int = 50
) -> Results[ChatMessage]:
    """List a channel's root messages. Replies are not included.

    Graph orders these by the last activity in the whole reply chain, so a
    message with recent replies appears above newer messages with none.

    There is no filter or sort argument because Graph supports neither here and
    ignores them rather than rejecting them.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel to read.
        limit: Maximum messages across pages. Defaults to 50; Graph caps a
            single page at 50.

    Returns:
        Results of ChatMessage. Filter out ``message_type ==
        "systemEventMessage"`` to skip join/leave/rename records, which make up
        much of a channel's history. Use ``teams_list_message_replies`` for a
        thread.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_channel_messages(
        team_id, channel_id, limit=limit
    )


@step(retry=_READ)
async def teams_get_channel_message(
    team_id: str, channel_id: str, message_id: str
) -> ChatMessage:
    """Fetch one channel message.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel the message is in.
        message_id: The message id.

    Returns:
        ChatMessage with ``text`` (plain) and ``body_html`` (original markup).
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).get_channel_message(
        team_id, channel_id, message_id
    )


@step(retry=_READ)
async def teams_list_message_replies(
    team_id: str, channel_id: str, message_id: str, limit: int = 100
) -> Results[ChatMessage]:
    """List the replies to one channel message.

    A separate call rather than an option on the listing: expanding replies
    inline truncates at 200 behind a nested next-link that is easy to miss, so
    a long thread comes back short with nothing saying so.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel the message is in.
        message_id: The root message whose thread to read.
        limit: Maximum replies across pages. Defaults to 100.

    Returns:
        Results of ChatMessage, each with ``reply_to_id`` set.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_message_replies(
        team_id, channel_id, message_id, limit=limit
    )


@step(retry=_UNSAFE_WRITE)
async def teams_send_channel_message(
    team_id: str,
    channel_id: str,
    content: str,
    content_type: str = "html",
    subject: str = "",
    importance: str = "",
) -> ChatMessage:
    """Post a message to a Teams channel.

    **Needs delegated credentials.** Graph does not support application
    permissions for sending; an app-only token gets a 403.

    Not retried: there is no idempotency key, so a retry after a timeout posts
    the message twice where everyone can see it.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel to post to.
        content: The message. HTML by default — a Teams client renders a
            subset, and plain prose is fine as-is.
        content_type: ``"html"`` (default) or ``"text"``.
        subject: Optional subject line, shown in bold above the message.
        importance: ``"normal"``, ``"high"``, or ``"urgent"``.

    Returns:
        The posted ChatMessage, including its id and web URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).send_channel_message(
        team_id,
        channel_id,
        content,
        content_type=content_type,
        subject=subject,
        importance=importance,
    )


@step(retry=_UNSAFE_WRITE)
async def teams_reply_to_message(
    team_id: str,
    channel_id: str,
    message_id: str,
    content: str,
    content_type: str = "html",
) -> ChatMessage:
    """Reply in the thread of an existing channel message.

    Replying keeps the conversation in one thread; posting a new message
    instead starts a second one beside it, which reads as a duplicate.

    Not retried: a retry posts the reply twice.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel the message is in.
        message_id: The message to reply to.
        content: The reply body.
        content_type: ``"html"`` (default) or ``"text"``.

    Returns:
        The posted reply as a ChatMessage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).reply_to_message(
        team_id, channel_id, message_id, content, content_type=content_type
    )


# -- chats -------------------------------------------------------------------


@step(retry=_READ)
async def teams_list_chats(limit: int = 50, expand_members: bool = False) -> Results[TeamsChat]:
    """List this user's chats, most recently active first.

    Args:
        limit: Maximum chats across pages. Defaults to 50.
        expand_members: Include member names inline. Off by default because
            Graph caps expanded members at 25 with no marker, so a larger group
            chat silently reports 25 — use ``teams_list_chat_members`` instead.

    Returns:
        Results of TeamsChat. ``topic`` is empty for one-on-one chats, so the
        members are what identify those.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_chats(
        limit=limit, expand_members=expand_members
    )


@step(retry=_READ)
async def teams_get_chat(chat_id: str) -> TeamsChat:
    """Fetch one chat by id.

    Args:
        chat_id: The chat's id.

    Returns:
        TeamsChat with topic, type, and timestamps.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).get_chat(chat_id)


@step(retry=_READ)
async def teams_list_chat_messages(chat_id: str, limit: int = 50) -> Results[ChatMessage]:
    """List the messages in a chat, newest first.

    Args:
        chat_id: The chat to read.
        limit: Maximum messages across pages. Defaults to 50.

    Returns:
        Results of ChatMessage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_chat_messages(chat_id, limit=limit)


@step(retry=_READ)
async def teams_list_chat_members(chat_id: str, limit: int = 100) -> Results[TeamsMember]:
    """List a chat's members, following pages properly.

    The reliable way to get a full membership: expanding members on the chat
    listing caps at 25 regardless of page size.

    Args:
        chat_id: The chat to list members of.
        limit: Maximum members. Defaults to 100.

    Returns:
        Results of TeamsMember with directory ``user_id`` and email.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).list_chat_members(chat_id, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def teams_send_chat_message(
    chat_id: str, content: str, content_type: str = "html"
) -> ChatMessage:
    """Send a message to an existing chat.

    **Needs delegated credentials**, as channel sending does.

    Not retried: a retry sends the message twice.

    Args:
        chat_id: The chat to post to.
        content: The message body.
        content_type: ``"html"`` (default) or ``"text"``.

    Returns:
        The sent ChatMessage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("teams", TeamsClient)).send_chat_message(
        chat_id, content, content_type=content_type
    )
