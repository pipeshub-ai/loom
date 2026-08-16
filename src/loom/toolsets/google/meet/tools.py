"""Google Meet steps, for use inside LOOM workflows.

    from loom.toolsets.google.meet.tools import meet_list_transcript_entries

    said = await ctx.step(meet_list_transcript_entries, transcript.name)
    notes = await ctx.agent(f"Summarise: {[e.text for e in said]}")

**To schedule a meeting, use Calendar, not this toolset.**
``calendar_create_event(..., add_meet=True)`` creates the event *and* the Meet
link, invites the attendees, and puts it in everyone's calendar.
``meet_create_space`` makes a room with a link and nothing else — no time, no
invitees, no calendar entry — which is right for "give me a link now" and wrong
for every request phrased as booking something.

Credentials come from the environment on first call — see
``loom.toolsets.google.auth``. Importing this module needs none.
"""

from __future__ import annotations

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.google.meet.models import (
    ConferenceRecord,
    MeetParticipant,
    MeetRecording,
    MeetSpace,
    MeetTranscript,
    TranscriptEntry,
)
from loom.toolsets.pagination import Results

__all__ = [
    "MEET_TOOL_DOCS",
    "meet_create_space",
    "meet_end_active_conference",
    "meet_get_conference_record",
    "meet_get_space",
    "meet_list_conference_records",
    "meet_list_participants",
    "meet_list_recordings",
    "meet_list_transcript_entries",
    "meet_list_transcripts",
    "meet_update_space",
]

_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Configuration writes are re-appliable — setting an access type to the value
#: it already holds is indistinguishable from setting it once.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)

#: Creating a space is not idempotent: a retry after a post-creation timeout
#: leaves a second room with a second link, and nothing ties the two together.
_CREATE = Retry(max_attempts=1)


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


@step(retry=_CREATE)
async def meet_create_space(access_type: str = "") -> MeetSpace:
    """Create a Meet space and get its joinable link.

    This is an *instant* room, not a booking: no start time, no attendees, no
    calendar entry. To schedule a meeting use
    ``calendar_create_event(..., add_meet=True)``, which does all three.

    Not retried — Meet has no idempotency key, so a retry would leave a second
    room with a different link.

    Args:
        access_type: ``"OPEN"`` (anyone with the link), ``"TRUSTED"``
            (organisation and invited people), or ``"RESTRICTED"`` (invited
            only). Google's account default applies when omitted.

    Returns:
        MeetSpace with name, meeting_uri (the link to send), and meeting_code.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().create_space(access_type)


@step(retry=_READ)
async def meet_get_space(name: str) -> MeetSpace:
    """Fetch a space by resource name, id, or meeting code.

    Args:
        name: ``"spaces/jQCFfuBOdN5z"``, a bare space id, or the
            ``"abc-mnop-xyz"`` code from a meeting link.

    Returns:
        MeetSpace. ``active_conference`` is non-empty only while a call is
        actually happening in the room — it is the way to ask "is this meeting
        live right now".
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().get_space(name)


@step(retry=_IDEMPOTENT_WRITE)
async def meet_update_space(
    name: str, access_type: str = "", entry_point_access: str = ""
) -> MeetSpace:
    """Change who can join a space.

    Args:
        name: Space resource name, id, or meeting code.
        access_type: ``"OPEN"``, ``"TRUSTED"``, or ``"RESTRICTED"``.
        entry_point_access: ``"ALL"`` or ``"CREATOR_APP_ONLY"``.

    Returns:
        The updated MeetSpace. Only the settings passed are touched.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().update_space(
        name, access_type=access_type, entry_point_access=entry_point_access
    )


@step(retry=_IDEMPOTENT_WRITE)
async def meet_end_active_conference(name: str) -> str:
    """Hang up the call currently happening in a space.

    Ends the call for everyone in it. The space survives and can be rejoined,
    so this is not destructive — but it does interrupt people, and is worth a
    ``ctx.wait_for_approval()`` when an agent chose the space.

    Args:
        name: Space resource name, id, or meeting code.

    Returns:
        The space name, so the journal records which room was cleared.
    """
    from loom.toolsets.google.meet.client import get_default_client

    await get_default_client().end_active_conference(name)
    return name


# ---------------------------------------------------------------------------
# Conference records
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def meet_list_conference_records(
    filter: str = "", max_results: int = 25
) -> Results[ConferenceRecord]:
    """List past and in-progress calls.

    Args:
        filter: Meet filter syntax, e.g.
            ``'space.meeting_code = "abc-mnop-xyz"'`` or
            ``'start_time >= "2026-03-01T00:00:00Z"'``. Build timestamps from
            ``ctx.now()``, never ``datetime.now()``.
        max_results: Maximum records to return (default 25).

    Returns:
        Results[ConferenceRecord] with name, space, start_time, end_time.
        ``name`` is what participants, recordings, and transcripts list under.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().list_conference_records(filter, max_results)


