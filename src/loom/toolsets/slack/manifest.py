"""Slack toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.slack.models import (
    PostedMessage,
    SlackChannel,
    SlackFileRef,
    SlackMessage,
    SlackUser,
)

__all__ = ["SLACK_MANIFEST"]

_message = SlackMessage.model_json_schema()
_channel = SlackChannel.model_json_schema()
_user = SlackUser.model_json_schema()
_posted = PostedMessage.model_json_schema()
_channel_id = {
    "type": "string",
    "description": 'Channel id such as "C024BE91L" — never a name.',
}
_ts = {
    "type": "string",
    "description": 'Message id, e.g. "1718280000.123456". A string, not a number.',
}

SLACK_MANIFEST = ToolsetManifest(
    id="slack",
    version="1.0.0",
    provider="loom",
    summary=(
        "Slack — read and post messages, work with threads, manage channels, "
        "resolve people, and share files."
    ),
    description=(
        "Slack Web API over REST. Reads channel history and threads, posts and "
        "edits messages (including Block Kit and scheduled sends), manages "
        "channels and membership, resolves an email address to the user id "
        "every mention and DM requires, and shares files. Every operation "
        "takes an id rather than a name: '#incidents' is what people type and "
        "'C024BE91L' is what Slack accepts, so resolve once with "
        "conversations.find and pass the id. Posting is deliberately not "
        "retried — Slack offers no idempotency key, so a retry after a "
        "post-delivery timeout posts the message twice, visibly."
    ),
    base_url="https://slack.com/api",
    auth=AuthSpec(
        client="loom.toolsets.slack.client:SlackClient",
        kind="oauth2",
        credential="slack",
        provider="slack",
        fields=(
            AuthField(name="SLACK_BOT_TOKEN", arg="token", label="Bot user OAuth token",
                      example="xoxb-…"),
            AuthField(name="SLACK_TOKEN", arg="token", label="OAuth token", required=False),
        ),
        setup_url="https://api.slack.com/apps",
        docs_url="https://api.slack.com/authentication/oauth-v2",
    ),
    tools_module="loom.toolsets.slack.tools",
    opaque_ids={
        # C024BE91L, U023BECGF — everything in Slack takes one of these and
        # never the #name a person types.
        #
        # The lookahead demanding a digit is not decoration. Without it
        # `C[A-Z0-9]{7,}` matches CANCELLED, COMPLETED, CRITICAL and
        # UNASSIGNED — ordinary constants in generated code — and a check that
        # flags ordinary data is one people switch off. The cost is a Slack id
        # of letters alone going unchecked, which is the right way round: a
        # missed guess is what this was before, a false alarm is worse.
        r"\bC(?=[A-Z0-9]*\d)[A-Z0-9]{7,}\b": "channel",
        r"\bU(?=[A-Z0-9]*\d)[A-Z0-9]{7,}\b": "user",
    },
    egress_hosts=["slack.com", "files.slack.com"],
    rate_limits={
        "tiers": "per method, per workspace, per minute",
        "chat.postMessage": "~1 per second per channel",
    },
    groups={
        "conversations": [
            OperationSpec(
                id="conversations.list",
                function="slack_list_channels",
                summary="List channels in the workspace.",
                description=(
                    "Private channels appear only where this app is already a "
                    "member; an empty result does not mean there are none."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "types": {"type": "string", "default": "public_channel"},
                        "max_results": {"type": "integer", "default": 200},
                        "exclude_archived": {"type": "boolean", "default": True},
                    },
                },
                output_schema={"type": "array", "items": _channel},
                scopes=["channels:read", "groups:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.find",
                function="slack_find_channel",
                summary="Resolve a channel name to its id — exact match.",
                description=(
                    "Call once, then pass the id. A name where an id belongs "
                    "is channel_not_found; a prefix match would silently post "
                    "to the wrong channel."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"channel_name": {"type": "string"}},
                    "required": ["channel_name"],
                },
                output_schema=_channel,
                scopes=["channels:read", "groups:read"],
                idempotent=True,
                resolves="channel",
            ),
            OperationSpec(
                id="conversations.info",
                function="slack_get_channel",
                summary="Fetch one channel by id.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"channel": _channel_id},
                    "required": ["channel"],
                },
                output_schema=_channel,
                scopes=["channels:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.history",
                function="slack_read_channel",
                summary="Read a channel's recent messages, newest first.",
                description="Join and leave notices are dropped by default.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "max_results": {"type": "integer", "default": 100},
                        "oldest": {"type": "string"},
                        "latest": {"type": "string"},
                        "include_joins": {"type": "boolean", "default": False},
                    },
                    "required": ["channel"],
                },
                output_schema={"type": "array", "items": _message},
                scopes=["channels:history", "groups:history"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.replies",
                function="slack_get_thread",
                summary="Read every message in one thread.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "thread_ts": _ts,
                        "max_results": {"type": "integer", "default": 100},
                    },
                    "required": ["channel", "thread_ts"],
                },
                output_schema={"type": "array", "items": _message},
                scopes=["channels:history"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.members",
                function="slack_list_channel_members",
                summary="List the user ids in a channel.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "max_results": {"type": "integer", "default": 500},
                    },
                    "required": ["channel"],
                },
                output_schema={"type": "array", "items": {"type": "string"}},
                scopes=["channels:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.join",
                function="slack_join_channel",
                summary="Join a public channel.",
                description=(
                    "The fix for not_in_channel — an app can see a public "
                    "channel it cannot post to."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {"channel": _channel_id},
                    "required": ["channel"],
                },
                output_schema=_channel,
                scopes=["channels:join"],
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.create",
                function="slack_create_channel",
                summary="Create a channel.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel_name": {"type": "string"},
                        "is_private": {"type": "boolean", "default": False},
                    },
                    "required": ["channel_name"],
                },
                output_schema=_channel,
                scopes=["channels:manage", "groups:write"],
            ),
            OperationSpec(
                id="conversations.invite",
                access_control=True,
                function="slack_invite_to_channel",
                summary="Invite users to a channel. Takes user ids.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "users": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["channel", "users"],
                },
                output_schema=_channel,
                scopes=["channels:manage"],
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.set_topic",
                function="slack_set_channel_topic",
                summary="Set a channel's topic.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "topic": {"type": "string"},
                    },
                    "required": ["channel", "topic"],
                },
                output_schema=_channel,
                scopes=["channels:manage"],
                idempotent=True,
            ),
            OperationSpec(
                id="conversations.archive",
                idempotent=True,
                function="slack_archive_channel",
                summary="Archive a channel. Only an admin can undo it.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"channel": _channel_id},
                    "required": ["channel"],
                },
                output_schema={"type": "string"},
                scopes=["channels:manage"],
            ),
        ],
        "messages": [
            OperationSpec(
                id="messages.post",
                function="slack_post_message",
                summary="Post a message to a channel or a DM.",
                description=(
                    "Not idempotent and not automatically retried — Slack "
                    "offers no idempotency key, so a retry posts twice."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "text": {"type": "string"},
                        "blocks": {"type": "array", "items": {"type": "object"}},
                        "unfurl_links": {"type": "boolean", "default": True},
                    },
                    "required": ["channel", "text"],
                },
                output_schema=_posted,
                scopes=["chat:write"],
            ),
            OperationSpec(
                id="messages.reply",
                function="slack_reply_in_thread",
                summary="Reply inside a thread.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "thread_ts": _ts,
                        "text": {"type": "string"},
                        "also_send_to_channel": {
                            "type": "boolean",
                            "default": False,
                        },
                    },
                    "required": ["channel", "thread_ts", "text"],
                },
                output_schema=_posted,
                scopes=["chat:write"],
            ),
            OperationSpec(
                id="messages.post_ephemeral",
                function="slack_post_ephemeral",
                summary="Post a message only one person sees.",
                description="Leaves no channel history and cannot be edited.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "user": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["channel", "user", "text"],
                },
                output_schema={"type": "string"},
                scopes=["chat:write"],
            ),
            OperationSpec(
                id="messages.update",
                function="slack_update_message",
                summary="Edit a message this app posted.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "ts": _ts,
                        "text": {"type": "string"},
                        "blocks": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["channel", "ts", "text"],
                },
                output_schema=_posted,
                scopes=["chat:write"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.delete",
                idempotent=True,
                function="slack_delete_message",
                summary="Delete a message. Not recoverable.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"channel": _channel_id, "ts": _ts},
                    "required": ["channel", "ts"],
                },
                output_schema={"type": "string"},
                scopes=["chat:write"],
            ),
            OperationSpec(
                id="messages.schedule",
                function="slack_schedule_message",
                summary="Post a message at a future time.",
                description=(
                    "Returns a scheduled_message_id rather than a ts — a "
                    "scheduled message has no ts until it is sent."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "text": {"type": "string"},
                        "post_at": {
                            "type": "integer",
                            "description": "Unix timestamp, from ctx.now().",
                        },
                        "thread_ts": _ts,
                    },
                    "required": ["channel", "text", "post_at"],
                },
                output_schema=_posted,
                scopes=["chat:write"],
            ),
            OperationSpec(
                id="messages.add_reaction",
                function="slack_add_reaction",
                summary="React to a message with an emoji.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "ts": _ts,
                        "emoji": {
                            "type": "string",
                            "description": "Emoji name without colons.",
                        },
                    },
                    "required": ["channel", "ts", "emoji"],
                },
                output_schema={"type": "string"},
                scopes=["reactions:write"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.permalink",
                function="slack_get_permalink",
                summary="A stable link to one message.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"channel": _channel_id, "ts": _ts},
                    "required": ["channel", "ts"],
                },
                output_schema={"type": "string"},
                scopes=["channels:history"],
                idempotent=True,
            ),
        ],
        "users": [
            OperationSpec(
                id="users.list",
                function="slack_list_users",
                summary="List workspace members.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "default": 200}
                    },
                },
                output_schema={"type": "array", "items": _user},
                scopes=["users:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="users.info",
                function="slack_get_user",
                summary="Fetch one user by id.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"user": {"type": "string"}},
                    "required": ["user"],
                },
                output_schema=_user,
                scopes=["users:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="users.find_by_email",
                function="slack_find_user_by_email",
                summary="Resolve an email address to a Slack user id.",
                description=(
                    "Mentions, DMs and invites all take ids. Needs the "
                    "users:read.email scope, which is separate from "
                    "users:read. A deactivated account reports as not found."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"email": {"type": "string", "format": "email"}},
                    "required": ["email"],
                },
                output_schema=_user,
                scopes=["users:read.email"],
                idempotent=True,
                resolves="user",
            ),
        ],
        "files": [
            OperationSpec(
                id="files.upload",
                function="slack_upload_file",
                summary="Share a file in a channel.",
                description=(
                    "Three API calls to two hosts behind one operation: "
                    "files.upload itself stopped working in March 2025."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": _channel_id,
                        "content": {"type": "string"},
                        "filename": {"type": "string"},
                        "title": {"type": "string"},
                        "initial_comment": {"type": "string"},
                        "thread_ts": _ts,
                    },
                    "required": ["channel", "content", "filename"],
                },
                output_schema=SlackFileRef.model_json_schema(),
                scopes=["files:write"],
            ),
            OperationSpec(
                id="files.download",
                function="slack_download_file",
                summary="Download a shared file as a LOOM Attachment.",
                description=(
                    "url_private needs the bot token; fetching it without one "
                    "returns a sign-in page rather than the file."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "url_private": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["url_private", "filename"],
                },
                scopes=["files:read"],
                idempotent=True,
            ),
        ],
    },
)
