"""Slack Web API client.

Pure httpx. Three things are unlike every other toolset here, and each is a
silent wrong answer rather than an error if you get it wrong.

**A failure is an HTTP 200.** Every response is checked for ``ok`` before
anything else looks at it — see :mod:`loom.toolsets.slack.errors`. Nothing in
this module returns a body that has not been through ``raise_for_status``.

**The cursor is nested.** ``response_metadata.next_cursor``, and an empty
string means exhausted rather than the field being absent. Both are already
:class:`~loom.toolsets.pagination.TokenPaging` behaviours — a tuple
``token_field`` addresses the nested position — so this is that dialect at a
deeper address, not a new one.

**Uploading is three calls, to two hosts.** ``files.upload`` stopped working in
March 2025. The replacement asks Slack for a URL, PUTs the bytes to somewhere
that is not the Slack API at all, and then tells Slack the upload finished —
and skipping that last step aborts it. :meth:`SlackClient.upload_file` is one
method so no workflow author has to know that.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.core.exceptions import ConfigurationError
from loom.toolsets.pagination import Results, TokenPaging, page_through
from loom.toolsets.slack.errors import SlackPermanentError, raise_for_status
from loom.toolsets.slack.models import (
    PostedMessage,
    SlackChannel,
    SlackFileRef,
    SlackMessage,
    SlackUser,
)

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment

__all__ = [
    "SlackClient",
    "SlackSession",
    "flatten_channel",
    "flatten_message",
    "flatten_user",
    "get_default_client",
]

API_BASE = "https://slack.com/api"

#: Slack's ceiling is 1000, but its own guidance is 100-200: a large page is
#: slower to build server-side and more likely to time out. Paging is cheap
#: here because the loop is not the caller's problem.
_PAGE = 200

#: How far a name resolver scans before it stops being able to say "does not
#: exist". Five pages: enough for almost every workspace, and bounded so one
#: lookup cannot become fifty requests against a per-minute tier. Past it the
#: resolver raises rather than answering ``None``, because a "not found" from a
#: partial search is a guess that reads exactly like a fact.
_RESOLVE_SCAN = 1000

#: Slack's ceiling for one ``conversations.invite`` call.
INVITE_LIMIT = 1000

#: Default per-request timeout. Thirty seconds suits an API call and is far
#: too short for moving a file, so uploads and downloads get their own.
DEFAULT_TIMEOUT = 30.0
DEFAULT_TRANSFER_TIMEOUT = 300.0

#: Every scope the operations in this toolset need, for the setup message.
SCOPES = [
    "channels:read",
    "channels:history",
    "groups:read",
    "chat:write",
    "users:read",
    "users:read.email",
    "reactions:write",
    "files:write",
]


class SlackSession:
    """Authenticated calls against the Slack Web API.

    Slack's methods are path segments rather than REST resources, so this takes
    a method name (``"conversations.list"``) rather than a path.
    """

    def __init__(
        self,
        token: Callable[[], Awaitable[str]],
        *,
        base_url: str = API_BASE,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        """Resolved per call, not captured once: a credential store is bound to
        the *run*, and a process-wide client built at import time would
        otherwise pin whichever token happened to exist first."""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def get(self, method: str, **params: Any) -> dict[str, Any]:
        """A read. Slack takes these as query parameters."""
        return await self._call("GET", method, params=_clean(params))

    async def post(self, method: str, **body: Any) -> dict[str, Any]:
        """A write, as a JSON body.

        JSON rather than form encoding because several arguments — ``blocks``,
        ``attachments`` — are structured, and form-encoding them means
        hand-serialising JSON into a string field.
        """
        return await self._call("POST", method, json=_clean(body))

    async def _call(
        self,
        verb: str,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import httpx

        url = f"{self._base_url}/{method}"
        headers = {
            "Authorization": f"Bearer {await self._token()}",
            "Accept": "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.request(
                verb, url, headers=headers, params=params, json=json
            )

        body = _body(response)
        # The whole point: a 200 is not success. Every response goes through
        # here, so a failure cannot be mistaken for an empty result.
        return raise_for_status(
            body,
            method=method,
            status=response.status_code,
            retry_after=_retry_after(response),
        )

    async def paginate(
        self,
        method: str,
        *,
        items_key: str,
        limit: int,
        params: dict[str, Any] | None = None,
    ) -> Results[Any]:
        """Follow ``response_metadata.next_cursor`` until *limit* rows.

        Returns :class:`Results` rather than a list so a caller can tell a
        complete answer from a truncated one — which matters more here than
        elsewhere, because Slack's per-minute tiers make a long history
        genuinely slow and a caller may well want to stop early and say so.
        """
        return await page_through(
            lambda asked: self.get(method, **{**(params or {}), **asked}),
            style=TokenPaging(
                items=items_key,
                size_param="limit",
                token_param="cursor",
                # Nested, and an empty string means exhausted — both of
                # which TokenPaging already handles, so this is the same
                # dialect at a deeper address rather than a new one.
                token_field=("response_metadata", "next_cursor"),
            ),
            limit=limit,
            page_size=_PAGE,
        )


class SlackClient:
    """Async Slack client returning typed models."""

    def __init__(
        self,
        token: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT,
    ) -> None:
        self._explicit = token
        # Checked here so the failure names the fix and happens where the object
        # is built — but only when nothing could still supply one. A
        # CredentialStore is bound to a *run*, which can only be read with an
        # await, so its presence is all that can be tested at construction.
        if not self._env_token() and not token and current_credential_store() is None:
            raise ConfigurationError(
                "No Slack token configured. Set SLACK_BOT_TOKEN (a bot token, "
                "'xoxb-...'), or run 'loom connect slack'."
            )
        self._session = SlackSession(
            self._token, transport=transport, timeout=timeout
        )
        self._transport = transport
        self._transfer_timeout = transfer_timeout

    @staticmethod
    def _env_token() -> str:
        return os.environ.get("SLACK_BOT_TOKEN", "") or os.environ.get("SLACK_TOKEN", "")

    async def _token(self) -> str:
        """The bot token for this call: explicit, then connected, then env.

        A run's ``CredentialStore`` is consulted on every call rather than
        cached, because the store owns expiry and renewal — caching its answer
        here would serve a token an hour after the store itself would have
        refreshed it.
        """
        if self._explicit:
            return self._explicit
        connected = await resolve_bearer_token("slack")
        if connected:
            return connected
        token = self._env_token()
        if not token:
            raise ConfigurationError(
                "No Slack token configured. Set SLACK_BOT_TOKEN, or run "
                "'loom connect slack'."
            )
        return token

    # -- conversations -------------------------------------------------------

    async def list_channels(
        self,
        *,
        types: str = "public_channel",
        max_results: int = 200,
        exclude_archived: bool = True,
    ) -> Results[SlackChannel]:
        """List conversations, following every page.

        ``types`` is a comma-separated set of ``public_channel``,
        ``private_channel``, ``mpim``, ``im``. Private channels appear only if
        the app is a member, which is not an error and is easy to misread as
        "the workspace has none".
        """
        rows = await self._session.paginate(
            "conversations.list",
            items_key="channels",
            limit=max_results,
            params={
                "types": types,
                "exclude_archived": "true" if exclude_archived else "false",
            },
        )
        return rows.mapped(flatten_channel)

    async def find_channel(self, name: str) -> SlackChannel | None:
        """The channel with exactly this name, or ``None``.

        Slack has no lookup-by-name call, so this pages the list and matches
        exactly. Exactly, deliberately: a prefix match would return ``#eng-alerts``
        for ``#eng`` and post to the wrong room, which is worse than not finding
        it. A leading ``#`` is accepted because that is how people write it.

        A scan that ran out before finding it **raises** rather than answering
        ``None``. That third answer is the point: "not found" is a fact a
        caller acts on — it creates the channel, or reports the gap — and it is
        only a fact if the whole list was searched. ``None`` from a truncated
        scan silently loses channels that plainly exist, in exactly the
        workspaces big enough for it to matter.
        """
        wanted = name.lstrip("#").strip().lower()
        found = await self.list_channels(
            types="public_channel,private_channel", max_results=_RESOLVE_SCAN
        )
        for channel in found:
            if channel.name.lower() == wanted:
                return channel
        if not found.complete:
            raise SlackPermanentError(
                f"searched the first {len(found)} channels without finding "
                f"'{name}', and the workspace has more. Pass the channel id "
                "directly, or narrow the search — a 'not found' from a partial "
                "scan is not a 'does not exist'.",
                error="resolution_incomplete",
                method="conversations.list",
            )
        return None

    async def get_channel(self, channel: str) -> SlackChannel:
        data = await self._session.get("conversations.info", channel=channel)
        return flatten_channel(data.get("channel") or {})

    async def history(
        self,
        channel: str,
        *,
        max_results: int = 100,
        oldest: str = "",
        latest: str = "",
        include_joins: bool = False,
    ) -> Results[SlackMessage]:
        """Recent messages in a channel, newest first.

        ``oldest``/``latest`` are Slack ``ts`` strings. Derive them from
        ``ctx.now()``, never ``datetime.now()``.

        Join and leave notices are filtered out by default: they are most of a
        busy channel's history and none of its conversation, and an agent asked
        to summarise a channel otherwise summarises who joined it.
        """
        rows = await self._session.paginate(
            "conversations.history",
            items_key="messages",
            limit=max_results,
            params={"channel": channel, "oldest": oldest or None,
                    "latest": latest or None},
        )
        messages = rows.mapped(lambda raw: flatten_message(raw, channel))
        if include_joins:
            return messages
        return _without_noise(messages)

    async def replies(
        self, channel: str, thread_ts: str, *, max_results: int = 100
    ) -> Results[SlackMessage]:
        """Every message in one thread, parent first."""
        rows = await self._session.paginate(
            "conversations.replies",
            items_key="messages",
            limit=max_results,
            params={"channel": channel, "ts": thread_ts},
        )
        return rows.mapped(lambda raw: flatten_message(raw, channel))

    async def members(self, channel: str, *, max_results: int = 500) -> Results[str]:
        """User ids in a channel. Ids only — hydrate with :meth:`get_user`."""
        return await self._session.paginate(
            "conversations.members", items_key="members", limit=max_results,
            params={"channel": channel},
        )

    async def join_channel(self, channel: str) -> SlackChannel:
        """Join a public channel. The fix for ``not_in_channel``."""
        data = await self._session.post("conversations.join", channel=channel)
        return flatten_channel(data.get("channel") or {})

    async def create_channel(
        self, name: str, *, is_private: bool = False
    ) -> SlackChannel:
        data = await self._session.post(
            "conversations.create", name=name, is_private=is_private
        )
        return flatten_channel(data.get("channel") or {})

    async def invite(self, channel: str, users: list[str]) -> SlackChannel:
        """Invite users to a channel. Takes user *ids*, not names."""
        if len(users) > INVITE_LIMIT:
            raise ValueError(
                f"invite takes at most {INVITE_LIMIT} users at a time, got "
                f"{len(users)}. Chunk the list."
            )
        data = await self._session.post(
            "conversations.invite", channel=channel, users=",".join(users)
        )
        return flatten_channel(data.get("channel") or {})

    async def archive_channel(self, channel: str) -> str:
        """Archive a channel. Recoverable by a workspace admin, not by this."""
        await self._session.post("conversations.archive", channel=channel)
        return channel

    async def set_topic(self, channel: str, topic: str) -> SlackChannel:
        data = await self._session.post(
            "conversations.setTopic", channel=channel, topic=topic
        )
        return flatten_channel(data.get("channel") or {})

    # -- messages ------------------------------------------------------------

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str = "",
        blocks: list[dict[str, Any]] | None = None,
        reply_broadcast: bool = False,
        unfurl_links: bool = True,
    ) -> PostedMessage:
        """Post a message.

        ``text`` is still required when ``blocks`` are given: it is what a
        notification and a screen reader use, and omitting it produces a push
        notification that says nothing.
        """
        data = await self._session.post(
            "chat.postMessage",
            channel=channel,
            text=text,
            thread_ts=thread_ts or None,
            blocks=blocks or None,
            reply_broadcast=True if reply_broadcast else None,
            unfurl_links=unfurl_links,
        )
        return _posted(data, text)

    async def post_ephemeral(self, channel: str, user: str, text: str) -> str:
        """A message only *user* sees. Leaves nothing in the channel history.

        Returns the ``message_ts``, which cannot be updated or deleted — an
        ephemeral message has no persistent identity.
        """
        data = await self._session.post(
            "chat.postEphemeral", channel=channel, user=user, text=text
        )
        return str(data.get("message_ts", ""))

    async def update_message(
        self, channel: str, ts: str, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> PostedMessage:
        data = await self._session.post(
            "chat.update", channel=channel, ts=ts, text=text, blocks=blocks or None
        )
        return _posted(data, text)

    async def delete_message(self, channel: str, ts: str) -> str:
        """Delete a message. Not recoverable."""
        await self._session.post("chat.delete", channel=channel, ts=ts)
        return ts

    async def schedule_message(
        self, channel: str, text: str, post_at: int, *, thread_ts: str = ""
    ) -> PostedMessage:
        """Post later. ``post_at`` is a Unix timestamp, from ``ctx.now()``."""
        data = await self._session.post(
            "chat.scheduleMessage",
            channel=channel, text=text, post_at=post_at,
            thread_ts=thread_ts or None,
        )
        return PostedMessage(
            ts=str(data.get("post_at", "")),
            channel=str(data.get("channel", channel)),
            text=text,
            scheduled_message_id=str(data.get("scheduled_message_id", "")),
        )

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> str:
        """React to a message. *emoji* is a name, without colons."""
        await self._session.post(
            "reactions.add", channel=channel, timestamp=ts, name=emoji.strip(":")
        )
        return emoji

    async def permalink(self, channel: str, ts: str) -> str:
        data = await self._session.get(
            "chat.getPermalink", channel=channel, message_ts=ts
        )
        return str(data.get("permalink", ""))

    # -- users ---------------------------------------------------------------

    async def list_users(self, *, max_results: int = 200) -> Results[SlackUser]:
        rows = await self._session.paginate(
            "users.list", items_key="members", limit=max_results
        )
        return rows.mapped(flatten_user)

    async def get_user(self, user: str) -> SlackUser:
        data = await self._session.get("users.info", user=user)
        return flatten_user(data.get("user") or {})

    async def find_user_by_email(self, email: str) -> SlackUser | None:
        """Resolve an email to a user id, or ``None``.

        ``None`` rather than a raise, because "nobody has that address" is an
        ordinary answer a workflow should branch on. Slack reports a
        *deactivated* user the same way, so absence here does not prove the
        person never existed.
        """
        from loom.toolsets.slack.errors import SlackPermanentError

        try:
            data = await self._session.get("users.lookupByEmail", email=email)
        except SlackPermanentError as exc:
            if exc.error in ("users_not_found", "user_not_found"):
                return None
            raise
        return flatten_user(data.get("user") or {})

    # -- files ---------------------------------------------------------------

    async def upload_file(
        self,
        channel: str,
        content: bytes | str,
        filename: str,
        *,
        title: str = "",
        initial_comment: str = "",
        thread_ts: str = "",
    ) -> SlackFileRef:
        """Share a file in a channel.

        Three calls behind one method, because ``files.upload`` stopped working
        in March 2025 and its replacement is a protocol rather than a call:

        1. ``files.getUploadURLExternal`` — ask Slack where to put the bytes.
        2. ``POST`` the bytes to that URL, which is **not** a Slack API host and
           takes no bearer token.
        3. ``files.completeUploadExternal`` — without which the upload is
           abandoned and the file never appears.
        """
        import httpx

        payload = content.encode() if isinstance(content, str) else content

        ticket = await self._session.get(
            "files.getUploadURLExternal", filename=filename, length=len(payload)
        )
        upload_url = str(ticket.get("upload_url", ""))
        file_id = str(ticket.get("file_id", ""))
        if not upload_url or not file_id:
            raise ConfigurationError(
                f"Slack did not return an upload URL for {filename!r}: {ticket}"
            )

        async with httpx.AsyncClient(
            timeout=self._transfer_timeout, transport=self._transport
        ) as client:
            # Deliberately no Authorization header: this is a pre-signed URL on
            # a different host, and sending the bot token to it would leak the
            # credential outside slack.com.
            posted = await client.post(upload_url, content=payload)
        if posted.status_code >= 400:
            raise ConfigurationError(
                f"uploading {filename!r} to Slack's storage failed with "
                f"HTTP {posted.status_code}"
            )

        entry: dict[str, Any] = {"id": file_id, "title": title or filename}
        done = await self._session.post(
            "files.completeUploadExternal",
            files=[entry],
            channel_id=channel or None,
            initial_comment=initial_comment or None,
            thread_ts=thread_ts or None,
        )
        shared = (done.get("files") or [{}])[0]
        return SlackFileRef(
            id=str(shared.get("id", file_id)),
            name=str(shared.get("name", filename)),
            mimetype=str(shared.get("mimetype", "")),
            size=int(shared.get("size", len(payload)) or 0),
            permalink=str(shared.get("permalink", "")),
            url_private=str(shared.get("url_private", "")),
        )

    async def download_file(self, url_private: str, filename: str) -> Attachment:
        """Download a file shared in Slack.

        ``url_private`` is not public — it needs the same bearer token as the
        API, which is why this cannot be a plain fetch.
        """
        import httpx

        from loom.blobs.attachment import Attachment

        async with httpx.AsyncClient(
            timeout=self._transfer_timeout, transport=self._transport
        ) as client:
            response = await client.get(
                url_private,
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
        if response.status_code >= 400:
            raise ConfigurationError(
                f"downloading {filename!r} failed with HTTP {response.status_code}"
            )
        return Attachment.from_bytes(filename, response.content)


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

#: Subtypes that are workspace bookkeeping rather than conversation.
_NOISE = frozenset(
    {"channel_join", "channel_leave", "channel_topic", "channel_purpose",
     "channel_name", "channel_archive", "channel_unarchive"}
)


def _without_noise(messages: Results[SlackMessage]) -> Results[SlackMessage]:
    """Drop join/leave notices, keeping the coverage.

    Rebuilt through ``Results`` rather than a comprehension: a comprehension
    yields a plain list and throws away whether the fetch saw everything.
    """
    kept = [m for m in messages if m.subtype not in _NOISE]
    return Results(
        kept,
        complete=messages.complete,
        total=messages.total,
        cursor=messages.cursor,
    )


def flatten_message(raw: dict[str, Any], channel: str = "") -> SlackMessage:
    """Flatten a Slack message into :class:`SlackMessage`."""
    return SlackMessage(
        ts=str(raw.get("ts", "")),
        channel=channel or str(raw.get("channel", "")),
        user=str(raw.get("user", "")),
        bot_id=str(raw.get("bot_id", "")),
        text=str(raw.get("text", "")),
        thread_ts=str(raw.get("thread_ts", "")),
        reply_count=int(raw.get("reply_count", 0) or 0),
        subtype=str(raw.get("subtype", "")),
        permalink=str(raw.get("permalink", "")),
        files=[
            SlackFileRef(
                id=str(f.get("id", "")),
                name=str(f.get("name", "")),
                mimetype=str(f.get("mimetype", "")),
                size=int(f.get("size", 0) or 0),
                permalink=str(f.get("permalink", "")),
                url_private=str(f.get("url_private", "")),
            )
            for f in raw.get("files") or []
            if isinstance(f, dict)
        ],
        reactions=list(raw.get("reactions") or []),
    )


def flatten_channel(raw: dict[str, Any]) -> SlackChannel:
    """Flatten a conversation into :class:`SlackChannel`."""
    return SlackChannel(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        is_private=bool(raw.get("is_private", False)),
        is_archived=bool(raw.get("is_archived", False)),
        is_member=bool(raw.get("is_member", False)),
        is_im=bool(raw.get("is_im", False)),
        # Both arrive as ``{"value": "...", "creator": ..., "last_set": ...}``.
        topic=str((raw.get("topic") or {}).get("value", "")),
        purpose=str((raw.get("purpose") or {}).get("value", "")),
        num_members=int(raw.get("num_members", 0) or 0),
        created=int(raw.get("created", 0) or 0),
    )


def flatten_user(raw: dict[str, Any]) -> SlackUser:
    """Flatten a member into :class:`SlackUser`."""
    profile = raw.get("profile") or {}
    return SlackUser(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        real_name=str(raw.get("real_name", "") or profile.get("real_name", "")),
        display_name=str(profile.get("display_name", "")),
        email=str(profile.get("email", "")),
        is_bot=bool(raw.get("is_bot", False)),
        is_admin=bool(raw.get("is_admin", False)),
        deleted=bool(raw.get("deleted", False)),
        timezone=str(raw.get("tz", "")),
        title=str(profile.get("title", "")),
    )


def _posted(data: dict[str, Any], text: str) -> PostedMessage:
    message = data.get("message") or {}
    return PostedMessage(
        ts=str(data.get("ts", "")),
        channel=str(data.get("channel", "")),
        text=str(message.get("text", text)),
    )


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    """Drop unset arguments.

    Slack distinguishes absent from empty for several parameters —
    ``thread_ts: ""`` is rejected where omitting it posts to the channel — so a
    ``None`` must not be sent as an empty string.
    """
    return {k: v for k, v in values.items() if v is not None}


def _body(response: Any) -> dict[str, Any]:
    try:
        decoded = response.json()
    except ValueError:
        return {"ok": False, "error": f"http_{response.status_code}"}
    return decoded if isinstance(decoded, dict) else {"ok": False, "error": "bad_body"}


def _retry_after(response: Any) -> float:
    try:
        return float(response.headers.get("Retry-After", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default_client: SlackClient | None = None


def get_default_client() -> SlackClient:
    """Return (or build) the module-level client from environment credentials."""
    global _default_client
    if _default_client is None:
        _default_client = SlackClient()
    return _default_client


def reset_default_client() -> None:
    """Drop the cached client. For tests, and after a credential change."""
    global _default_client
    _default_client = None
