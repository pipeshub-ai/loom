"""Google Calendar steps, for use inside LOOM workflows.

    from loom.toolsets.google.calendar.tools import calendar_list_events

    now = ctx.now()
    today = await ctx.step(
        calendar_list_events, "primary", now.isoformat(),
        (now + timedelta(days=1)).isoformat(),
    )

Time windows must come from ``ctx.now()``, never ``datetime.now()`` — a body
that reads the wall clock returns different events on replay.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.google.calendar.client import CalendarClient
from loom.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarAccessRule,
    CalendarEvent,
    CalendarSummary,
)
from loom.toolsets.pagination import Results

__all__ = [
    "CALENDAR_TOOL_DOCS",
    "calendar_add_meet_link",
    "calendar_create_calendar",
    "calendar_create_event",
    "calendar_delete_calendar",
    "calendar_delete_event",
    "calendar_find_busy_periods",
    "calendar_find_calendar",
    "calendar_get_calendar",
    "calendar_get_event",
    "calendar_list_acl",
    "calendar_list_calendars",
    "calendar_list_event_instances",
    "calendar_list_events",
    "calendar_move_event",
    "calendar_quick_add_event",
    "calendar_respond_to_event",
    "calendar_share_calendar",
    "calendar_unshare_calendar",
    "calendar_update_event",
]

_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Creating an event twice leaves two events, and Calendar has no idempotency
#: key either. Unlike a send, though, a duplicate here is visible and
#: deletable — so one retry is a reasonable trade against a transient 503.
_WRITE = Retry(max_attempts=2, initial_delay=1.0)

#: Creating and commenting have no idempotency key here. If the request times
#: out *after* the service accepted it, a retry files a second issue or posts
#: the comment twice, and nothing on the client side can tell which happened.
#: One attempt, and a failure that reaches the workflow — journaling already
#: stops a *replay* from repeating it.
_CREATE = Retry(max_attempts=1)



@step(retry=_READ)
async def calendar_list_calendars() -> Results[CalendarSummary]:
    """List the calendars this account can see.

    Returns:
        List of CalendarSummary with id, summary, time_zone, primary,
        access_role. The id is what every other tool takes as calendar_id.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).list_calendars()


@step(retry=_READ)
async def calendar_list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    query: str = "",
    max_results: int = 25,
) -> Results[CalendarEvent]:
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
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).list_events(
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
    from loom.toolsets.factory import client_for

    client = await client_for("google_calendar", CalendarClient)
    return await client.get_event(event_id, calendar_id)


@step(retry=_CREATE)
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
    add_meet: bool = False,
    recurrence: list[str] | None = None,
) -> CalendarEvent:
    """Create a calendar event, optionally with a Google Meet link.

    **This is how a Meet meeting is scheduled.** The Meet API cannot schedule
    anything — ``meet_create_space`` makes a room with a link and no time, no
    attendees and no calendar entry. Pass ``add_meet=True`` here instead.

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
        add_meet: Provision a Google Meet link on the event. Read it back from
            ``event.hangout_link``.
        recurrence: RRULE strings for a repeating event, e.g.
            ``["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10"]``.

    Returns:
        The created CalendarEvent, including its id, url, and hangout_link when
        add_meet was set.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).create_event(
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
        add_meet=add_meet,
        recurrence=recurrence,
    )


@step(retry=_WRITE)
async def calendar_add_meet_link(
    event_id: str, calendar_id: str = "primary"
) -> CalendarEvent:
    """Attach a Google Meet link to an event that has none.

    Args:
        event_id: Event id.
        calendar_id: Calendar the event lives on.

    Returns:
        The updated CalendarEvent, with hangout_link populated. A second call
        reuses the same conference rather than provisioning another.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).add_meet_link(
        event_id, calendar_id=calendar_id
    )


@step(retry=_READ)
async def calendar_list_event_instances(
    event_id: str,
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
) -> Results[CalendarEvent]:
    """Expand one recurring series into its individual occurrences.

    Use this to cancel or move a *single* occurrence: editing the series master
    changes every occurrence, including ones that already happened.

    Args:
        event_id: Id of the recurring series master.
        calendar_id: Calendar the series lives on.
        time_min: RFC 3339 start of the window, from ``ctx.now()``.
        time_max: RFC 3339 end of the window.
        max_results: Maximum instances to return (default 50).

    Returns:
        Results[CalendarEvent], one per occurrence. Each has its own id, which
        is what update and delete take.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).list_event_instances(
        event_id,
        calendar_id=calendar_id,
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
    )


@step(retry=_WRITE)
async def calendar_move_event(
    event_id: str,
    destination_calendar_id: str,
    calendar_id: str = "primary",
    send_updates: str = "none",
) -> CalendarEvent:
    """Move an event to another calendar, keeping its id and its RSVPs.

    Args:
        event_id: Event id.
        destination_calendar_id: Calendar to move it to.
        calendar_id: Calendar it is currently on.
        send_updates: Whether to notify attendees.

    Returns:
        The moved CalendarEvent. Deleting and recreating instead would drop
        every RSVP and re-invite everyone.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).move_event(
        event_id,
        destination_calendar_id,
        calendar_id=calendar_id,
        send_updates=send_updates,
    )


