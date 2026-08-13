"""Google Calendar steps, for use inside LOOM workflows.

    from workflow_builder.toolsets.google.calendar.tools import calendar_list_events

    now = ctx.now()
    today = await ctx.step(
        calendar_list_events, "primary", now.isoformat(),
        (now + timedelta(days=1)).isoformat(),
    )

Time windows must come from ``ctx.now()``, never ``datetime.now()`` — a body
that reads the wall clock returns different events on replay.
"""

from __future__ import annotations

from workflow_builder import Retry, step
from workflow_builder.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    CalendarSummary,
)

__all__ = [
    "CALENDAR_TOOL_DOCS",
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_find_busy_periods",
    "calendar_get_event",
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_quick_add_event",
    "calendar_update_event",
]

_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Creating an event twice leaves two events, and Calendar has no idempotency
#: key either. Unlike a send, though, a duplicate here is visible and
#: deletable — so one retry is a reasonable trade against a transient 503.
_WRITE = Retry(max_attempts=2, initial_delay=1.0)


@step(retry=_READ)
async def calendar_list_calendars() -> list[CalendarSummary]:
    """List the calendars this account can see.

    Returns:
        List of CalendarSummary with id, summary, time_zone, primary,
        access_role. The id is what every other tool takes as calendar_id.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().list_calendars()


@step(retry=_READ)
async def calendar_list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    query: str = "",
    max_results: int = 25,
) -> list[CalendarEvent]:
    """List events in a time window, recurring series expanded to instances.

    Args:
        calendar_id: Calendar id, or ``"primary"`` for the default one.
        time_min: RFC 3339 start of the window, e.g. ``ctx.now().isoformat()``.
        time_max: RFC 3339 end of the window.
        query: Free-text filter over summary, description, location, attendees.
        max_results: Maximum events to return (default 25).

    Returns:
        List of CalendarEvent ordered by start time, with id, summary, start,
        end, all_day, location, attendees, organizer, url.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().list_events(
        calendar_id, time_min, time_max, query, max_results
    )


@step(retry=_READ)
async def calendar_get_event(
    event_id: str, calendar_id: str = "primary"
) -> CalendarEvent:
    """Fetch a single event by id.

    Args:
        event_id: Event id.
        calendar_id: Calendar the event lives on.

    Returns:
        CalendarEvent.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().get_event(event_id, calendar_id)


@step(retry=_WRITE)
async def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    time_zone: str = "",
    all_day: bool = False,
    send_updates: str = "none",
) -> CalendarEvent:
    """Create a calendar event.

    Args:
        summary: Event title.
        start: RFC 3339 timestamp, or ``YYYY-MM-DD`` when all_day.
        end: RFC 3339 timestamp, or ``YYYY-MM-DD`` when all_day.
        calendar_id: Calendar to create it on.
        description: Longer description.
        location: Free-text location.
        attendees: Email addresses to invite.
        time_zone: IANA zone, e.g. ``"Europe/London"``. Ignored for all-day.
        all_day: Treat start/end as dates rather than instants.
        send_updates: ``"none"`` (default), ``"all"``, or ``"externalOnly"`` —
            whether Google emails the attendees. Defaults to none so a bulk
            workflow does not mail everyone as a side effect.

    Returns:
        The created CalendarEvent, including its id and url.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().create_event(
        summary,
        start,
        end,
        calendar_id=calendar_id,
        description=description,
        location=location,
        attendees=attendees,
        time_zone=time_zone,
        all_day=all_day,
        send_updates=send_updates,
    )


@step(retry=_WRITE)
async def calendar_update_event(
    event_id: str,
    fields: dict,
    calendar_id: str = "primary",
    send_updates: str = "none",
) -> CalendarEvent:
    """Patch fields on an existing event.

    Args:
        event_id: Event id.
        fields: Calendar API field names to new values, e.g.
            ``{"location": "Room 4"}`` or
            ``{"start": {"dateTime": "2026-01-01T10:00:00Z"}}``.
        calendar_id: Calendar the event lives on.
        send_updates: Whether to notify attendees. See create.

    Returns:
        The updated CalendarEvent.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().update_event(
        event_id, fields, calendar_id=calendar_id, send_updates=send_updates
    )


@step(retry=_WRITE)
async def calendar_delete_event(
    event_id: str,
    calendar_id: str = "primary",
    send_updates: str = "none",
) -> str:
    """Delete an event. Not recoverable.

    Args:
        event_id: Event id.
        calendar_id: Calendar the event lives on.
        send_updates: Whether to notify attendees of the cancellation.

    Returns:
        The deleted event id, so the journal records what was removed.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    await get_default_client().delete_event(
        event_id, calendar_id=calendar_id, send_updates=send_updates
    )
    return event_id


