"""Typed response models for the Google Calendar toolset.

The API distinguishes a timed event (``start.dateTime``) from an all-day one
(``start.date``), and the difference matters — an all-day event has no timezone
and comparing it against an instant is a category error. Rather than hide that,
:class:`CalendarEvent` keeps ``all_day`` explicit and normalises both forms into
``start``/``end`` strings, so a caller can tell which it has.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BusyPeriod",
    "CalendarEvent",
    "CalendarSummary",
    "EventAttendee",
]


class EventAttendee(BaseModel):
    """Someone invited to an event."""

    model_config = ConfigDict(frozen=True)

    email: str
    display_name: str = ""
    response_status: str = "needsAction"
    """``needsAction``, ``declined``, ``tentative``, or ``accepted``."""
    optional: bool = False
    organizer: bool = False


class CalendarEvent(BaseModel):
    """One event on a calendar."""

    model_config = ConfigDict(frozen=True)

    id: str
    calendar_id: str = ""
    summary: str = ""
    """The title. Google calls it ``summary``; the UI calls it the event name."""
    description: str = ""
    location: str = ""
    start: str = ""
    """RFC 3339 timestamp, or ``YYYY-MM-DD`` when ``all_day``."""
    end: str = ""
    all_day: bool = False
    time_zone: str = ""
    status: str = "confirmed"
    """``confirmed``, ``tentative``, or ``cancelled``."""
    organizer: str = ""
    attendees: list[EventAttendee] = Field(default_factory=list)
    recurring_event_id: str = ""
    """Set on an instance expanded from a recurring series."""
    hangout_link: str = ""
    url: str = ""
    created: str = ""
    updated: str = ""


class CalendarSummary(BaseModel):
    """A calendar in the authenticated user's list."""

    model_config = ConfigDict(frozen=True)

    id: str
    summary: str = ""
    description: str = ""
    time_zone: str = ""
    primary: bool = False
    access_role: str = ""
    """``owner``, ``writer``, ``reader``, or ``freeBusyReader``."""


class BusyPeriod(BaseModel):
    """An interval in which a calendar is not free."""

    model_config = ConfigDict(frozen=True)

    calendar_id: str
    start: str
    end: str
