"""Microsoft Teams over Graph — pure httpx, no vendor SDK.

Three constraints from the reference shape this client, and none of them is
obvious from the endpoints:

1. **Sending is delegated-only.** Application permissions are not supported for
   posting a message; only ``Teamwork.Migrate.All``, for migration. That is
   documented rather than refused — a migration app is a legitimate caller —
   but a 403 on a send is annotated to say so.
2. **Only ``$top`` and ``$expand`` work on messages.** "The other OData query
   parameters aren't currently supported", so this client offers no filter or
   sort arguments there. Accepting one Graph ignores would be the silent
   wrong-answer shape the rest of this package is built to avoid.
3. **Replies are a second pagination.** ``$expand=replies`` returns up to 200
   by default and 1,000 at most, with its own ``replies@odata.nextLink``
   nested inside each message — so replies get their own operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loom.toolsets.microsoft.auth import (
    GRAPH_BASE_URL,
    MicrosoftAuth,
    get_default_auth,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.scope import user_root
from loom.toolsets.microsoft.teams.models import (
    Channel,
    ChatMessage,
    Team,
    TeamsChat,
    TeamsMember,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

__all__ = ["MESSAGE_PAGE_MAX", "TeamsClient"]

#: Graph caps a page of channel messages and of chats at 50, and defaults to 20.
#: Asking for more is not an error — it is clamped — so the ceiling lives here
#: rather than being discovered as a short page.
MESSAGE_PAGE_MAX = 50


class TeamsClient:
    """Teams, channels, messages, and chats."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        user_id: str = "",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._user_id = user_id
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    # -- addressing ----------------------------------------------------------

    def _user(self) -> str:
        return user_root(
            self._auth,
            self._user_id,
            workload="a user's teams and chats",
            env_hint="MS_TEAMS_USER",
        )

    @staticmethod
    def _channel(team_id: str, channel_id: str) -> str:
        """Address a channel.

        Both ids go through ``quote`` because a channel id is
        ``19:...@thread.tacv2`` — a colon and an ``@`` inside a path segment.
        """
        return (
            f"/teams/{quote(team_id, safe='')}"
            f"/channels/{quote(channel_id, safe='')}"
        )

    # -- identity ------------------------------------------------------------

    async def whoami(self) -> MicrosoftUser:
        root = user_root(self._auth, workload="a signed-in user")
        return MicrosoftUser.from_api(await self._session.get(root))

    # -- teams and channels --------------------------------------------------

    async def list_joined_teams(self, *, limit: int = 100) -> Results[Team]:
        return await self._session.paginate(
            f"{self._user()}/joinedTeams",
            limit=limit,
            page_size=100,
            row=Team.from_api,
        )

    async def get_team(self, team_id: str) -> Team:
        return Team.from_api(
            await self._session.get(f"/teams/{quote(team_id, safe='')}")
        )

    async def list_channels(self, team_id: str, *, limit: int = 100) -> Results[Channel]:
        return await self._session.paginate(
            f"/teams/{quote(team_id, safe='')}/channels",
            limit=limit,
            page_size=100,
            row=Channel.from_api,
        )

    async def get_channel(self, team_id: str, channel_id: str) -> Channel:
        return Channel.from_api(
            await self._session.get(self._channel(team_id, channel_id))
        )

    async def list_team_members(
        self, team_id: str, *, limit: int = 100
    ) -> Results[TeamsMember]:
        return await self._session.paginate(
            f"/teams/{quote(team_id, safe='')}/members",
            limit=limit,
            page_size=100,
            row=TeamsMember.from_api,
        )

    # -- channel messages ----------------------------------------------------

    async def list_channel_messages(
        self, team_id: str, channel_id: str, *, limit: int = 50
    ) -> Results[ChatMessage]:
        """List a channel's root messages, newest reply-chain activity first.

        Replies are **not** included. Graph sorts by the last modified date of
        the whole reply chain, so a message with recent replies floats up even
        though the message itself is old.
        """
        return await self._session.paginate(
            f"{self._channel(team_id, channel_id)}/messages",
            limit=limit,
            page_size=MESSAGE_PAGE_MAX,
            row=ChatMessage.from_api,
        )

    async def get_channel_message(
        self, team_id: str, channel_id: str, message_id: str
    ) -> ChatMessage:
        return ChatMessage.from_api(
            await self._session.get(
                f"{self._channel(team_id, channel_id)}"
                f"/messages/{quote(message_id, safe='')}"
            )
        )

    async def list_message_replies(
        self, team_id: str, channel_id: str, message_id: str, *, limit: int = 100
    ) -> Results[ChatMessage]:
        """List the replies to one channel message.

        Its own operation rather than a flag on the listing, because
        ``$expand=replies`` truncates at 200 by default with a *separate*
        nested next-link that a caller reading ``value[].replies`` never
        follows — a silently short thread.
        """
        return await self._session.paginate(
            f"{self._channel(team_id, channel_id)}"
            f"/messages/{quote(message_id, safe='')}/replies",
            limit=limit,
            page_size=MESSAGE_PAGE_MAX,
            row=ChatMessage.from_api,
        )

    async def send_channel_message(
        self,
        team_id: str,
        channel_id: str,
        content: str,
        *,
        content_type: str = "html",
        subject: str = "",
        importance: str = "",
    ) -> ChatMessage:
        return ChatMessage.from_api(
            await self._session.post(
                f"{self._channel(team_id, channel_id)}/messages",
                json=_message_body(content, content_type, subject, importance),
            )
            or {}
        )

    async def reply_to_message(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        content_type: str = "html",
    ) -> ChatMessage:
        return ChatMessage.from_api(
            await self._session.post(
                f"{self._channel(team_id, channel_id)}"
                f"/messages/{quote(message_id, safe='')}/replies",
                json=_message_body(content, content_type, "", ""),
            )
            or {}
        )

    # -- chats ---------------------------------------------------------------

    async def list_chats(
        self, *, limit: int = 50, expand_members: bool = False
    ) -> Results[TeamsChat]:
        """List the user's chats, most recently active first.

        ``expand_members`` is off by default because Graph caps expanded
        members at **25** whatever page size is asked for, and returns no
        marker saying it truncated — so a group chat of 40 silently reports 25.
        Use ``list_chat_members`` when the membership matters.
        """
        params: dict[str, Any] = {
            # The only ordering Graph supports here; ascending is rejected.
            "$orderby": "lastMessagePreview/createdDateTime desc",
        }
        if expand_members:
            params["$expand"] = "members"
        return await self._session.paginate(
            f"{self._user()}/chats",
            limit=limit,
            params=params,
            page_size=MESSAGE_PAGE_MAX,
            row=TeamsChat.from_api,
        )

    async def get_chat(self, chat_id: str) -> TeamsChat:
        return TeamsChat.from_api(
            await self._session.get(f"/chats/{quote(chat_id, safe='')}")
        )

    async def list_chat_messages(
        self, chat_id: str, *, limit: int = 50
    ) -> Results[ChatMessage]:
        return await self._session.paginate(
            f"/chats/{quote(chat_id, safe='')}/messages",
            limit=limit,
            page_size=MESSAGE_PAGE_MAX,
            row=ChatMessage.from_api,
        )

    async def list_chat_members(
        self, chat_id: str, *, limit: int = 100
    ) -> Results[TeamsMember]:
        return await self._session.paginate(
            f"/chats/{quote(chat_id, safe='')}/members",
            limit=limit,
            page_size=100,
            row=TeamsMember.from_api,
        )

    async def send_chat_message(
        self, chat_id: str, content: str, *, content_type: str = "html"
    ) -> ChatMessage:
        return ChatMessage.from_api(
            await self._session.post(
                f"/chats/{quote(chat_id, safe='')}/messages",
                json=_message_body(content, content_type, "", ""),
            )
            or {}
        )


def _message_body(
    content: str, content_type: str, subject: str, importance: str
) -> dict[str, Any]:
    """Build a chatMessage request body.

    ``content_type`` is validated rather than passed through: Graph accepts
    only ``text`` and ``html``, and an unrecognised value is rejected with a
    message that names the enum rather than the argument.
    """
    if content_type not in ("html", "text"):
        raise GraphPermanentError(
            f"content_type must be 'html' or 'text', not {content_type!r}.",
            status=0,
            code="invalidContentType",
        )
    body: dict[str, Any] = {"body": {"contentType": content_type, "content": content}}
    if subject:
        body["subject"] = subject
    if importance:
        body["importance"] = importance
    return body


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


