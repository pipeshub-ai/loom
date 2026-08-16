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
    "CalendarAccessRule",
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
    recurrence: list[str] = Field(default_factory=list)
    """RRULE strings on the series master, e.g.
    ``["RRULE:FREQ=WEEKLY;BYDAY=TU"]``. Empty on an expanded instance and on a
    one-off event — so this is how the master is told from its instances."""
    hangout_link: str = ""
    """The Google Meet link, when the event has one. Populated by creating the
    event with ``add_meet=True``, not by writing a link into the location."""
    conference_id: str = ""
    """The Meet meeting code behind ``hangout_link``. This is the value the
    Meet toolset takes, so an event and its recording are joinable."""
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


class CalendarAccessRule(BaseModel):
    """One entry in a calendar's access control list.

    Distinct from :class:`EventAttendee`: an attendee is invited to a single
    event, a rule holds standing access to the whole calendar. Sharing a
    calendar with someone who should have seen one meeting is the mistake this
    separation is meant to make visible.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The rule id, e.g. ``user:ada@example.com``. What unsharing takes."""
    scope_type: str = "user"
    """``user``, ``group``, ``domain``, or ``default`` (i.e. public)."""
    scope_value: str = ""
    """The address or domain. Empty for ``default``, which names nobody."""
    role: str = "reader"
    """``none``, ``freeBusyReader``, ``reader``, ``writer``, or ``owner``."""