@step(retry=_WRITE)
async def calendar_quick_add_event(
    text: str, calendar_id: str = "primary"
) -> CalendarEvent:
    """Create an event from a natural-language phrase, parsed by Google.

    Args:
        text: e.g. ``"Lunch with Bob tomorrow at 1pm"``.
        calendar_id: Calendar to create it on.

    Returns:
        The created CalendarEvent — check its start/end, since the parse is
        Google's and is not always what the phrase implied.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().quick_add_event(text, calendar_id)


@step(retry=_READ)
async def calendar_find_busy_periods(
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
) -> list[BusyPeriod]:
    """Return busy intervals across calendars — the basis for finding a slot.

    Args:
        time_min: RFC 3339 start of the window.
        time_max: RFC 3339 end of the window.
        calendar_ids: Calendars to check (default ``["primary"]``).

    Returns:
        List of BusyPeriod with calendar_id, start, end. Free time is the gaps
        between them; compute that in the workflow body, which is deterministic.
    """
    from workflow_builder.toolsets.google.calendar.client import get_default_client

    return await get_default_client().free_busy(time_min, time_max, calendar_ids)


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Google Calendar Tools

Import: from workflow_builder.toolsets.google.calendar.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  GOOGLE_ACCESS_TOKEN, or
  GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

All tools return typed Pydantic models. Use attribute access:
event.summary, event.start, event.attendees[0].email.

### Tools

calendar_list_calendars() -> list[CalendarSummary]
  CalendarSummary fields: {fields(CalendarSummary)}

calendar_list_events(calendar_id="primary", time_min="", time_max="",
                     query="", max_results=25) -> list[CalendarEvent]
  Recurring series are expanded into instances, ordered by start.
  CalendarEvent fields: {fields(CalendarEvent)}
    now = ctx.now()
    today = await ctx.step(calendar_list_events, "primary",
                           now.isoformat(),
                           (now + timedelta(days=1)).isoformat())

calendar_get_event(event_id, calendar_id="primary") -> CalendarEvent

calendar_create_event(summary, start, end, calendar_id="primary",
                      description="", location="", attendees=None,
                      time_zone="", all_day=False, send_updates="none")
    -> CalendarEvent
  send_updates defaults to "none" — pass "all" to actually email attendees.
    ev = await ctx.step(calendar_create_event, "Standup",
                        "2026-03-02T09:00:00Z", "2026-03-02T09:15:00Z")

calendar_update_event(event_id, fields, calendar_id="primary",
                      send_updates="none") -> CalendarEvent
    await ctx.step(calendar_update_event, ev.id, {{"location": "Room 4"}})

calendar_delete_event(event_id, calendar_id="primary",
                      send_updates="none") -> str
  Not recoverable. Returns the deleted id.

calendar_quick_add_event(text, calendar_id="primary") -> CalendarEvent
    await ctx.step(calendar_quick_add_event, "Lunch with Bob tomorrow 1pm")

calendar_find_busy_periods(time_min, time_max, calendar_ids=None)
    -> list[BusyPeriod]
  BusyPeriod fields: {fields(BusyPeriod)}
  Free slots are the gaps — compute them in the workflow body.

### Notes

- Every timestamp is RFC 3339. Derive windows from ctx.now(), never
  datetime.now(): a workflow body must be deterministic across replays.
- An all-day event has date-only start/end and all_day=True; do not compare
  it against an instant without checking that flag.
- Deleting and inviting are the operations worth a ctx.wait_for_approval()
  when an agent chose the arguments.
"""


CALENDAR_TOOL_DOCS: str = _build_tool_docs()
