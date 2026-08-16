"""Outlook calendar step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.outlook.calendar.tools import (
        outlook_list_calendar_view,
    )

    today = await ctx.step(outlook_list_calendar_view,
                           start="2026-01-05T00:00:00-08:00",
                           end="2026-01-06T00:00:00-08:00")

**Use the calendar view to see what is happening.** ``outlook_list_calendar_view``
expands recurring series into their occurrences; ``outlook_list_events`` returns
the series *records*, so a weekly stand-up appears once with a recurrence rule
and not on the days it actually happens. Asking "what is on Tuesday" over the
events listing returns a short answer that looks correct — use the view.

**Times are UTC unless you say otherwise.** Pass ``timezone`` (a Windows zone
name like ``"Pacific Standard Time"``) to have Graph render start and end times
in it. Note that this does *not* reinterpret the window you asked for: Graph
reads ``start`` and ``end`` using their own offset, so include one —
``"2026-01-05T00:00:00-08:00"`` rather than ``"2026-01-05T00:00:00"``.

**Whose calendar.** Delegated credentials act on the signed-in person's
calendar; under an app-only token set ``MS_OUTLOOK_USER``.

Retries are per operation. Reads retry; **creating an event, responding to an
invitation and cancelling do not**, because each mails the attendees and a
retry mails them again.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.microsoft.outlook.models import (
    Calendar,
    CalendarEvent,
    MeetingSuggestion,
    ScheduleSlot,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def outlook_list_calendars(limit: int = 100) -> Results[Calendar]:
    """List the calendars this account can reach.

    Resolve a calendar here before reading or writing a non-default one — a
    secondary calendar's id is an opaque string, not its name.

    Args:
        limit: Maximum calendars. Defaults to 100.

    Returns:
        Results of Calendar. ``can_edit`` is worth checking before writing: a
        calendar shared read-only accepts no events.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().list_calendars(limit=limit)


@step(retry=_READ)
async def outlook_list_calendar_view(
    start: str,
    end: str,
    calendar_id: str = "",
    limit: int = 100,
    timezone: str = "",
) -> Results[CalendarEvent]:
    """What is on the calendar between two instants, recurrences expanded.

    **This is the read you want for "what is happening".** It returns
    occurrences, exceptions and single events in the window;
    ``outlook_list_events`` returns series masters instead and would miss every
    recurring meeting.

    Args:
        start: ISO-8601 start of the window. Include an offset —
            ``"2026-01-05T00:00:00-08:00"`` — because a naive value is read as
            UTC regardless of ``timezone``, silently shifting the window.
        end: ISO-8601 end of the window.
        calendar_id: A specific calendar. Omit for the default one.
        limit: Maximum events across pages. Defaults to 100.
        timezone: Windows timezone name for the *returned* times, e.g.
            ``"Pacific Standard Time"``. Defaults to UTC.

    Returns:
        Results of CalendarEvent. Each occurrence carries ``series_master_id``
        when it came from a recurring series — editing the occurrence and
        editing the series are different calls.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().list_calendar_view(
        start, end, calendar_id=calendar_id, limit=limit, timezone=timezone
    )


@step(retry=_READ)
async def outlook_list_events(
    calendar_id: str = "",
    limit: int = 50,
    filter_query: str = "",
    order_by: str = "",
    timezone: str = "",
) -> Results[CalendarEvent]:
    """List event records — series masters, not occurrences.

    For "what is happening on a given day" use ``outlook_list_calendar_view``.
    This listing is for reaching a recurring series in order to edit it: a
    weekly meeting appears here once, carrying its recurrence rule.

    Args:
        calendar_id: A specific calendar. Omit for the default one.
        limit: Maximum events across pages. Defaults to 50.
        filter_query: OData ``$filter``, e.g. ``"subject eq 'Standup'"``.
        order_by: OData ``$orderby``.
        timezone: Windows timezone name for the returned times.

    Returns:
        Results of CalendarEvent, with ``event_type`` of ``"seriesMaster"`` for
        recurring ones.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().list_events(
        calendar_id=calendar_id,
        limit=limit,
        filter_query=filter_query,
        order_by=order_by,
        timezone=timezone,
    )


