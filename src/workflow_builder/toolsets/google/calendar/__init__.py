"""Google Calendar toolset.

Lazy: importing this package needs no credentials and pulls in no vendor SDK.
The client reads the environment when a tool is first called.
"""

from __future__ import annotations

from workflow_builder.toolsets.google.calendar.manifest import (
    GOOGLE_CALENDAR_MANIFEST,
)
from workflow_builder.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    CalendarSummary,
    EventAttendee,
)

__all__ = [
    "GOOGLE_CALENDAR_MANIFEST",
    "BusyPeriod",
    "CalendarEvent",
    "CalendarSummary",
    "EventAttendee",
]
