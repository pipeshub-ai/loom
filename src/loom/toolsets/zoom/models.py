"""Typed response models for the Zoom toolset.

One thing to understand before writing anything against these: **a meeting has
two identifiers, and they are not interchangeable.**

``id``
    A number, e.g. ``81234567890``. It identifies the *series* — a recurring
    meeting keeps one id for every occurrence — and it is what a person reads
    off an invitation. Reusable, and reused: a deleted meeting's id can come
    back on a new one.
``uuid``
    An opaque base64 string, e.g. ``"aDYlohsHRtCd4ii1uC2+hA=="``. It identifies
    **one occurrence**, and it is what every past-meeting endpoint takes.

So "who attended the standup" is a question about a ``uuid``, and passing the
``id`` there answers for whichever occurrence Zoom guesses at — usually the
most recent, silently. Both are on :class:`ZoomMeeting` for that reason, and
the tools say which they take.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ZoomMeeting",
    "ZoomParticipant",
    "ZoomRecording",
    "ZoomRecordingFile",
    "ZoomUser",
]


class ZoomMeeting(BaseModel):
    """A scheduled or in-progress meeting."""

    model_config = ConfigDict(frozen=True)

    id: int = 0
    """The numeric meeting id — the *series*. See the module docstring."""
    uuid: str = ""
    """This occurrence. What past-meeting and recording endpoints take."""
    topic: str = ""
    agenda: str = ""
    type: int = 2
    """``1`` instant, ``2`` scheduled, ``3`` recurring with no fixed time,
    ``8`` recurring with a fixed time."""
    status: str = ""
    start_time: str = ""
    """RFC 3339, in UTC unless ``timezone`` says otherwise. Compare against
    ``ctx.now()``, never ``datetime.now()``."""
    duration: int = 0
    """Minutes."""
    timezone: str = ""
    join_url: str = Field(
        default="", description="The link to send to attendees. Safe to share."
    )
    start_url: str = Field(
        default="",
        # Deliberately a Field description rather than an attribute docstring:
        # only this reaches ``model_json_schema()``, which is what the manifest
        # publishes and what the coding agent reads. A warning that lives only
        # in the source is a warning the agent generating the code never sees.
        description=(
            "HOST CREDENTIAL — carries an embedded host token. Anyone who "
            "opens it joins as the host and can admit people, record, and end "
            "the call. Never post it to a channel, log it, or return it from a "
            "workflow; send join_url instead."
        ),
    )
    password: str = ""
    host_id: str = ""
    host_email: str = ""
    created_at: str = ""


class ZoomUser(BaseModel):
    """An account member."""

    model_config = ConfigDict(frozen=True)

    id: str
    """Zoom's user id. ``"me"`` is accepted anywhere this is taken."""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    display_name: str = ""
    type: int = 1
    """``1`` basic, ``2`` licensed, ``3`` on-prem. Only a licensed host can
    schedule a meeting longer than 40 minutes."""
    status: str = ""
    """``active``, ``inactive``, or ``pending``."""
    timezone: str = ""
    department: str = ""
    last_login_time: str = ""


class ZoomParticipant(BaseModel):
    """Someone who was in a past meeting."""

    model_config = ConfigDict(frozen=True)

    id: str = ""
    """Empty for a guest who was not signed in — which is common, and why
    attendance cannot always be matched back to a directory user."""
    user_id: str = ""
    name: str = ""
    email: str = ""
    join_time: str = ""
    leave_time: str = ""
    duration: int = 0
    """Seconds in the call. Someone who dropped and rejoined appears **once
    per session**, not once per person, so summing by name is how a report
    ends up double-counting."""
    status: str = ""


class ZoomRecordingFile(BaseModel):
    """One artifact of a recorded meeting — video, audio, chat, or transcript."""

    model_config = ConfigDict(frozen=True)

    id: str = ""
    file_type: str = ""
    """``MP4``, ``M4A``, ``TIMELINE``, ``TRANSCRIPT``, ``CHAT``, ``CC``."""
    file_extension: str = ""
    file_size: int = 0
    download_url: str = ""
    """Needs the bearer token, or a ``?access_token=`` appended. Not public."""
    play_url: str = ""
    recording_type: str = ""
    status: str = ""
    """``completed`` when the file is ready. Anything else means Zoom is still
    processing it and downloading now returns nothing useful."""

    @property
    def is_ready(self) -> bool:
        return self.status == "completed" and bool(self.download_url)


class ZoomRecording(BaseModel):
    """Everything recorded for one meeting occurrence."""

    model_config = ConfigDict(frozen=True)

    meeting_id: int = 0
    meeting_uuid: str = ""
    topic: str = ""
    start_time: str = ""
    duration: int = 0
    total_size: int = 0
    files: list[ZoomRecordingFile] = Field(default_factory=list)