@step(retry=_READ)
async def outlook_get_event(
    event_id: str, calendar_id: str = "", timezone: str = ""
) -> CalendarEvent:
    """Fetch one event.

    Args:
        event_id: The event's id.
        calendar_id: A specific calendar. Omit for the default one.
        timezone: Windows timezone name for the returned times.

    Returns:
        CalendarEvent with attendees, body, and the online meeting link if it
        has one.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().get_event(
        event_id, calendar_id=calendar_id, timezone=timezone
    )


@step(retry=_UNSAFE_WRITE)
async def outlook_create_event(
    subject: str,
    start: str,
    end: str,
    calendar_id: str = "",
    timezone: str = "UTC",
    attendees: list[str] | None = None,
    body: str = "",
    body_type: str = "text",
    location: str = "",
    is_all_day: bool = False,
    add_teams_meeting: bool = False,
    reminder_minutes: int | None = None,
) -> CalendarEvent:
    """Create a calendar event, optionally with a Teams meeting.

    Not retried: creating an event with attendees sends invitations, and a
    retry after a timeout puts a second meeting on everybody's calendar.

    Args:
        subject: Event title.
        start: ISO-8601 start, interpreted in ``timezone``.
        end: ISO-8601 end.
        calendar_id: A specific calendar. Omit for the default one.
        timezone: Windows timezone name the start and end are given in.
            Defaults to UTC.
        attendees: Email addresses to invite. Each is added as required.
        body: Event description.
        body_type: ``"text"`` (default) or ``"html"``.
        location: Free-text location.
        is_all_day: True for an all-day event. Start and end must then be
            midnight boundaries.
        add_teams_meeting: True attaches a Teams meeting and returns its join
            link. Creating the event and creating the meeting are one call —
            there is no way to add a working link afterwards without a second
            update.
        reminder_minutes: Minutes before the start to remind attendees.

    Returns:
        The created CalendarEvent, including ``online_meeting_url`` when a
        Teams meeting was requested.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().create_event(
        subject,
        start,
        end,
        calendar_id=calendar_id,
        timezone=timezone,
        attendees=attendees,
        body=body,
        body_type=body_type,
        location=location,
        is_all_day=is_all_day,
        add_teams_meeting=add_teams_meeting,
        reminder_minutes=reminder_minutes,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def outlook_update_event(
    event_id: str,
    calendar_id: str = "",
    subject: str = "",
    start: str = "",
    end: str = "",
    timezone: str = "UTC",
    body: str = "",
    body_type: str = "text",
    location: str = "",
) -> CalendarEvent:
    """Change an event's subject, times, body, or location.

    Retried once: applying the same change twice leaves the same event.

    Args:
        event_id: The event to change.
        calendar_id: A specific calendar. Omit for the default one.
        subject: New title. Omit to leave alone.
        start: New ISO-8601 start, in ``timezone``.
        end: New ISO-8601 end.
        timezone: Windows timezone name the new times are given in.
        body: New description.
        body_type: ``"text"`` (default) or ``"html"``.
        location: New location.

    Returns:
        The updated CalendarEvent.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().update_event(
        event_id,
        calendar_id=calendar_id,
        subject=subject,
        start=start,
        end=end,
        timezone=timezone,
        body=body,
        body_type=body_type,
        location=location,
    )


@step(retry=_UNSAFE_WRITE)
async def outlook_respond_to_event(
    event_id: str,
    response: str,
    calendar_id: str = "",
    comment: str = "",
    send_response: bool = True,
) -> bool:
    """Accept, decline, or tentatively accept a meeting invitation.

    Not retried: responding emails the organiser, and a retry emails them
    again.

    Args:
        event_id: The invitation to respond to.
        response: ``"accept"``, ``"decline"``, or ``"tentative"``.
        calendar_id: A specific calendar. Omit for the default one.
        comment: A note sent to the organiser with the response.
        send_response: False records the response without emailing anyone.

    Returns:
        True when the response was accepted.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().respond_to_event(
        event_id,
        response,
        calendar_id=calendar_id,
        comment=comment,
        send_response=send_response,
    )


@step(retry=_UNSAFE_WRITE)
async def outlook_cancel_event(
    event_id: str, calendar_id: str = "", comment: str = ""
) -> bool:
    """Cancel a meeting and tell the attendees.

    Different from deleting it: cancelling notifies everyone and withdraws the
    meeting; deleting removes it from this calendar and leaves the invitations
    sitting on everyone else's. Cancel when other people were invited.

    Not retried: a retry sends a second cancellation notice.

    Args:
        event_id: The meeting to cancel.
        calendar_id: A specific calendar. Omit for the default one.
        comment: Explanation included in the cancellation notice.

    Returns:
        True when the cancellation was accepted.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().cancel_event(
        event_id, calendar_id=calendar_id, comment=comment
    )


@step(retry=_IDEMPOTENT_WRITE)
async def outlook_delete_event(event_id: str, calendar_id: str = "") -> bool:
    """Delete an event from this calendar.

    For a meeting with attendees, use ``outlook_cancel_event`` instead —
    deleting leaves everyone else's invitation in place.

    Retried once: deleting an already-deleted event is a 404, not a second
    deletion.

    Args:
        event_id: The event to delete.
        calendar_id: A specific calendar. Omit for the default one.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().delete_event(
        event_id, calendar_id=calendar_id
    )


@step(retry=_READ)
async def outlook_find_meeting_times(
    attendees: list[str],
    duration_minutes: int = 30,
    start: str = "",
    end: str = "",
    max_candidates: int = 10,
    minimum_confidence: float = 0.0,
) -> list[MeetingSuggestion]:
    """Ask Exchange when a set of people could meet.

    Args:
        attendees: Email addresses to find a slot for.
        duration_minutes: How long the meeting needs to be. Defaults to 30.
        start: ISO-8601 start of the window to search. Omit to let Exchange
            choose.
        end: ISO-8601 end of the window.
        max_candidates: Maximum suggestions to return. Defaults to 10.
        minimum_confidence: Minimum percentage of attendees who must be free,
            0-100.

    Returns:
        A list of MeetingSuggestion, each with the slot, a ``confidence``
        score, and each attendee's availability. An empty list means no slot
        satisfied the constraints — not an error.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().find_meeting_times(
        attendees,
        duration_minutes=duration_minutes,
        start=start,
        end=end,
        max_candidates=max_candidates,
        minimum_confidence=minimum_confidence,
    )


@step(retry=_READ)
async def outlook_get_schedule(
    addresses: list[str],
    start: str,
    end: str,
    interval_minutes: int = 30,
    timezone: str = "UTC",
) -> list[ScheduleSlot]:
    """Read free/busy for a set of mailboxes.

    Args:
        addresses: Email addresses to check.
        start: ISO-8601 start of the window.
        end: ISO-8601 end of the window.
        interval_minutes: Width of each slot in ``availability_view``.
            Defaults to 30.
        timezone: Windows timezone name the window is given in.

    Returns:
        A list of ScheduleSlot. ``availability_view`` is a digit per interval —
        0 free, 1 tentative, 2 busy, 3 out of office, 4 working elsewhere.
        **Check ``error``**: a mailbox the token cannot read comes back with an
        error rather than an absence, and treating it as free would schedule
        over somebody.
    """
    from loom.toolsets.microsoft.outlook.calendar.client import get_default_client

    return await get_default_client().get_schedule(
        addresses,
        start,
        end,
        interval_minutes=interval_minutes,
        timezone=timezone,
    )
