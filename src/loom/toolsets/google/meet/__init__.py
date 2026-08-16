"""Google Meet toolset.

Lazy: importing this package needs no credentials and pulls in no vendor SDK.
The client reads the environment when a tool is first called.
"""

from __future__ import annotations

from loom.toolsets.google.meet.manifest import GOOGLE_MEET_MANIFEST
from loom.toolsets.google.meet.models import (
    ConferenceRecord,
    MeetParticipant,
    MeetRecording,
    MeetSpace,
    MeetTranscript,
    TranscriptEntry,
)

__all__ = [
    "GOOGLE_MEET_MANIFEST",
    "ConferenceRecord",
    "MeetParticipant",
    "MeetRecording",
    "MeetSpace",
    "MeetTranscript",
    "TranscriptEntry",
]