@step(retry=_WRITE)
async def calendar_respond_to_event(
    event_id: str,
    response: str,
    calendar_id: str = "primary",
    comment: str = "",
) -> CalendarEvent:
    """RSVP to an invitation as the authenticated account.

    Args:
        event_id: Event id.
        response: ``"accepted"``, ``"declined"``, ``"tentative"``, or
            ``"needsAction"``.
        calendar_id: Calendar the invitation is on.
        comment: Optional note sent with the response.

    Returns:
        The updated CalendarEvent. Raises if this account is not an attendee —
        an organiser edits the event with calendar_update_event instead.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).respond_to_event(
        event_id, response, calendar_id=calendar_id, comment=comment
    )


@step(retry=_WRITE)
async def calendar_update_event(
    event_id: str,
    fields: dict[str, Any],
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
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).update_event(
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
    from loom.toolsets.factory import client_for

    await (await client_for("google_calendar", CalendarClient)).delete_event(
        event_id, calendar_id=calendar_id, send_updates=send_updates
    )
    return event_id


@step(retry=_CREATE)
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
    from loom.toolsets.factory import client_for

    client = await client_for("google_calendar", CalendarClient)
    return await client.quick_add_event(text, calendar_id)


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
    from loom.toolsets.factory import client_for

    client = await client_for("google_calendar", CalendarClient)
    return await client.free_busy(time_min, time_max, calendar_ids)


# ---------------------------------------------------------------------------
# Calendars themselves
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def calendar_get_calendar(calendar_id: str = "primary") -> CalendarSummary:
    """Fetch one calendar's metadata, including its timezone.

    Args:
        calendar_id: Calendar id, or ``"primary"``.

    Returns:
        CalendarSummary with id, summary, time_zone. The timezone is what an
        event created without an explicit one is interpreted in, so read it
        before scheduling "9am".
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).get_calendar(calendar_id)


@step(retry=_READ)
async def calendar_find_calendar(calendar_name: str) -> CalendarSummary | None:
    """Resolve a calendar name to the id every other tool takes.

    A secondary calendar's id is an opaque
    ``...@group.calendar.google.com`` address that nobody types; a person says
    "the Team calendar". Resolve once here, then pass ``calendar.id``.

    Args:
        calendar_name: Exact calendar name, as shown in Google Calendar.
            Named ``calendar_name`` because ``ctx.step`` reserves ``name``.

    Returns:
        The CalendarSummary, or None when nothing matches. ``"primary"`` needs
        no resolution — it is accepted everywhere as-is.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).find_calendar(calendar_name)


@step(retry=_CREATE)
async def calendar_create_calendar(
    summary: str, time_zone: str = "", description: str = ""
) -> CalendarSummary:
    """Create a secondary calendar.

    Args:
        summary: Calendar name.
        time_zone: IANA zone, e.g. ``"Europe/London"``.
        description: Longer description.

    Returns:
        The created CalendarSummary, including the id other tools take.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).create_calendar(
        summary, time_zone=time_zone, description=description
    )


@step(retry=_WRITE)
async def calendar_delete_calendar(calendar_id: str) -> str:
    """Delete a secondary calendar and every event on it. Not recoverable.

    Refuses ``"primary"``, which would clear the account's main calendar rather
    than remove a calendar. Worth a ``ctx.wait_for_approval()`` when an agent
    chose the id.

    Args:
        calendar_id: Id of the calendar to delete.

    Returns:
        The deleted calendar id, so the journal records what was removed.
    """
    from loom.toolsets.factory import client_for

    await (await client_for("google_calendar", CalendarClient)).delete_calendar(calendar_id)
    return calendar_id


@step(retry=_READ)
async def calendar_list_acl(
    calendar_id: str = "primary", max_results: int = 100
) -> Results[CalendarAccessRule]:
    """List who has standing access to a calendar.

    Args:
        calendar_id: Calendar id, or ``"primary"``.
        max_results: Maximum rules to return (default 100).

    Returns:
        Results[CalendarAccessRule] with id, scope_type, scope_value, role.
        The id is what ``calendar_unshare_calendar`` takes.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("google_calendar", CalendarClient)
    return await client.list_acl(calendar_id, max_results)


@step(retry=_WRITE)
async def calendar_share_calendar(
    calendar_id: str,
    email: str = "",
    role: str = "reader",
    scope_type: str = "user",
    domain: str = "",
) -> CalendarAccessRule:
    """Grant standing access to a whole calendar.

    Much wider than inviting someone to one event, and permanent until revoked
    — if the intent is "let them see this meeting", add them as an attendee.

    Args:
        calendar_id: Calendar to share.
        email: Who to share with, for scope_type ``"user"`` or ``"group"``.
        role: ``"reader"`` (default), ``"freeBusyReader"``, ``"writer"``,
            ``"owner"``, or ``"none"``.
        scope_type: ``"user"`` (default), ``"group"``, ``"domain"``, or
            ``"default"`` — the last makes the calendar public.
        domain: The domain, for scope_type ``"domain"``.

    Returns:
        The created CalendarAccessRule, including its id.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_calendar", CalendarClient)).share_calendar(
        calendar_id, email=email, role=role, scope_type=scope_type, domain=domain
    )