@step(retry=_READ)
async def meet_get_conference_record(name: str) -> ConferenceRecord:
    """Fetch one conference record.

    Args:
        name: ``"conferenceRecords/{id}"``, or a bare id.

    Returns:
        ConferenceRecord. ``in_progress`` is True while the call is still live,
        which is when recordings and transcripts do not exist yet.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().get_conference_record(name)


@step(retry=_READ)
async def meet_list_participants(
    conference_record: str, max_results: int = 100
) -> Results[MeetParticipant]:
    """Who attended a call.

    Args:
        conference_record: ``"conferenceRecords/{id}"`` from
            ``meet_list_conference_records``.
        max_results: Maximum participants to return (default 100).

    Returns:
        Results[MeetParticipant] with display_name, kind, identifier, and the
        earliest/latest times. ``kind`` is ``signed_in``, ``anonymous``, or
        ``phone`` — only the first can be matched against a directory user, so
        do not assume an attendance check can identify everyone.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().list_participants(
        conference_record, max_results
    )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def meet_list_recordings(
    conference_record: str, max_results: int = 10
) -> Results[MeetRecording]:
    """Recordings of a call.

    Args:
        conference_record: ``"conferenceRecords/{id}"``.
        max_results: Maximum recordings to return (default 10).

    Returns:
        Results[MeetRecording]. Check ``is_ready`` before using
        ``drive_file_id``: Meet reports a recording as soon as it stops, and
        the Drive file appears only once the state is ``FILE_GENERATED``.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().list_recordings(conference_record, max_results)


@step(retry=_READ)
async def meet_list_transcripts(
    conference_record: str, max_results: int = 10
) -> Results[MeetTranscript]:
    """Transcripts of a call.

    Args:
        conference_record: ``"conferenceRecords/{id}"``.
        max_results: Maximum transcripts to return (default 10).

    Returns:
        Results[MeetTranscript] with name, state, document_id. The transcript
        is a Google Doc, so ``drive_export_file`` reads it — but
        ``meet_list_transcript_entries`` gives the text already structured by
        speaker, which is usually what a workflow wants.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().list_transcripts(conference_record, max_results)


@step(retry=_READ)
async def meet_list_transcript_entries(
    transcript: str, max_results: int = 500
) -> Results[TranscriptEntry]:
    """The spoken text of a transcript, in order.

    Args:
        transcript: Full transcript resource name,
            ``"conferenceRecords/{id}/transcripts/{id}"`` — the ``name`` field
            from ``meet_list_transcripts``.
        max_results: Maximum entries to return (default 500).

    Returns:
        Results[TranscriptEntry] with participant, text, start_time. An
        hour-long meeting runs to hundreds of entries, so check ``.complete``
        before summarising — a summary of the first page reads exactly like a
        summary of the meeting.
    """
    from loom.toolsets.google.meet.client import get_default_client

    return await get_default_client().list_transcript_entries(transcript, max_results)


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type[BaseModel]) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Google Meet Tools

Import: from loom.toolsets.google.meet.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  GOOGLE_ACCESS_TOKEN, or
  GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

### SCHEDULING A MEETING IS A CALENDAR OPERATION

  from loom.toolsets.google.calendar.tools import calendar_create_event
  ev = await ctx.step(calendar_create_event, "Review", start, end,
                      attendees=["a@b.com"], add_meet=True)
  ev.hangout_link   # the Meet link

meet_create_space makes a room with a link and NOTHING else — no time, no
attendees, no calendar entry. Use it only for "give me a link right now".

### Spaces

meet_create_space(access_type="") -> MeetSpace
  MeetSpace fields: {fields(MeetSpace)}
  access_type: "OPEN" | "TRUSTED" | "RESTRICTED"

meet_get_space(name) -> MeetSpace
  Takes "spaces/abc123", a bare id, or the "abc-mnop-xyz" meeting code.
  space.active_conference is non-empty only while a call is live.

meet_update_space(name, access_type="", entry_point_access="") -> MeetSpace
meet_end_active_conference(name) -> str    (hangs up on everyone in the room)

### After the meeting

meet_list_conference_records(filter="", max_results=25)
    -> Results[ConferenceRecord]
  ConferenceRecord fields: {fields(ConferenceRecord)}
  filter: 'space.meeting_code = "abc-mnop-xyz"'
          'start_time >= "2026-03-01T00:00:00Z"'   (build from ctx.now())

meet_get_conference_record(name) -> ConferenceRecord

meet_list_participants(conference_record, max_results=100)
    -> Results[MeetParticipant]
  MeetParticipant fields: {fields(MeetParticipant)}
  kind is signed_in | anonymous | phone — only signed_in maps to a directory
  user, so an attendance check cannot identify everyone.

meet_list_recordings(conference_record, max_results=10)
    -> Results[MeetRecording]
  MeetRecording fields: {fields(MeetRecording)}
  Check recording.is_ready before using drive_file_id — the Drive file exists
  only once state == "FILE_GENERATED".

meet_list_transcripts(conference_record, max_results=10)
    -> Results[MeetTranscript]
  MeetTranscript fields: {fields(MeetTranscript)}

meet_list_transcript_entries(transcript, max_results=500)
    -> Results[TranscriptEntry]
  TranscriptEntry fields: {fields(TranscriptEntry)}
  Takes the FULL transcript name: "conferenceRecords/x/transcripts/y".
    said = await ctx.step(meet_list_transcript_entries, t.name)
    if not said.complete:
        await ctx.report(f"transcript truncated: {{said.summary()}}")

### Notes

- Every Meet resource is named by a path, not an id, and that path is what the
  next call takes. Keep resource.name whole.
- Recordings and transcripts do not exist while a call is in progress
  (record.in_progress is True) and are not instant once it ends.
- All the list tools are paged and return Results — check .complete before
  reporting a count or summarising, especially transcript entries.
- Recordings land in Drive and transcripts land in Google Docs, so the Drive
  toolset reads them: drive_download_file for a recording, drive_export_file
  for a transcript doc.
"""


MEET_TOOL_DOCS: str = _build_tool_docs()
