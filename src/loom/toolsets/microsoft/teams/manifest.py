"""Microsoft Teams ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.teams.models import (
    Channel,
    ChatMessage,
    Team,
    TeamsChat,
    TeamsMember,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


TEAMS_MANIFEST = ToolsetManifest(
    id="teams",
    version="1.0.0",
    summary="Microsoft Teams — teams, channels, messages, and chats.",
    description=(
        "Microsoft Graph v1.0. Read teams, channels, channel messages and their "
        "reply threads, chats and chat messages; post messages and replies. "
        "SENDING REQUIRES DELEGATED CREDENTIALS — Graph does not support "
        "application permissions for posting a Teams message (only "
        "Teamwork.Migrate.All, for migration), so an app-only token gets 403 on "
        "any send. DO NOT POLL: Graph's Teams documentation states that polling "
        "a resource more than once per day violates the Microsoft APIs Terms of "
        "Use; use change notifications for anything that must react quickly. "
        "Channel message listings support paging only — no filter or sort, "
        "which Graph ignores rather than rejecting."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth={
        "type": "oauth2",
        "fields": [
            "MS_TENANT_ID",
            "MS_CLIENT_ID",
            "MS_CLIENT_SECRET",
            "MS_REFRESH_TOKEN",
            "MS_GRAPH_ACCESS_TOKEN",
            # Read by the shared auth layer, so declared here: the Azure SDK
            # trio is what a host already has in its environment, and
            # MS_AUTHORITY_HOST is the only way to reach a national cloud.
            # Omitting them told `loom toolset` users to set MS_* variables
            # they did not need.
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "MS_AUTHORITY_HOST",
            "MS_TEAMS_USER",
        ],
    },
    tools_module="loom.toolsets.microsoft.teams.tools",
    egress_hosts=["graph.microsoft.com", "login.microsoftonline.com"],
    groups={
        "teams": [
            OperationSpec(
                id="teams.list_joined",
                function="teams_list_joined_teams",
                summary="List the teams this user belongs to.",
                description=(
                    "Resolve a team before working in it — every other tool "
                    "takes the id, and a display name is not addressable."
                ),
                resolves="team",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Team),
            ),
            OperationSpec(
                id="teams.get",
                function="teams_get_team",
                summary="Fetch one team by id.",
                description="is_archived matters: an archived team is read-only.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=Team.model_json_schema(),
            ),
            OperationSpec(
                id="teams.list_channels",
                function="teams_list_channels",
                summary="List a team's channels.",
                description=(
                    "Resolve a channel before posting; ids look like "
                    "19:...@thread.tacv2 and are never typed by a person."
                ),
                resolves="channel",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Channel),
            ),
            OperationSpec(
                id="teams.get_channel",
                function="teams_get_channel",
                summary="Fetch one channel by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=Channel.model_json_schema(),
            ),
            OperationSpec(
                id="teams.list_members",
                function="teams_list_team_members",
                summary="List a team's members.",
                description=(
                    "Resolve a person here: a mention needs the directory "
                    "user_id, not a display name."
                ),
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(TeamsMember),
            ),
            OperationSpec(
                id="teams.whoami",
                function="teams_whoami",
                summary="The person these credentials authenticate as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=MicrosoftUser.model_json_schema(),
            ),
        ],
        "messages": [
            OperationSpec(
                id="messages.list_channel",
                function="teams_list_channel_messages",
                summary="List a channel's root messages, replies excluded.",
                description=(
                    "Ordered by last activity in the whole reply chain, so an "
                    "old message with new replies floats up. Filter out "
                    "message_type == 'systemEventMessage' to skip join/leave "
                    "records. No filter or sort argument: Graph supports "
                    "neither and ignores them."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ChatMessage),
            ),
            OperationSpec(
                id="messages.get_channel",
                function="teams_get_channel_message",
                summary="Fetch one channel message.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=ChatMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.list_replies",
                function="teams_list_message_replies",
                summary="List the replies to one channel message.",
                description=(
                    "Its own call because expanding replies inline truncates "
                    "at 200 behind a nested next-link, returning a short "
                    "thread with nothing saying so."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ChatMessage),
            ),
            OperationSpec(
                id="messages.send_channel",
                function="teams_send_channel_message",
                summary="Post a message to a channel.",
                description=(
                    "Needs DELEGATED credentials — app-only gets 403. Not "
                    "retried: a retry posts twice, visibly."
                ),
                effect=EffectClass.WRITE,
                output_schema=ChatMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.reply",
                function="teams_reply_to_message",
                summary="Reply in an existing message's thread.",
                description=(
                    "Keeps the conversation in one thread; a new message "
                    "instead starts a second one that reads as a duplicate."
                ),
                effect=EffectClass.WRITE,
                output_schema=ChatMessage.model_json_schema(),
            ),
        ],
        "chats": [
            OperationSpec(
                id="chats.list",
                function="teams_list_chats",
                summary="List this user's chats, most recently active first.",
                description=(
                    "topic is empty for one-on-one chats, so members identify "
                    "those. expand_members is off by default: Graph caps "
                    "expanded members at 25 with no marker."
                ),
                resolves="chat",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(TeamsChat),
            ),
            OperationSpec(
                id="chats.get",
                function="teams_get_chat",
                summary="Fetch one chat by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=TeamsChat.model_json_schema(),
            ),
            OperationSpec(
                id="chats.list_messages",
                function="teams_list_chat_messages",
                summary="List the messages in a chat.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ChatMessage),
            ),
            OperationSpec(
                id="chats.list_members",
                function="teams_list_chat_members",
                summary="List a chat's members, paging properly.",
                description=(
                    "The reliable way to get a full membership; the inline "
                    "expansion on chats.list caps at 25."
                ),
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(TeamsMember),
            ),
            OperationSpec(
                id="chats.send",
                function="teams_send_chat_message",
                summary="Send a message to an existing chat.",
                description="Needs DELEGATED credentials. Not retried.",
                effect=EffectClass.WRITE,
                output_schema=ChatMessage.model_json_schema(),
            ),
        ],
    },
)
