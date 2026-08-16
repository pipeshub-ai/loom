"""Zoom steps, for use inside LOOM workflows.

    from loom.toolsets.zoom.tools import zoom_create_meeting

    now = ctx.now()
    meeting = await ctx.step(
        zoom_create_meeting, "Design review",
        start_time=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    await ctx.report(f"Join: {meeting.join_url}")

**Send ``join_url``, never ``start_url``** — the second carries an embedded host
token, and anyone who opens it joins as the host.

**A meeting has two ids.** ``meeting.id`` is a number identifying the *series*;
``meeting.uuid`` identifies one *occurrence*, and is what every past-meeting
tool takes. Passing the id where a uuid belongs answers for whichever
occurrence Zoom guesses at.

Credentials come from a Server-to-Server OAuth app in the environment, or
`loom connect zoom`. Importing this module needs neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.pagination import Results
from loom.toolsets.zoom.models import (
    ZoomMeeting,
    ZoomParticipant,
    ZoomRecording,
    ZoomUser,
)

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

__all__ = [
    "ZOOM_TOOL_DOCS",
    "zoom_create_meeting",
    "zoom_delete_meeting",
    "zoom_delete_recording",
    "zoom_download_recording",
    "zoom_find_user_by_email",
    "zoom_get_meeting",
    "zoom_get_past_meeting",
    "zoom_get_recording",
    "zoom_get_user",
    "zoom_list_meetings",
    "zoom_list_participants",
    "zoom_list_recordings",
    "zoom_list_users",
    "zoom_update_meeting",
]

#: Reads are safe to repeat. Zoom's 4xx failures raise ``NonRetryableError``
#: subclasses, so this stops on a bad meeting id — and on a *daily* rate limit,
#: which no amount of backing off inside one run will clear.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Creating a meeting is not idempotent and Zoom offers no idempotency key: a
#: timeout after the meeting was scheduled is indistinguishable from a failure,
#: and a retry puts a second meeting on the calendar with a different join
#: link. One attempt, and a failure the workflow can decide about. Journaling
#: already prevents a *replay* from rescheduling.
_CREATE = Retry(max_attempts=1)

#: Writes a repeat would merely re-apply — patching a topic to the value it
#: already holds is indistinguishable from patching it once.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def zoom_list_meetings(
    user_id: str = "me",
    meeting_type: str = "scheduled",
    max_results: int = 100,
) -> Results[ZoomMeeting]:
    """List a user's meetings.

    Args:
        user_id: Zoom user id or email. ``"me"`` is the authenticated account.
        meeting_type: ``"scheduled"`` (default), ``"live"``, ``"upcoming"``,
            ``"upcoming_meetings"``, or ``"previous_meetings"``. Note that
            ``"scheduled"`` lists what is *set up*, not what is happening —
            it excludes instant meetings and includes recurring ones whether
            or not an occurrence is near.
        max_results: Maximum meetings to return (default 100).

    Returns:
        Results[ZoomMeeting] with id, uuid, topic, start_time, join_url.
        Check ``.complete`` before reporting a count.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().list_meetings(
        user_id, meeting_type=meeting_type, max_results=max_results
    )


