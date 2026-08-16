"""Google Meet REST API v2 client.

Pure httpx. The important thing to know before writing against this API is what
it does **not** do: **it cannot schedule a meeting.**

``spaces.create`` makes a room that exists immediately and forever, with a link
and no start time, no invitees, and no calendar entry. A "meeting at 3pm on
Tuesday with these five people" is a *Calendar event* carrying conference data —
``calendar_create_event(..., add_meet=True)`` — and that is the operation almost
every scheduling request actually wants. Reaching for ``meet_create_space``
instead produces a link nobody is told about, which looks like success right up
until nobody joins.

What this API is for is the other half: the room itself (create, configure, end
a live call) and everything a call leaves behind — who attended, the recording,
the transcript, and the transcript's text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.toolsets.google.http import DEFAULT_TIMEOUT, GoogleSession
from loom.toolsets.google.meet.models import (
    ConferenceRecord,
    MeetParticipant,
    MeetRecording,
    MeetSpace,
    MeetTranscript,
    TranscriptEntry,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.toolsets.google.auth import GoogleAuth

__all__ = ["MeetClient", "flatten_space", "get_default_client"]

API_BASE = "https://meet.googleapis.com/v2"
SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/meetings.space.settings",
]

#: Meet's page ceilings, per endpoint. Asking for more is a 400 rather than a
#: clamp, so these are the values and not merely defaults.
_RECORD_PAGE = 50
_PARTICIPANT_PAGE = 100
_ARTIFACT_PAGE = 10
_ENTRY_PAGE = 100


class MeetClient:
    """Async Google Meet client returning typed models."""

    def __init__(
        self,
        auth: GoogleAuth | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        from loom.toolsets.google.auth import get_default_auth

        self._session = GoogleSession(
            auth or get_default_auth(SCOPES), API_BASE,
            transport=transport, timeout=timeout,
        )

    # -- spaces --------------------------------------------------------------

    async def create_space(self, access_type: str = "") -> MeetSpace:
        """Create a Meet space and return its joinable link.

        A space, not a scheduled meeting: it has a URI and no time, no
        attendees, and no calendar entry. For "book a meeting", create a
        Calendar event with ``add_meet=True`` instead.
        """
        body: dict[str, Any] = {}
        if access_type:
            body["config"] = {"accessType": access_type}
        data = await self._session.post("spaces", body)
        return flatten_space(data)

    async def get_space(self, name: str) -> MeetSpace:
        """Fetch a space by resource name or by meeting code.

        Accepts ``spaces/jQCFfuBOdN5z``, a bare space id, or the
        ``abc-mnop-xyz`` code a human copied out of an invitation — the last is
        the only form anyone has to hand, and requiring the path would mean
        every caller reassembles it.
        """
        return flatten_space(await self._session.get(_space_path(name)))

    async def update_space(
        self, name: str, *, access_type: str = "", entry_point_access: str = ""
    ) -> MeetSpace:
        """Change a space's access configuration.

        The update mask is derived from what was passed. Sending the whole
        config instead would reset ``entryPointAccess`` to its default every
        time someone only meant to change ``accessType``.
        """
        config: dict[str, Any] = {}
        mask: list[str] = []
        if access_type:
            config["accessType"] = access_type
            mask.append("config.accessType")
        if entry_point_access:
            config["entryPointAccess"] = entry_point_access
            mask.append("config.entryPointAccess")
        if not mask:
            raise ValueError(
                "update_space needs access_type or entry_point_access; "
                "a patch with an empty update mask changes nothing."
            )

        data = await self._session.patch(
            _space_path(name), {"config": config}, updateMask=",".join(mask)
        )
        return flatten_space(data)

    async def end_active_conference(self, name: str) -> None:
        """Hang up the call currently in a space. The space itself survives.

        A 404 here means nobody was in the room — the API models "no active
        conference" as a missing sub-resource rather than a no-op.
        """
        await self._session.post(f"{_space_path(name)}:endActiveConference", {})

    # -- conference records --------------------------------------------------

    async def list_conference_records(
        self, filter: str = "", max_results: int = 25
    ) -> Results[ConferenceRecord]:
        """Past and in-progress calls, most recent first.

        ``filter`` uses the API's own syntax:
        ``space.meeting_code = "abc-mnop-xyz"``, or
        ``start_time >= "2026-03-01T00:00:00Z"``. Build any timestamp in it
        from ``ctx.now()`` — a literal ``datetime.now()`` makes the workflow
        return different rows on replay.
        """
        items = await self._session.paginate(
            "conferenceRecords",
            items_key="conferenceRecords",
            limit=max_results,
            params={"filter": filter or None},
            size_param="pageSize",
            page_size=_RECORD_PAGE,
        )
        return items.mapped(_record)

    async def get_conference_record(self, name: str) -> ConferenceRecord:
        return _record(await self._session.get(_path(name, "conferenceRecords")))

    async def list_participants(
        self, conference_record: str, max_results: int = 100
    ) -> Results[MeetParticipant]:
        """Who attended a call, one record per person across every rejoin."""
        items = await self._session.paginate(
            f"{_path(conference_record, 'conferenceRecords')}/participants",
            items_key="participants",
            limit=max_results,
            size_param="pageSize",
            page_size=_PARTICIPANT_PAGE,
        )
        return items.mapped(_participant)

    async def list_recordings(
        self, conference_record: str, max_results: int = 10
    ) -> Results[MeetRecording]:
        """Recordings of a call. Check ``is_ready`` before reaching for Drive."""
        items = await self._session.paginate(
            f"{_path(conference_record, 'conferenceRecords')}/recordings",
            items_key="recordings",
            limit=max_results,
            size_param="pageSize",
            page_size=_ARTIFACT_PAGE,
        )
        return items.mapped(_recording)

    async def list_transcripts(
        self, conference_record: str, max_results: int = 10
    ) -> Results[MeetTranscript]:
        """Transcripts of a call, as Google Docs."""
        items = await self._session.paginate(
            f"{_path(conference_record, 'conferenceRecords')}/transcripts",
            items_key="transcripts",
            limit=max_results,
            size_param="pageSize",
            page_size=_ARTIFACT_PAGE,
        )
        return items.mapped(_transcript)

    async def list_transcript_entries(
        self, transcript: str, max_results: int = 500
    ) -> Results[TranscriptEntry]:
        """The spoken text of a transcript, in order.

        An hour-long call is well past one page, so this is where a truncated
        answer is most likely and least visible: a summary built from the first
        page reads as a summary of the meeting. Check ``.complete``.
        """
        items = await self._session.paginate(
            f"{_transcript_path(transcript)}/entries",
            items_key="transcriptEntries",
            limit=max_results,
            size_param="pageSize",
            page_size=_ENTRY_PAGE,
        )
        return items.mapped(_entry)


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def flatten_space(raw: dict[str, Any]) -> MeetSpace:
    """Flatten a space resource into :class:`MeetSpace`."""
    config = (raw or {}).get("config") or {}
    active = (raw or {}).get("activeConference") or {}
    return MeetSpace(
        name=raw.get("name", ""),
        meeting_uri=raw.get("meetingUri", ""),
        meeting_code=raw.get("meetingCode", ""),
        access_type=config.get("accessType", ""),
        entry_point_access=config.get("entryPointAccess", ""),
        active_conference=active.get("conferenceRecord", ""),
    )


def _record(raw: dict[str, Any]) -> ConferenceRecord:
    return ConferenceRecord(
        name=raw.get("name", ""),
        space=raw.get("space", ""),
        start_time=raw.get("startTime", ""),
        end_time=raw.get("endTime", ""),
        expire_time=raw.get("expireTime", ""),
    )


def _participant(raw: dict[str, Any]) -> MeetParticipant:
    """Collapse the three participant unions into one shape.

    Order matters only in that exactly one is ever present; checking all three
    is what makes an anonymous attendee show up as an attendee rather than as a
    blank row.
    """
    signed_in = raw.get("signedinUser") or {}
    anonymous = raw.get("anonymousUser") or {}
    phone = raw.get("phoneUser") or {}

    if signed_in:
        kind, name, identifier = (
            "signed_in",
            signed_in.get("displayName", ""),
            signed_in.get("user", ""),
        )
    elif phone:
        kind, name, identifier = (
            "phone",
            phone.get("displayName", ""),
            phone.get("displayName", ""),
        )
    else:
        kind, name, identifier = "anonymous", anonymous.get("displayName", ""), ""

    return MeetParticipant(
        name=raw.get("name", ""),
        display_name=name,
        kind=kind,
        identifier=identifier,
        earliest_start_time=raw.get("earliestStartTime", ""),
        latest_end_time=raw.get("latestEndTime", ""),
    )


def _recording(raw: dict[str, Any]) -> MeetRecording:
    destination = raw.get("driveDestination") or {}
    return MeetRecording(
        name=raw.get("name", ""),
        state=raw.get("state", ""),
        drive_file_id=destination.get("file", ""),
        export_uri=destination.get("exportUri", ""),
        start_time=raw.get("startTime", ""),
        end_time=raw.get("endTime", ""),
    )


def _transcript(raw: dict[str, Any]) -> MeetTranscript:
    destination = raw.get("docsDestination") or {}
    return MeetTranscript(
        name=raw.get("name", ""),
        state=raw.get("state", ""),
        document_id=destination.get("document", ""),
        export_uri=destination.get("exportUri", ""),
        start_time=raw.get("startTime", ""),
        end_time=raw.get("endTime", ""),
    )


def _entry(raw: dict[str, Any]) -> TranscriptEntry:
    return TranscriptEntry(
        name=raw.get("name", ""),
        participant=raw.get("participant", ""),
        text=raw.get("text", ""),
        language_code=raw.get("languageCode", ""),
        start_time=raw.get("startTime", ""),
        end_time=raw.get("endTime", ""),
    )


# ---------------------------------------------------------------------------
# Resource names
# ---------------------------------------------------------------------------


def _path(name: str, collection: str) -> str:
    """Normalise a resource name to ``{collection}/{id}``.

    Meet identifies everything by path, and a caller holds whichever half was
    printed to them. Accepting both is a two-line normalisation here or a
    404 naming a URL the caller never built.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"expected a {collection} resource name, got {name!r} — "
            f'e.g. "{collection}/abc123"'
        )
    if name.startswith(f"{collection}/"):
        return name
    if "/" in name:
        raise ValueError(
            f"'{name}' is not a {collection} resource name; expected "
            f'"{collection}/..." or a bare id'
        )
    return f"{collection}/{name}"


def _space_path(name: str) -> str:
    """Normalise a space name, code, or id to ``spaces/{value}``.

    Meeting codes contain hyphens and no slash, so they normalise identically
    to a bare id — which is exactly how the API treats them.
    """
    return _path(name, "spaces")


def _transcript_path(name: str) -> str:
    """A transcript is named under its conference record, so the full path is
    the only form that identifies one."""
    if not isinstance(name, str) or "/transcripts/" not in name:
        raise ValueError(
            f"expected a transcript resource name, got {name!r} — "
            'e.g. "conferenceRecords/abc/transcripts/def", which is the '
            "`name` field of a transcript from meet_list_transcripts"
        )
    return name


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default_client: MeetClient | None = None


def get_default_client() -> MeetClient:
    """Return (or build) the module-level client from environment credentials."""
    global _default_client
    if _default_client is None:
        _default_client = MeetClient()
    return _default_client


def reset_default_client() -> None:
    """Drop the cached client. For tests, and after a credential change."""
    global _default_client
    _default_client = None