@step(retry=_WRITE)
async def calendar_unshare_calendar(calendar_id: str, rule_id: str) -> str:
    """Revoke one person's standing access to a calendar.

    Args:
        calendar_id: Calendar id.
        rule_id: Rule id from ``calendar_list_acl``, e.g.
            ``"user:ada@example.com"``.

    Returns:
        The revoked rule id.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("google_calendar", CalendarClient)
    await client.unshare_calendar(calendar_id, rule_id)
    return rule_id


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type[BaseModel]) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Google Calendar Tools

Import: from loom.toolsets.google.calendar.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  GOOGLE_ACCESS_TOKEN, or
  GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

All tools return typed Pydantic models. Use attribute access:
event.summary, event.start, event.attendees[0].email.

### Tools

calendar_list_calendars() -> Results[CalendarSummary]
  Every calendar this account can see. Returns a Results list (.complete,
  .total, .summary()) — Google pages this, so check .complete before saying
  "all".
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
                      time_zone="", all_day=False, send_updates="none",
                      add_meet=False, recurrence=None) -> CalendarEvent
  send_updates defaults to "none" — pass "all" to actually email attendees.
  add_meet=True provisions a Google Meet link; read it from ev.hangout_link.
  THIS is how a Meet meeting is scheduled — meet_create_space cannot do it.
    ev = await ctx.step(calendar_create_event, "Standup",
                        "2026-03-02T09:00:00Z", "2026-03-02T09:15:00Z",
                        attendees=["a@b.com"], add_meet=True)
  recurrence takes RRULEs: ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10"]

calendar_add_meet_link(event_id, calendar_id="primary") -> CalendarEvent
  For an event that already exists. Calling twice reuses one conference.

calendar_update_event(event_id, fields, calendar_id="primary",
                      send_updates="none") -> CalendarEvent
    await ctx.step(calendar_update_event, ev.id, {{"location": "Room 4"}})

calendar_delete_event(event_id, calendar_id="primary",
                      send_updates="none") -> str
  Not recoverable. Returns the deleted id.

calendar_move_event(event_id, destination_calendar_id, calendar_id="primary",
                    send_updates="none") -> CalendarEvent
  Keeps the id and the RSVPs; delete-and-recreate drops both.

calendar_respond_to_event(event_id, response, calendar_id="primary",
                          comment="") -> CalendarEvent
  response: "accepted" | "declined" | "tentative" | "needsAction"

calendar_list_event_instances(event_id, calendar_id="primary", time_min="",
                              time_max="", max_results=50)
    -> Results[CalendarEvent]
  Occurrences of ONE recurring series. Cancel a single occurrence by its own
  id — editing the series master changes every occurrence, past ones too.

calendar_quick_add_event(text, calendar_id="primary") -> CalendarEvent
    await ctx.step(calendar_quick_add_event, "Lunch with Bob tomorrow 1pm")

calendar_find_busy_periods(time_min, time_max, calendar_ids=None)
    -> list[BusyPeriod]
  BusyPeriod fields: {fields(BusyPeriod)}
  Free slots are the gaps — compute them in the workflow body.

### Calendars themselves

calendar_find_calendar(calendar_name) -> CalendarSummary | None
  THE calendar resolver. A secondary calendar's id is an opaque
  ...@group.calendar.google.com address; "primary" needs no resolving.

calendar_get_calendar(calendar_id="primary") -> CalendarSummary
  Read .time_zone before scheduling "9am" — that is the zone it means.

calendar_create_calendar(summary, time_zone="", description="")
    -> CalendarSummary
calendar_delete_calendar(calendar_id) -> str
  Deletes every event on it. Refuses "primary". Not recoverable.

calendar_list_acl(calendar_id="primary", max_results=100)
    -> Results[CalendarAccessRule]
  CalendarAccessRule fields: {fields(CalendarAccessRule)}
calendar_share_calendar(calendar_id, email="", role="reader",
                        scope_type="user", domain="") -> CalendarAccessRule
  Standing access to the WHOLE calendar. To share one meeting, invite them
  to it as an attendee instead.
calendar_unshare_calendar(calendar_id, rule_id) -> str

### Notes

- Every timestamp is RFC 3339. Derive windows from ctx.now(), never
  datetime.now(): a workflow body must be deterministic across replays.
- An all-day event has date-only start/end and all_day=True; do not compare
  it against an instant without checking that flag.
- list_events and list_event_instances are paged and return Results — check
  .complete before reporting a count.
- Deleting, sharing a whole calendar, and inviting are the operations worth a
  ctx.wait_for_approval() when an agent chose the arguments.
"""


CALENDAR_TOOL_DOCS: str = _build_tool_docs()
