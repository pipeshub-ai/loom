"""Typed response models for the Google Meet toolset.

Two things about the Meet API shape the models here.

**Every resource is named by a path, not an id.** A space is
``spaces/jQCFfuBOdN5z``, a participant is
``conferenceRecords/xyz/participants/abc``. Those strings are what every other
call takes, so :attr:`MeetSpace.name` and friends keep the whole path rather
than the trailing segment — splitting it would produce a value nothing accepts.

**A participant is a union.** The API returns exactly one of ``signedinUser``,
``anonymousUser``, or ``phoneUser``, each with a different shape, and a caller
that reaches for the wrong one gets ``None`` rather than an error.
:class:`MeetParticipant` collapses the three into ``display_name`` plus a
``kind``, so "who attended" is one field and the distinction is still there for
anyone who needs it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ConferenceRecord",
    "MeetParticipant",
    "MeetRecording",
    "MeetSpace",
    "MeetTranscript",
    "TranscriptEntry",
]


class MeetSpace(BaseModel):
    """A Meet space — the durable room, not one call held in it.

    A space is reusable: the same ``meeting_uri`` works every time, and each
    use produces a separate :class:`ConferenceRecord`.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """Resource path, ``spaces/{space_id}``. What every other space call takes."""
    meeting_uri: str = ""
    """The ``https://meet.google.com/...`` link to send to a human."""
    meeting_code: str = ""
    """The ``abc-mnop-xyz`` code. Usable in place of the id when fetching."""
    access_type: str = ""
    """``OPEN`` (anyone with the link), ``TRUSTED`` (organisation and invitees),
    or ``RESTRICTED`` (invited only)."""
    entry_point_access: str = ""
    """``ALL`` or ``CREATOR_APP_ONLY``."""
    active_conference: str = ""
    """Resource path of the call happening right now, or ``""`` if nobody is in
    the room. This is the only field that says whether a meeting is live."""


class ConferenceRecord(BaseModel):
    """One completed (or in-progress) call held in a space."""

    model_config = ConfigDict(frozen=True)

    name: str
    """``conferenceRecords/{id}`` — what participants, recordings, and
    transcripts are listed under."""
    space: str = ""
    """Resource path of the space this was held in."""
    start_time: str = ""
    end_time: str = ""
    """Empty while the call is still in progress."""
    expire_time: str = ""
    """When Meet discards this record. Artifacts do not outlive it."""

    @property
    def in_progress(self) -> bool:
        return not self.end_time


class MeetParticipant(BaseModel):
    """Someone who was in a call, whichever way they joined."""

    model_config = ConfigDict(frozen=True)

    name: str
    """``conferenceRecords/{id}/participants/{id}``."""
    display_name: str = ""
    """The name shown in the call. Empty for a phone participant that Meet
    could not attribute — a number is in ``identifier`` instead."""
    kind: str = "signed_in"
    """``signed_in``, ``anonymous``, or ``phone``. An anonymous or phone
    participant cannot be matched to a directory user, so a workflow that
    correlates attendance against invitees must handle all three."""
    identifier: str = ""
    """``users/{id}`` for a signed-in participant; the phone number for a
    dial-in. Empty for an anonymous one, which has no stable identity at all."""
    earliest_start_time: str = ""
    latest_end_time: str = ""
    """Across every session — someone who dropped and rejoined has one
    participant record spanning both."""


class MeetRecording(BaseModel):
    """A recording of a call, stored in Drive."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: str = ""
    """``STARTED``, ``ENDED``, or ``FILE_GENERATED``. Only the last means the
    Drive file exists — acting on an ``ENDED`` recording finds nothing there."""
    drive_file_id: str = ""
    """Drive file id. Pass it straight to ``drive_download_file``."""
    export_uri: str = ""
    start_time: str = ""
    end_time: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "FILE_GENERATED" and bool(self.drive_file_id)


class MeetTranscript(BaseModel):
    """A transcript of a call, stored as a Google Doc."""

    model_config = ConfigDict(frozen=True)

    name: str
    """``conferenceRecords/{id}/transcripts/{id}`` — what transcript entries
    are listed under."""
    state: str = ""
    """``STARTED``, ``ENDED``, or ``FILE_GENERATED``."""
    document_id: str = ""
    """Google Docs id. It is a Doc, so ``drive_export_file`` reads it and
    ``drive_download_file`` cannot."""
    export_uri: str = ""
    start_time: str = ""
    end_time: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "FILE_GENERATED"


class TranscriptEntry(BaseModel):
    """One person's uninterrupted speech within a transcript."""

    model_config = ConfigDict(frozen=True)

    name: str
    participant: str = ""
    """Resource path of the speaker — join against ``MeetParticipant.name`` to
    get a display name, which is not carried here."""
    text: str = ""
    language_code: str = ""
    start_time: str = ""
    end_time: str = ""
