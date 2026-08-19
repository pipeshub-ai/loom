"""Google Meet toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from loom.toolsets.google.meet.models import (
    ConferenceRecord,
    MeetParticipant,
    MeetRecording,
    MeetSpace,
    MeetTranscript,
    TranscriptEntry,
)
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GOOGLE_MEET_MANIFEST"]

_space = MeetSpace.model_json_schema()
_record = ConferenceRecord.model_json_schema()
_space_name = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": 'Resource name, bare id, or "abc-mnop-xyz" code.',
        }
    },
    "required": ["name"],
}


def _under_record(limit: int) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "conference_record": {
                "type": "string",
                "description": 'e.g. "conferenceRecords/abc123".',
            },
            "max_results": {"type": "integer", "default": limit},
        },
        "required": ["conference_record"],
    }


GOOGLE_MEET_MANIFEST = ToolsetManifest(
    id="google_meet",
    version="1.0.0",
    provider="loom",
    summary=(
        "Google Meet — create meeting rooms, and read attendance, recordings "
        "and transcripts after a call."
    ),
    description=(
        "Google Meet API v2 over REST. Creates and configures Meet spaces, "
        "ends a live call, and reads what a meeting left behind: who attended "
        "(including anonymous and dial-in participants), the recordings and "
        "their Drive file ids, the transcripts, and the transcript text by "
        "speaker. Note that this API cannot *schedule* anything — a meeting at "
        "a time with invitees is a Calendar event created with add_meet=True, "
        "and a space created here has a link but no time, no attendees and no "
        "calendar entry."
    ),
    base_url="https://meet.googleapis.com/v2",
    auth={
        "type": "oauth2",
        "fields": [
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
        ],
        "token_url": "https://oauth2.googleapis.com/token",
    },
    tools_module="loom.toolsets.google.meet.tools",
    egress_hosts=["meet.googleapis.com", "oauth2.googleapis.com"],
    rate_limits={
        "model": (
            "per-project quota units configured in the Google Cloud console; "
            "no fixed per-second rate is published per method"
        ),
    },
    groups={
        "spaces": [
            OperationSpec(
                id="spaces.create",
                function="meet_create_space",
                summary="Create a Meet space and get its joinable link.",
                description=(
                    "An instant room: a link with no start time, no attendees "
                    "and no calendar entry. To schedule a meeting, use the "
                    "Calendar toolset's events.create with add_meet=True."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "access_type": {
                            "type": "string",
                            "enum": ["OPEN", "TRUSTED", "RESTRICTED"],
                        }
                    },
                },
                output_schema=_space,
                scopes=["https://www.googleapis.com/auth/meetings.space.created"],
            ),
            OperationSpec(
                id="spaces.get",
                function="meet_get_space",
                summary="Fetch a space by resource name, id, or meeting code.",
                effect=EffectClass.READ,
                input_schema=_space_name,
                output_schema=_space,
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="spaces.update",
                function="meet_update_space",
                summary="Change who can join a space.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "access_type": {
                            "type": "string",
                            "enum": ["OPEN", "TRUSTED", "RESTRICTED"],
                        },
                        "entry_point_access": {
                            "type": "string",
                            "enum": ["ALL", "CREATOR_APP_ONLY"],
                        },
                    },
                    "required": ["name"],
                },
                output_schema=_space,
                scopes=["https://www.googleapis.com/auth/meetings.space.settings"],
                idempotent=True,
            ),
            OperationSpec(
                id="spaces.end_active_conference",
                idempotent=True,
                function="meet_end_active_conference",
                summary="Hang up the call currently happening in a space.",
                description=(
                    "Ends the call for everyone in it. The space itself "
                    "survives and can be rejoined."
                ),
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_space_name,
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/meetings.space.created"],
            ),
        ],
        "conferences": [
            OperationSpec(
                id="conferences.list",
                function="meet_list_conference_records",
                summary="List past and in-progress calls.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": (
                                'e.g. space.meeting_code = "abc-mnop-xyz" or '
                                'start_time >= "2026-03-01T00:00:00Z"'
                            ),
                        },
                        "max_results": {"type": "integer", "default": 25},
                    },
                },
                output_schema={"type": "array", "items": _record},
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="conferences.get",
                function="meet_get_conference_record",
                summary="Fetch one conference record.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                output_schema=_record,
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="conferences.list_participants",
                function="meet_list_participants",
                summary="Who attended a call.",
                description=(
                    "kind is signed_in, anonymous, or phone; only the first "
                    "maps to a directory user."
                ),
                effect=EffectClass.READ,
                input_schema=_under_record(100),
                output_schema={
                    "type": "array",
                    "items": MeetParticipant.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                pagination=True,
                idempotent=True,
            ),
        ],
        "artifacts": [
            OperationSpec(
                id="artifacts.list_recordings",
                function="meet_list_recordings",
                summary="Recordings of a call, with their Drive file ids.",
                description=(
                    "The Drive file exists only once state is FILE_GENERATED; "
                    "check is_ready before reaching for it."
                ),
                effect=EffectClass.READ,
                input_schema=_under_record(10),
                output_schema={
                    "type": "array",
                    "items": MeetRecording.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="artifacts.list_transcripts",
                function="meet_list_transcripts",
                summary="Transcripts of a call, as Google Docs.",
                effect=EffectClass.READ,
                input_schema=_under_record(10),
                output_schema={
                    "type": "array",
                    "items": MeetTranscript.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="artifacts.list_transcript_entries",
                function="meet_list_transcript_entries",
                summary="The spoken text of a transcript, in order.",
                description=(
                    "Takes the full transcript resource name. An hour-long "
                    "meeting runs to hundreds of entries — check .complete "
                    "before summarising."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "transcript": {
                            "type": "string",
                            "description": (
                                'Full name, e.g. "conferenceRecords/abc/'
                                'transcripts/def".'
                            ),
                        },
                        "max_results": {"type": "integer", "default": 500},
                    },
                    "required": ["transcript"],
                },
                output_schema={
                    "type": "array",
                    "items": TranscriptEntry.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
                pagination=True,
                idempotent=True,
            ),
        ],
    },
)
