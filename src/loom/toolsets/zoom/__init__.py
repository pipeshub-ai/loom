"""Zoom toolset.

Lazy: importing this package needs no credentials and pulls in no vendor SDK.
The client reads the environment, or the run's connected credential, when a
tool is first called.
"""

from __future__ import annotations

from loom.toolsets.zoom.errors import (
    ZoomAPIError,
    ZoomAuthError,
    ZoomDailyLimitReached,
    ZoomPermanentError,
    ZoomRateLimited,
)
from loom.toolsets.zoom.manifest import ZOOM_MANIFEST
from loom.toolsets.zoom.models import (
    ZoomMeeting,
    ZoomParticipant,
    ZoomRecording,
    ZoomRecordingFile,
    ZoomUser,
)

__all__ = [
    "ZOOM_MANIFEST",
    "ZoomAPIError",
    "ZoomAuthError",
    "ZoomDailyLimitReached",
    "ZoomMeeting",
    "ZoomParticipant",
    "ZoomPermanentError",
    "ZoomRateLimited",
    "ZoomRecording",
    "ZoomRecordingFile",
    "ZoomUser",
]
