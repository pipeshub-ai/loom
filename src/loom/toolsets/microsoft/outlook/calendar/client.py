"""Outlook calendar over Microsoft Graph — pure httpx, no vendor SDK.

**`calendarView` is the one that answers the question people ask.** This is the
distinction the whole client is arranged around, and it is the direct analogue
of the ``singleEvents=True`` decision already made for Google Calendar:

* ``GET /events`` returns **series masters**. A weekly stand-up appears *once*,
  carrying a recurrence rule — not as the occurrences in your window.
* ``GET /calendarView?startDateTime=…&endDateTime=…`` returns "the occurrences,
  exceptions, and single instances of events" in the range.

So "what is on my calendar on Tuesday" answered over ``/events`` misses every
recurring meeting and returns a short list that looks perfectly valid. The
calendar view is therefore the primary read here, and ``list_events`` exists
for editing a series rather than for looking at a day.

Times are returned in UTC unless ``Prefer: outlook.timezone`` says otherwise —
and, importantly, that header does **not** reinterpret the window: Graph reads
``startDateTime``/``endDateTime`` using their own offset, defaulting to UTC when
they carry none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loom.toolsets.microsoft.auth import (
    GRAPH_BASE_URL,
    MicrosoftAuth,
    get_default_auth,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.outlook.models import (
    Calendar,
    CalendarEvent,
    MeetingSuggestion,
    Recipient,
    ScheduleSlot,
)
from loom.toolsets.microsoft.scope import user_root
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

__all__ = [
    "OutlookCalendarClient",
]


class OutlookCalendarClient:
    """Calendars, events, and availability in Exchange Online."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        user_id: str = "",
        timezone: str = "",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._user_id = user_id
        self._timezone = timezone
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    def _root(self) -> str:
        return user_root(
            self._auth,
            self._user_id,
            workload="a calendar",
            env_hint="MS_OUTLOOK_USER",
        )

    def _calendar(self, calendar_id: str = "") -> str:
        """The calendar to work in — a named one, or the user's default."""
        root = self._root()
        if calendar_id:
            return f"{root}/calendars/{quote(calendar_id, safe='')}"
        return root

    def _prefer(self, timezone: str = "") -> dict[str, str]:
        zone = timezone or self._timezone
        return {"Prefer": f'outlook.timezone="{zone}"'} if zone else {}

    # -- calendars -----------------------------------------------------------

    async def list_calendars(self, *, limit: int = 100) -> Results[Calendar]:
        return await self._session.paginate(
            f"{self._root()}/calendars",
            limit=limit,
            page_size=100,
            row=Calendar.from_api,
        )

    # -- reading events ------------------------------------------------------

    async def list_calendar_view(
        self,
        start: str,
        end: str,
        *,
        calendar_id: str = "",
        limit: int = 100,
        timezone: str = "",
    ) -> Results[CalendarEvent]:
        """Everything happening between two instants, recurrences expanded."""
        if not start or not end:
            raise GraphPermanentError(
                "calendarView needs both start and end — Graph requires the "
                "window, and without it there is nothing to expand recurring "
                "series over.",
                status=0,
                code="missingWindow",
            )
        return await self._session.paginate(
            f"{self._calendar(calendar_id)}/calendarView",
            limit=limit,
            params={"startDateTime": start, "endDateTime": end},
            page_size=100,
            row=CalendarEvent.from_api,
            headers=self._prefer(timezone),
        )

    async def list_events(
        self,
        *,
        calendar_id: str = "",
        limit: int = 50,
        filter_query: str = "",
        order_by: str = "",
        timezone: str = "",
    ) -> Results[CalendarEvent]:
        """List event *records* — series masters, not occurrences.

        For "what is happening on Tuesday" use :meth:`list_calendar_view`; this
        is for reaching a series in order to edit it.
        """
        params: dict[str, Any] = {}
        if filter_query:
            params["$filter"] = filter_query
        if order_by:
            params["$orderby"] = order_by
        return await self._session.paginate(
            f"{self._calendar(calendar_id)}/events",
            limit=limit,
            params=params,
            page_size=50,
            row=CalendarEvent.from_api,
            headers=self._prefer(timezone),
        )

    async def get_event(
        self, event_id: str, *, calendar_id: str = "", timezone: str = ""
    ) -> CalendarEvent:
        raw = await self._session.request(
            "GET",
            f"{self._calendar(calendar_id)}/events/{quote(event_id, safe='')}",
            headers=self._prefer(timezone),
        )
        return CalendarEvent.from_api(raw or {})

    # -- writing events ------------------------------------------------------

    async def create_event(
        self,
        subject: str,
        start: str,
        end: str,
        *,
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
        """Create an event, optionally with a Teams meeting attached.

        ``add_teams_meeting`` sets both ``isOnlineMeeting`` and
        ``onlineMeetingProvider``: setting only the first produces an event
        that claims to be online and carries no join link, which is the same
        shape of quiet failure as Google Calendar's missing
        ``conferenceDataVersion``.
        """
        event: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": timezone},
            "end": {"dateTime": end, "timeZone": timezone},
        }
        if body:
            event["body"] = {"contentType": body_type, "content": body}
        if location:
            event["location"] = {"displayName": location}
        if attendees:
            event["attendees"] = [
                {**Recipient(address=a).to_api(), "type": "required"}
                for a in attendees
            ]
        if is_all_day:
            event["isAllDay"] = True
        if add_teams_meeting:
            event["isOnlineMeeting"] = True
            event["onlineMeetingProvider"] = "teamsForBusiness"
        if reminder_minutes is not None:
            event["isReminderOn"] = True
            event["reminderMinutesBeforeStart"] = reminder_minutes

        raw = await self._session.post(
            f"{self._calendar(calendar_id)}/events", json=event
        )
        return CalendarEvent.from_api(raw or {})

    async def update_event(
        self,
        event_id: str,
        *,
        calendar_id: str = "",
        subject: str = "",
        start: str = "",
        end: str = "",
        timezone: str = "UTC",
        body: str = "",
        body_type: str = "text",
        location: str = "",
    ) -> CalendarEvent:
        changes: dict[str, Any] = {}
        if subject:
            changes["subject"] = subject
        if start:
            changes["start"] = {"dateTime": start, "timeZone": timezone}
        if end:
            changes["end"] = {"dateTime": end, "timeZone": timezone}
        if body:
            changes["body"] = {"contentType": body_type, "content": body}
        if location:
            changes["location"] = {"displayName": location}
        if not changes:
            raise GraphPermanentError(
                "update_event needs something to change; with no arguments it "
                "is a no-op PATCH that reads as a successful update.",
                status=0,
                code="nothingToDo",
            )
        raw = await self._session.patch(
            f"{self._calendar(calendar_id)}/events/{quote(event_id, safe='')}",
            json=changes,
        )
        return CalendarEvent.from_api(raw or {})

    async def delete_event(self, event_id: str, *, calendar_id: str = "") -> bool:
        await self._session.delete(
            f"{self._calendar(calendar_id)}/events/{quote(event_id, safe='')}"
        )
        return True

    async def respond_to_event(
        self,
        event_id: str,
        response: str,
        *,
        calendar_id: str = "",
        comment: str = "",
        send_response: bool = True,
    ) -> bool:
        """Accept, decline, or tentatively accept an invitation."""
        actions = {
            "accept": "accept",
            "decline": "decline",
            "tentative": "tentativelyAccept",
        }
        action = actions.get(response)
        if action is None:
            raise GraphPermanentError(
                f"response must be one of {sorted(actions)}, not {response!r}.",
                status=0,
                code="invalidResponse",
            )
        await self._session.post(
            f"{self._calendar(calendar_id)}/events/"
            f"{quote(event_id, safe='')}/{action}",
            json={"comment": comment, "sendResponse": send_response},
        )
        return True

    async def cancel_event(
        self, event_id: str, *, calendar_id: str = "", comment: str = ""
    ) -> bool:
        """Cancel a meeting, notifying the attendees.

        Distinct from deleting it: cancelling tells everyone, deleting removes
        it from this calendar and leaves the invitations in place.
        """
        await self._session.post(
            f"{self._calendar(calendar_id)}/events/"
            f"{quote(event_id, safe='')}/cancel",
            json={"comment": comment},
        )
        return True

    # -- availability --------------------------------------------------------

    async def find_meeting_times(
        self,
        attendees: list[str],
        *,
        duration_minutes: int = 30,
        start: str = "",
        end: str = "",
        max_candidates: int = 10,
        minimum_confidence: float = 0.0,
    ) -> list[MeetingSuggestion]:
        """Ask Exchange when everyone could meet."""
        payload: dict[str, Any] = {
            "attendees": [
                {"type": "required", "emailAddress": {"address": a}}
                for a in attendees
            ],
            # ISO-8601 duration. Graph rejects a plain integer here.
            "meetingDuration": f"PT{duration_minutes}M",
            "maxCandidates": max_candidates,
            "minimumAttendeePercentage": minimum_confidence,
        }
        if start and end:
            payload["timeConstraint"] = {
                "activityDomain": "work",
                "timeSlots": [
                    {
                        "start": {"dateTime": start, "timeZone": "UTC"},
                        "end": {"dateTime": end, "timeZone": "UTC"},
                    }
                ],
            }
        body = await self._session.post(
            f"{self._root()}/findMeetingTimes", json=payload
        )
        return [
            MeetingSuggestion.from_api(item)
            for item in (body or {}).get("meetingTimeSuggestions", []) or []
        ]

    async def get_schedule(
        self,
        addresses: list[str],
        start: str,
        end: str,
        *,
        interval_minutes: int = 30,
        timezone: str = "UTC",
    ) -> list[ScheduleSlot]:
        """Read free/busy for a set of mailboxes."""
        body = await self._session.request(
            "POST",
            f"{self._root()}/calendar/getSchedule",
            json={
                "schedules": addresses,
                "startTime": {"dateTime": start, "timeZone": timezone},
                "endTime": {"dateTime": end, "timeZone": timezone},
                "availabilityViewInterval": interval_minutes,
            },
            headers=self._prefer(timezone),
        )
        return [
            ScheduleSlot.from_api(item)
            for item in (body or {}).get("value", []) or []
        ]


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


