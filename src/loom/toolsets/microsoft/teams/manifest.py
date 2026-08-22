"""Microsoft Teams ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
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
    auth=AuthSpec(
        client="loom.toolsets.microsoft.teams.client:TeamsClient",
        credentials="loom.toolsets.microsoft.auth:MicrosoftAuth",
        # One credential across the six Graph toolsets, for the reason the
        # Google five share one. `MS_*_USER` exists because `/me` does not
        # resolve under app-only credentials — see toolsets/CLAUDE.md.
        kind="oauth2",
        credential="microsoft",
        provider="microsoft",
        scopes=("offline_access",),
        fields=(
            # Three alternatives, mirroring `MicrosoftCredentials.mode`. The
            # AZURE_* trio is the same credential under the names the Azure
            # SDKs already put in an environment, so it is a mode rather than
            # three more required variables.
            AuthField(name="MS_TENANT_ID", label="Tenant id", secret=False, mode="app"),
            AuthField(name="MS_CLIENT_ID", label="Application (client) id",
                      secret=False, mode="app"),
            AuthField(name="MS_CLIENT_SECRET", label="Client secret", mode="app"),
            AuthField(name="AZURE_TENANT_ID", label="Tenant id (Azure SDK name)",
                      secret=False, mode="azure"),
            AuthField(name="AZURE_CLIENT_ID", label="Client id (Azure SDK name)",
                      secret=False, mode="azure"),
            AuthField(name="AZURE_CLIENT_SECRET", label="Client secret (Azure SDK name)",
                      mode="azure"),
            AuthField(name="MS_GRAPH_ACCESS_TOKEN", label="Graph access token",
                      mode="token"),
            # Adds delegated identity to the app mode rather than replacing it:
            # without it the same three variables authenticate the application.
            AuthField(name="MS_REFRESH_TOKEN", label="Refresh token (delegated)",
                      required=False),
            AuthField(name="MS_AUTHORITY_HOST", label="Authority host (sovereign cloud)",
                      secret=False, required=False),
            AuthField(name="MS_TEAMS_USER", arg="user_id", label="User to act as (app-only)",
                      secret=False, required=False),
        ),
        docs_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
    ),
    tools_module="loom.toolsets.microsoft.teams.tools",
    egress_hosts=["graph.microsoft.com", "login.microsoftonline.com"],
    rate_limits={
        "model": "dynamic per-workload throttling; honour Retry-After on a 429",
        "polling": (
            "Graph states that polling a resource more than once a day "
            "violates the Microsoft APIs Terms of Use"
        ),
        "source": "learn.microsoft.com/en-us/graph/throttling",
    },
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
                scopes=["Team.ReadBasic.All"],
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
                scopes=["Team.ReadBasic.All"],
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
                scopes=["Team.ReadBasic.All"],
                idempotent=True,
                pagination=True,
                output_schema=_array(Channel),
            ),
            OperationSpec(
                id="teams.get_channel",
                function="teams_get_channel",
                summary="Fetch one channel by id.",
                effect=EffectClass.READ,
                scopes=["Team.ReadBasic.All"],
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
                scopes=["Team.ReadBasic.All"],
                idempotent=True,
                pagination=True,
                output_schema=_array(TeamsMember),
            ),
            OperationSpec(
                id="teams.whoami",
                function="teams_whoami",
                summary="The person these credentials authenticate as.",
                effect=EffectClass.READ,
                scopes=["User.Read"],
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
                scopes=["ChannelMessage.Read.All"],
                idempotent=True,
                pagination=True,
                output_schema=_array(ChatMessage),
            ),
            OperationSpec(
                id="messages.get_channel",
                function="teams_get_channel_message",
                summary="Fetch one channel message.",
                effect=EffectClass.READ,
                scopes=["ChannelMessage.Read.All"],
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
                scopes=["ChannelMessage.Read.All"],
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
                scopes=["ChannelMessage.Send"],
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
                scopes=["ChannelMessage.Send"],
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
                scopes=["Chat.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(TeamsChat),
            ),
            OperationSpec(
                id="chats.get",
                function="teams_get_chat",
                summary="Fetch one chat by id.",
                effect=EffectClass.READ,
                scopes=["Chat.Read"],
                idempotent=True,
                output_schema=TeamsChat.model_json_schema(),
            ),
            OperationSpec(
                id="chats.list_messages",
                function="teams_list_chat_messages",
                summary="List the messages in a chat.",
                effect=EffectClass.READ,
                scopes=["Chat.Read"],
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
                scopes=["Chat.Read"],
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
                scopes=["ChatMessage.Send"],
                output_schema=ChatMessage.model_json_schema(),
            ),
        ],
    },
)
