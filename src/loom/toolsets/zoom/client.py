"""Zoom API v2 client.

Pure httpx. Two things about Zoom are easy to get wrong and produce a *not
found* rather than an error, which is the worst kind of wrong.

**A meeting UUID may need double URL-encoding.** UUIDs are base64, so they can
contain ``/``. Zoom's rule is that a UUID beginning with ``/`` or containing
``//`` must be encoded **twice**; encoded once, the API answers ``3001 Meeting
does not exist`` for a meeting that plainly does. :func:`encode_uuid` applies
the rule, and every past-meeting path goes through it.

**The paging token expires after fifteen minutes.** That is fine for a loop
that runs to completion, and it is a trap for the pattern this codebase
otherwise encourages — carrying ``Results.cursor`` in ``ctx.state`` across a
durable park. Against Zoom that only works if the run resumes inside the
window; past it, start again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.toolsets.pagination import Results, TokenPaging, page_through
from loom.toolsets.zoom.errors import classify
from loom.toolsets.zoom.models import (
    ZoomMeeting,
    ZoomParticipant,
    ZoomRecording,
    ZoomRecordingFile,
    ZoomUser,
)

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment
    from loom.toolsets.zoom.auth import ZoomAuth

__all__ = [
    "ZoomClient",
    "ZoomSession",
    "encode_uuid",
    "flatten_meeting",
]

API_BASE = "https://api.zoom.us/v2"

#: Zoom's own ceilings, which differ by how expensive the endpoint is. Asking
#: for more is a 400 rather than a clamp, so these are the values and not
#: merely defaults.
_LIGHT_PAGE = 300
_HEAVY_PAGE = 100

#: Default per-request timeout. Thirty seconds suits an API call; a cloud
#: recording is hundreds of megabytes and legitimately takes minutes, so it
#: gets its own budget rather than forcing a caller to subclass this.
DEFAULT_TIMEOUT = 30.0
DEFAULT_TRANSFER_TIMEOUT = 300.0


class ZoomSession:
    """Authenticated JSON calls against the Zoom API."""

    def __init__(
        self,
        auth: ZoomAuth,
        *,
        base_url: str = API_BASE,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """One call, returning the decoded body (``None`` for a 204).

        A 401 is retried exactly once with a freshly minted token — not a retry
        policy in disguise, but the one case where the identical request sent
        again legitimately succeeds, because the cached token expired sooner
        than its stated lifetime.
        """
        import httpx

        url = f"{self._base_url}/{path.lstrip('/')}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(client, method, url, clean, json)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(client, method, url, clean, json)

            if response.status_code >= 400:
                raise classify(response.status_code, _body(response), url)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: dict[str, Any],
        json: Any,
    ) -> httpx.Response:
        headers = await self._auth.headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        return await client.request(
            method, url, headers=headers, params=params, json=json
        )

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("POST", path, params=params, json=json)

    async def patch(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("PATCH", path, params=params, json=json)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params)

    async def paginate(
        self,
        path: str,
        *,
        items_key: str,
        limit: int,
        params: dict[str, Any] | None = None,
        page_size: int = _HEAVY_PAGE,
    ) -> Results[Any]:
        """Follow ``next_page_token`` until *limit* rows.

        ``page_size`` is per endpoint because Zoom's ceiling is: 300 for a
        light read, 30/50/100 for a heavy one, and exceeding it is a 400 rather
        than a clamp.
        """
        return await page_through(
            lambda asked: self.request("GET", path, params={**(params or {}), **asked}),
            style=TokenPaging(
                items=items_key,
                size_param="page_size",
                token_param="next_page_token",
                token_field="next_page_token",
                total_field="total_records",
            ),
            limit=limit,
            page_size=page_size,
        )


class ZoomClient:
    """Async Zoom client returning typed models."""

    def __init__(
        self,
        auth: ZoomAuth | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT,
    ) -> None:
        from loom.toolsets.zoom.auth import get_default_auth

        self._session = ZoomSession(
            auth or get_default_auth(), transport=transport, timeout=timeout
        )
        self._transport = transport
        self._transfer_timeout = transfer_timeout

    # -- meetings ------------------------------------------------------------

    async def list_meetings(
        self,
        user_id: str = "me",
        *,
        meeting_type: str = "scheduled",
        max_results: int = 100,
    ) -> Results[ZoomMeeting]:
        """Meetings for a user.

        ``meeting_type`` is ``scheduled`` (default), ``live``, ``upcoming``,
        ``upcoming_meetings``, or ``previous_meetings``. Note that
        ``scheduled`` excludes instant meetings and includes recurring ones
        whether or not they have an occurrence soon — it is a list of what is
        *set up*, not of what is happening.
        """
        rows = await self._session.paginate(
            f"users/{_quote(user_id, 'user_id')}/meetings",
            items_key="meetings",
            limit=max_results,
            params={"type": meeting_type},
            page_size=_LIGHT_PAGE,
        )
        return rows.mapped(flatten_meeting)

    async def get_meeting(self, meeting_id: int | str) -> ZoomMeeting:
        """Fetch one meeting by its numeric id."""
        data = await self._session.get(f"meetings/{meeting_id}")
        return flatten_meeting(data or {})

    async def create_meeting(
        self,
        topic: str,
        *,
        user_id: str = "me",
        start_time: str = "",
        duration: int = 30,
        timezone: str = "UTC",
        agenda: str = "",
        password: str = "",
        meeting_type: int = 2,
        settings: dict[str, Any] | None = None,
    ) -> ZoomMeeting:
        """Schedule a meeting.

        ``start_time`` is ``YYYY-MM-DDTHH:MM:SSZ``. Derive it from
        ``ctx.now()``: a literal ``datetime.now()`` in a workflow body
        schedules a different meeting on every replay.
        """
        body: dict[str, Any] = {
            "topic": topic,
            "type": meeting_type,
            "duration": duration,
            "timezone": timezone,
        }
        if start_time:
            body["start_time"] = start_time
        if agenda:
            body["agenda"] = agenda
        if password:
            body["password"] = password
        if settings:
            body["settings"] = settings

        data = await self._session.post(
            f"users/{_quote(user_id, 'user_id')}/meetings", body
        )
        return flatten_meeting(data or {})

    async def update_meeting(
        self, meeting_id: int | str, fields: dict[str, Any]
    ) -> int | str:
        """Patch a meeting. Answers 204 with no body, so the id comes back."""
        await self._session.patch(f"meetings/{meeting_id}", fields)
        return meeting_id

    async def delete_meeting(
        self, meeting_id: int | str, *, notify: bool = False
    ) -> int | str:
        """Cancel a meeting.

        ``notify`` defaults to ``False`` so cancelling a hundred stale meetings
        does not email a hundred sets of attendees as a side effect of a
        default — the same rule as Calendar's ``send_updates``.
        """
        await self._session.delete(
            f"meetings/{meeting_id}",
            schedule_for_reminder="true" if notify else "false",
        )
        return meeting_id

    # -- past meetings -------------------------------------------------------

    async def past_meeting(self, meeting_uuid: str) -> dict[str, Any]:
        """Details of one finished occurrence, by **UUID**."""
        data = await self._session.get(f"past_meetings/{encode_uuid(meeting_uuid)}")
        return dict(data or {})

    async def participants(
        self, meeting_uuid: str, *, max_results: int = 300
    ) -> Results[ZoomParticipant]:
        """Who attended a finished meeting, by **UUID**, not by id.

        One row per *session*: someone who dropped and rejoined appears twice,
        which is why summing ``duration`` by name is how an attendance report
        ends up double-counting people.
        """
        rows = await self._session.paginate(
            f"past_meetings/{encode_uuid(meeting_uuid)}/participants",
            items_key="participants",
            limit=max_results,
            page_size=_LIGHT_PAGE,
        )
        return rows.mapped(_participant)

    # -- recordings ----------------------------------------------------------

    async def list_recordings(
        self,
        user_id: str = "me",
        *,
        start: str = "",
        end: str = "",
        max_results: int = 100,
    ) -> Results[ZoomRecording]:
        """Cloud recordings for a user.

        Zoom's window defaults to the last month and spans **at most one
        month** per request — a wider ``start``/``end`` is silently narrowed
        rather than rejected, so a "everything this year" query returns one
        month and no error.
        """
        rows = await self._session.paginate(
            f"users/{_quote(user_id, 'user_id')}/recordings",
            items_key="meetings",
            limit=max_results,
            params={"from": start or None, "to": end or None},
            page_size=_HEAVY_PAGE,
        )
        return rows.mapped(_recording)

    async def get_recording(self, meeting_id: int | str) -> ZoomRecording:
        """Recordings for one meeting, by id or UUID."""
        target = (
            encode_uuid(str(meeting_id))
            if _looks_like_uuid(str(meeting_id))
            else str(meeting_id)
        )
        data = await self._session.get(f"meetings/{target}/recordings")
        return _recording(data or {})

    async def delete_recording(self, meeting_id: int | str) -> int | str:
        """Delete a meeting's recordings. Goes to the trash for 30 days."""
        await self._session.delete(f"meetings/{meeting_id}/recordings")
        return meeting_id

    async def download_recording(
        self, download_url: str, filename: str
    ) -> Attachment:
        """Download a recording file as a LOOM :class:`Attachment`.

        The URL is not public — it needs the same bearer token as the API,
        which is why this cannot be a plain fetch. Recordings are large; with
        ``Runtime(blobs=...)`` the payload offloads out of the journal.
        """
        import httpx

        from loom.blobs.attachment import Attachment
        from loom.toolsets.zoom.auth import get_default_auth

        headers = await get_default_auth().headers()
        async with httpx.AsyncClient(
            timeout=self._transfer_timeout,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            response = await client.get(download_url, headers=headers)
        if response.status_code >= 400:
            raise classify(
                response.status_code, _body(response), download_url
            )
        return Attachment.from_bytes(filename, response.content)

    # -- users ---------------------------------------------------------------

    async def list_users(
        self, *, status: str = "active", max_results: int = 300
    ) -> Results[ZoomUser]:
        """Account members."""
        rows = await self._session.paginate(
            "users",
            items_key="users",
            limit=max_results,
            params={"status": status},
            page_size=_LIGHT_PAGE,
        )
        return rows.mapped(_user)

    async def get_user(self, user_id: str = "me") -> ZoomUser:
        """One user. ``"me"`` is the authenticated account."""
        data = await self._session.get(f"users/{_quote(user_id, 'user_id')}")
        return _user(data or {})

    async def find_user_by_email(self, email: str) -> ZoomUser | None:
        """Resolve an email address to a Zoom user, or ``None``.

        Zoom's ``users/{id}`` accepts an email directly, so this is a lookup
        rather than a scan — but it 404s for an address with no account, and
        that is an ordinary answer a workflow should branch on rather than an
        error.
        """
        from loom.toolsets.zoom.errors import ZoomPermanentError

        try:
            return await self.get_user(email)
        except ZoomPermanentError as exc:
            if exc.status == 404 or exc.code == 1001:
                return None
            raise


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def encode_uuid(meeting_uuid: str) -> str:
    """Percent-encode a meeting UUID, doubly when Zoom requires it.

    UUIDs are base64, so they contain ``/`` and ``+``. Zoom's documented rule:
    a UUID that **begins with ``/`` or contains ``//``** must be encoded twice.
    Encoded once, those requests answer ``3001 Meeting does not exist`` — a
    *not found* for a meeting that exists, which sends the reader looking for
    the wrong bug entirely.

    A UUID with neither is encoded once, because double-encoding it would
    produce the same 3001 from the other direction.
    """
    if not isinstance(meeting_uuid, str) or not meeting_uuid:
        raise ValueError(
            f"meeting_uuid must be a non-empty string, got {meeting_uuid!r}. "
            "It comes from meeting.uuid — the numeric meeting.id identifies "
            "the series, not one occurrence."
        )

    from urllib.parse import quote

    once = quote(meeting_uuid, safe="")
    if meeting_uuid.startswith("/") or "//" in meeting_uuid:
        return quote(once, safe="")
    return once


def _looks_like_uuid(value: str) -> bool:
    """Whether this is a UUID rather than a numeric meeting id."""
    return not value.isdigit()


def _quote(value: str, argument: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{argument} must be a non-empty string, got {value!r}")

    from urllib.parse import quote

    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def flatten_meeting(raw: dict[str, Any]) -> ZoomMeeting:
    """Flatten a meeting resource into :class:`ZoomMeeting`."""
    return ZoomMeeting(
        id=int(raw.get("id", 0) or 0),
        uuid=str(raw.get("uuid", "")),
        topic=str(raw.get("topic", "")),
        agenda=str(raw.get("agenda", "")),
        type=int(raw.get("type", 2) or 2),
        status=str(raw.get("status", "")),
        start_time=str(raw.get("start_time", "")),
        duration=int(raw.get("duration", 0) or 0),
        timezone=str(raw.get("timezone", "")),
        join_url=str(raw.get("join_url", "")),
        start_url=str(raw.get("start_url", "")),
        password=str(raw.get("password", "")),
        host_id=str(raw.get("host_id", "")),
        host_email=str(raw.get("host_email", "")),
        created_at=str(raw.get("created_at", "")),
    )


def _user(raw: dict[str, Any]) -> ZoomUser:
    return ZoomUser(
        id=str(raw.get("id", "")),
        email=str(raw.get("email", "")),
        first_name=str(raw.get("first_name", "")),
        last_name=str(raw.get("last_name", "")),
        display_name=str(raw.get("display_name", "")),
        type=int(raw.get("type", 1) or 1),
        status=str(raw.get("status", "")),
        timezone=str(raw.get("timezone", "")),
        department=str(raw.get("dept", "")),
        last_login_time=str(raw.get("last_login_time", "")),
    )


def _participant(raw: dict[str, Any]) -> ZoomParticipant:
    return ZoomParticipant(
        id=str(raw.get("id", "")),
        user_id=str(raw.get("user_id", "")),
        name=str(raw.get("name", "")),
        email=str(raw.get("user_email", "") or raw.get("email", "")),
        join_time=str(raw.get("join_time", "")),
        leave_time=str(raw.get("leave_time", "")),
        duration=int(raw.get("duration", 0) or 0),
        status=str(raw.get("status", "")),
    )


def _recording(raw: dict[str, Any]) -> ZoomRecording:
    return ZoomRecording(
        meeting_id=int(raw.get("id", 0) or 0),
        meeting_uuid=str(raw.get("uuid", "")),
        topic=str(raw.get("topic", "")),
        start_time=str(raw.get("start_time", "")),
        duration=int(raw.get("duration", 0) or 0),
        total_size=int(raw.get("total_size", 0) or 0),
        files=[
            ZoomRecordingFile(
                id=str(f.get("id", "")),
                file_type=str(f.get("file_type", "")),
                file_extension=str(f.get("file_extension", "")),
                file_size=int(f.get("file_size", 0) or 0),
                download_url=str(f.get("download_url", "")),
                play_url=str(f.get("play_url", "")),
                recording_type=str(f.get("recording_type", "")),
                status=str(f.get("status", "")),
            )
            for f in raw.get("recording_files") or []
            if isinstance(f, dict)
        ],
    )


def _body(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