@step(retry=_READ)
async def zoom_get_meeting(meeting_id: int | str) -> ZoomMeeting:
    """Fetch one meeting by its numeric id.

    Args:
        meeting_id: The numeric meeting id (the series), e.g. ``81234567890``.

    Returns:
        ZoomMeeting. Send ``join_url`` to attendees; ``start_url`` is a host
        credential and must not be shared.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().get_meeting(meeting_id)


@step(retry=_CREATE)
async def zoom_create_meeting(
    topic: str,
    start_time: str = "",
    duration: int = 30,
    user_id: str = "me",
    timezone: str = "UTC",
    agenda: str = "",
    password: str = "",
    meeting_type: int = 2,
    settings: dict[str, Any] | None = None,
) -> ZoomMeeting:
    """Schedule a meeting. Not retried — a retry schedules a second one.

    Args:
        topic: Meeting title.
        start_time: ``"YYYY-MM-DDTHH:MM:SSZ"``. Derive it from ``ctx.now()``,
            never ``datetime.now()`` — a wall-clock read schedules a different
            meeting on every replay. Omit for an instant meeting.
        duration: Minutes (default 30). A basic (unlicensed) host is capped at
            40 minutes regardless of what is asked for here.
        user_id: Who hosts it. ``"me"`` is the authenticated account.
        timezone: IANA zone, e.g. ``"Europe/London"``.
        agenda: Longer description.
        password: Join passcode. Zoom generates one when omitted.
        meeting_type: ``2`` scheduled (default), ``1`` instant, ``3`` recurring
            with no fixed time, ``8`` recurring with a fixed time.
        settings: Raw Zoom settings, e.g.
            ``{"join_before_host": True, "waiting_room": False,
            "auto_recording": "cloud"}``.

    Returns:
        The created ZoomMeeting, including id, uuid, join_url and start_url.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().create_meeting(
        topic,
        user_id=user_id,
        start_time=start_time,
        duration=duration,
        timezone=timezone,
        agenda=agenda,
        password=password,
        meeting_type=meeting_type,
        settings=settings,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def zoom_update_meeting(
    meeting_id: int | str, fields: dict[str, Any]
) -> str:
    """Patch a meeting. Only the keys given are touched.

    Args:
        meeting_id: The numeric meeting id.
        fields: Zoom field names to new values, e.g.
            ``{"topic": "Renamed", "duration": 60}``.

    Returns:
        The meeting id. Zoom answers 204 with no body, so there is nothing
        else honest to return — read it back with ``zoom_get_meeting`` if the
        updated object is needed.
    """
    from loom.toolsets.zoom.client import get_default_client

    return str(await get_default_client().update_meeting(meeting_id, fields))


@step(retry=_IDEMPOTENT_WRITE)
async def zoom_delete_meeting(meeting_id: int | str, notify: bool = False) -> str:
    """Cancel a meeting. Not recoverable.

    Args:
        meeting_id: The numeric meeting id.
        notify: Email the registrants (default False, so a bulk cleanup does
            not mail hundreds of people as a side effect of a default).

    Returns:
        The cancelled meeting id, so the journal records what was removed.
    """
    from loom.toolsets.zoom.client import get_default_client

    return str(await get_default_client().delete_meeting(meeting_id, notify=notify))


# ---------------------------------------------------------------------------
# After the meeting
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def zoom_get_past_meeting(meeting_uuid: str) -> dict[str, Any]:
    """Details of one finished meeting occurrence.

    Args:
        meeting_uuid: ``meeting.uuid`` — the *occurrence*, not the numeric id.
            UUIDs containing a slash are encoded correctly for you.

    Returns:
        Dict with uuid, id, topic, start_time, end_time, participants_count,
        total_minutes.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().past_meeting(meeting_uuid)


@step(retry=_READ)
async def zoom_list_participants(
    meeting_uuid: str, max_results: int = 300
) -> Results[ZoomParticipant]:
    """Who attended a finished meeting.

    Args:
        meeting_uuid: ``meeting.uuid`` — the occurrence. Passing the numeric
            ``meeting.id`` here answers for whichever occurrence Zoom picks,
            silently.
        max_results: Maximum participants to return (default 300).

    Returns:
        Results[ZoomParticipant] with name, email, join_time, duration. One
        row per *session*, so someone who dropped and rejoined appears twice —
        group by name or email before counting attendance.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().participants(
        meeting_uuid, max_results=max_results
    )


@step(retry=_READ)
async def zoom_list_recordings(
    user_id: str = "me",
    start: str = "",
    end: str = "",
    max_results: int = 100,
) -> Results[ZoomRecording]:
    """List a user's cloud recordings.

    Args:
        user_id: Zoom user id or email, or ``"me"``.
        start: ``"YYYY-MM-DD"`` lower bound, from ``ctx.now()``.
        end: ``"YYYY-MM-DD"`` upper bound.
        max_results: Maximum recordings to return (default 100).

    Returns:
        Results[ZoomRecording], each with a ``files`` list. Zoom spans **at
        most one month** per query and silently narrows a wider window, so a
        year-long range returns one month and no error — page month by month
        when you need more.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().list_recordings(
        user_id, start=start, end=end, max_results=max_results
    )


@step(retry=_READ)
async def zoom_get_recording(meeting_id: int | str) -> ZoomRecording:
    """Recordings for one meeting.

    Args:
        meeting_id: The numeric meeting id, or a meeting uuid.

    Returns:
        ZoomRecording. Check each file's ``is_ready`` before downloading —
        Zoom lists a file while it is still processing it.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().get_recording(meeting_id)


@step(retry=_READ)
async def zoom_download_recording(download_url: str, filename: str) -> Attachment:
    """Download a recording file as a LOOM Attachment.

    Args:
        download_url: From ``recording.files[i].download_url``. Not public —
            it needs the bearer token, which is why a plain fetch of it does
            not work.
        filename: Name to record on the attachment.

    Returns:
        Attachment. Recordings are large; with ``Runtime(blobs=...)`` the
        payload offloads out of the journal automatically.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().download_recording(download_url, filename)


@step(retry=_IDEMPOTENT_WRITE)
async def zoom_delete_recording(meeting_id: int | str) -> str:
    """Delete a meeting's cloud recordings.

    Goes to the account's trash for 30 days rather than vanishing, but it is
    still worth a ``ctx.wait_for_approval()`` when an agent chose the id.

    Args:
        meeting_id: The numeric meeting id.

    Returns:
        The meeting id, so the journal records what was removed.
    """
    from loom.toolsets.zoom.client import get_default_client

    return str(await get_default_client().delete_recording(meeting_id))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def zoom_list_users(
    status: str = "active", max_results: int = 300
) -> Results[ZoomUser]:
    """List account members.

    Args:
        status: ``"active"`` (default), ``"inactive"``, or ``"pending"``.
        max_results: Maximum users to return (default 300).

    Returns:
        Results[ZoomUser] with id, email, display_name, type.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().list_users(
        status=status, max_results=max_results
    )


@step(retry=_READ)
async def zoom_get_user(user_id: str = "me") -> ZoomUser:
    """Fetch one user.

    Args:
        user_id: Zoom user id or email. ``"me"`` is the authenticated account.

    Returns:
        ZoomUser. ``type`` is 1 basic / 2 licensed — only a licensed host can
        schedule a meeting longer than 40 minutes.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().get_user(user_id)


@step(retry=_READ)
async def zoom_find_user_by_email(email: str) -> ZoomUser | None:
    """Resolve an email address to a Zoom user.

    Call this once to turn a name in a spec into the id that hosting a meeting
    takes, rather than passing an address into every call and finding out at
    the end that nobody matched.

    Args:
        email: The person's email address.

    Returns:
        The ZoomUser, or None when the address has no Zoom account. None is an
        ordinary answer to branch on — create the user, or report it.
    """
    from loom.toolsets.zoom.client import get_default_client

    return await get_default_client().find_user_by_email(email)


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type[BaseModel]) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Zoom Tools

Import: from loom.toolsets.zoom.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials: a Server-to-Server OAuth app via
  ZOOM_ACCOUNT_ID + ZOOM_CLIENT_ID + ZOOM_CLIENT_SECRET
or `loom connect zoom`, or ZOOM_ACCESS_TOKEN.

### TWO IDS, AND THEY ARE NOT INTERCHANGEABLE

  meeting.id    numeric, identifies the SERIES     -> get/update/delete
  meeting.uuid  opaque,  identifies ONE OCCURRENCE -> participants, past

Passing the id where a uuid belongs answers for whichever occurrence Zoom
picks, silently. And send meeting.join_url to people — meeting.start_url
carries an embedded HOST token and must never be posted or logged.

### Meetings

zoom_list_meetings(user_id="me", meeting_type="scheduled", max_results=100)
    -> Results[ZoomMeeting]
  ZoomMeeting fields: {fields(ZoomMeeting)}
  Paged — check .complete.

zoom_get_meeting(meeting_id) -> ZoomMeeting

zoom_create_meeting(topic, start_time="", duration=30, user_id="me",
                    timezone="UTC", agenda="", password="", meeting_type=2,
                    settings=None) -> ZoomMeeting
  NOT retried — a retry schedules a second meeting with a different link.
  start_time is "YYYY-MM-DDTHH:MM:SSZ", built from ctx.now().
    now = ctx.now()
    m = await ctx.step(zoom_create_meeting, "Review",
                       (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       60)

zoom_update_meeting(meeting_id, fields) -> str   (Zoom answers 204; id back)
zoom_delete_meeting(meeting_id, notify=False) -> str

### After the meeting

zoom_get_past_meeting(meeting_uuid) -> dict
zoom_list_participants(meeting_uuid, max_results=300)
    -> Results[ZoomParticipant]
  ZoomParticipant fields: {fields(ZoomParticipant)}
  ONE ROW PER SESSION — someone who rejoined appears twice. Group by email
  before counting attendance.

zoom_list_recordings(user_id="me", start="", end="", max_results=100)
    -> Results[ZoomRecording]
  ZoomRecording fields: {fields(ZoomRecording)}
  Zoom spans at most ONE MONTH per query and silently narrows a wider window.

zoom_get_recording(meeting_id) -> ZoomRecording
zoom_download_recording(download_url, filename) -> Attachment
  Check file.is_ready first — Zoom lists a file while still processing it.
zoom_delete_recording(meeting_id) -> str          (trash, 30 days)

### Users

zoom_list_users(status="active", max_results=300) -> Results[ZoomUser]
  ZoomUser fields: {fields(ZoomUser)}
zoom_get_user(user_id="me") -> ZoomUser
zoom_find_user_by_email(email) -> ZoomUser | None
  THE resolver. None means no Zoom account for that address.

### Notes

- Every timestamp is RFC 3339 and must be derived from ctx.now(); a workflow
  body that reads the wall clock schedules something different on replay.
- A basic (unlicensed) host is capped at 40 minutes whatever duration says.
- Creating a meeting is not retried. Deleting a meeting or a recording is
  worth a ctx.wait_for_approval() when an agent chose the id.
- A daily rate limit raises a NON-retryable error on purpose: it does not
  clear until midnight UTC, so backing off inside the run cannot help.
"""


ZOOM_TOOL_DOCS: str = _build_tool_docs()
