"""Google Calendar API v3 client.

Pure httpx. The one non-obvious behaviour is that ``list_events`` defaults to
``singleEvents=True`` with ``orderBy=startTime``: without it a weekly standup
comes back as a single recurring master with a recurrence rule, which is almost
never what a workflow asking "what is on Tuesday" wants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflow_builder.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    CalendarSummary,
    EventAttendee,
)
from workflow_builder.toolsets.google.http import GoogleSession
from workflow_builder.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from workflow_builder.toolsets.google.auth import GoogleAuth

__all__ = ["CalendarClient", "flatten_event", "get_default_client"]

API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]


class CalendarClient:
    """Async Google Calendar client returning typed models."""

    def __init__(
        self,
        auth: GoogleAuth | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        from workflow_builder.toolsets.google.auth import get_default_auth

        self._session = GoogleSession(
            auth or get_default_auth(SCOPES), API_BASE, transport=transport
        )

    # -- calendars -----------------------------------------------------------

    async def list_calendars(
        self, max_results: int = 250
    ) -> Results[CalendarSummary]:
        """List calendars the authenticated user can see, following every page."""
        items = await self._session.paginate(
            "users/me/calendarList", items_key="items", limit=max_results
        )
        return items.mapped(
            lambda item: CalendarSummary(
                id=item.get("id", ""),
                summary=item.get("summary", ""),
                description=item.get("description", ""),
                time_zone=item.get("timeZone", ""),
                primary=bool(item.get("primary", False)),
                access_role=item.get("accessRole", ""),
            )
        )

    # -- events --------------------------------------------------------------

    async def list_events(
        self,
        calendar_id: str = "primary",
        time_min: str = "",
        time_max: str = "",
        query: str = "",
        max_results: int = 25,
        single_events: bool = True,
    ) -> Results[CalendarEvent]:
        """List events in a window.

        ``time_min``/``time_max`` are RFC 3339 timestamps. In a workflow they
        must come from ``ctx.now()`` — a body that calls ``datetime.now()``
        returns different events on replay and is flagged by the determinism
        lint.
        """
        params: dict[str, Any] = {
            "timeMin": time_min or None,
            "timeMax": time_max or None,
            "q": query or None,
            "singleEvents": "true" if single_events else "false",
        }
        if single_events:
            # Only legal with singleEvents; the API rejects it otherwise.
            params["orderBy"] = "startTime"

        items = await self._session.paginate(
            f"calendars/{_quote(calendar_id)}/events",
            items_key="items",
            limit=max_results,
            params=params,
        )
        return items.mapped(lambda item: flatten_event(item, calendar_id))

    async def get_event(
        self, event_id: str, calendar_id: str = "primary"
    ) -> CalendarEvent:
        data = await self._session.get(
            f"calendars/{_quote(calendar_id)}/events/{event_id}"
        )
        return flatten_event(data, calendar_id)

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        *,
        calendar_id: str = "primary",
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        time_zone: str = "",
        all_day: bool = False,
        send_updates: str = "none",
    ) -> CalendarEvent:
        """Create an event.

        ``send_updates`` decides whether Google emails the attendees: ``none``
        (default), ``all``, or ``externalOnly``. It defaults to ``none`` because
        a workflow that creates a hundred events should not, as a side effect of
        a default, send a hundred invitations.
        """
        body = _event_body(
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            time_zone=time_zone,
            all_day=all_day,
        )
        data = await self._session.post(
            f"calendars/{_quote(calendar_id)}/events",
            body,
            sendUpdates=send_updates,
        )
        return flatten_event(data, calendar_id)

    async def update_event(
        self,
        event_id: str,
        fields: dict[str, Any],
        *,
        calendar_id: str = "primary",
        send_updates: str = "none",
    ) -> CalendarEvent:
        """Patch an event. Only the keys given are touched."""
        data = await self._session.patch(
            f"calendars/{_quote(calendar_id)}/events/{event_id}",
            fields,
            sendUpdates=send_updates,
        )
        return flatten_event(data, calendar_id)

    async def delete_event(
        self,
        event_id: str,
        *,
        calendar_id: str = "primary",
        send_updates: str = "none",
    ) -> None:
        """Delete an event. Returns nothing; the API answers 204."""
        await self._session.delete(
            f"calendars/{_quote(calendar_id)}/events/{event_id}",
            sendUpdates=send_updates,
        )

    async def quick_add_event(
        self, text: str, calendar_id: str = "primary"
    ) -> CalendarEvent:
        """Create an event from a phrase like 'Lunch with Bob tomorrow 1pm'.

        Google parses the text server-side. Convenient for an agent, and less
        predictable than ``create_event`` — the parse is not something the
        workflow can inspect before it commits.
        """
        data = await self._session.post(
            f"calendars/{_quote(calendar_id)}/events/quickAdd", None, text=text
        )
        return flatten_event(data, calendar_id)

    # -- availability --------------------------------------------------------

    async def free_busy(
        self, time_min: str, time_max: str, calendar_ids: list[str] | None = None
    ) -> list[BusyPeriod]:
        """Return the busy intervals across one or more calendars."""
        ids = calendar_ids or ["primary"]
        data = await self._session.post(
            "freeBusy",
            {
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": cid} for cid in ids],
            },
        )
        periods: list[BusyPeriod] = []
        for cid, entry in ((data or {}).get("calendars") or {}).items():
            for slot in entry.get("busy") or []:
                periods.append(
                    BusyPeriod(
                        calendar_id=cid,
                        start=slot.get("start", ""),
                        end=slot.get("end", ""),
                    )
                )
        return periods


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def flatten_event(raw: dict[str, Any], calendar_id: str = "") -> CalendarEvent:
    """Flatten a Calendar event resource into :class:`CalendarEvent`."""
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    all_day = "date" in start and "dateTime" not in start

    return CalendarEvent(
        id=raw.get("id", ""),
        calendar_id=calendar_id,
        summary=raw.get("summary", ""),
        description=raw.get("description", ""),
        location=raw.get("location", ""),
        start=start.get("dateTime") or start.get("date", ""),
        end=end.get("dateTime") or end.get("date", ""),
        all_day=all_day,
        time_zone=start.get("timeZone", ""),
        status=raw.get("status", "confirmed"),
        organizer=(raw.get("organizer") or {}).get("email", ""),
        attendees=[
            EventAttendee(
                email=person.get("email", ""),
                display_name=person.get("displayName", ""),
                response_status=person.get("responseStatus", "needsAction"),
                optional=bool(person.get("optional", False)),
                organizer=bool(person.get("organizer", False)),
            )
            for person in raw.get("attendees") or []
        ],
        recurring_event_id=raw.get("recurringEventId", ""),
        hangout_link=raw.get("hangoutLink", ""),
        url=raw.get("htmlLink", ""),
        created=raw.get("created", ""),
        updated=raw.get("updated", ""),
    )


def _event_body(
    *,
    summary: str,
    start: str,
    end: str,
    description: str,
    location: str,
    attendees: list[str] | None,
    time_zone: str,
    all_day: bool,
) -> dict[str, Any]:
    key = "date" if all_day else "dateTime"
    start_field: dict[str, Any] = {key: start}
    end_field: dict[str, Any] = {key: end}
    if time_zone and not all_day:
        start_field["timeZone"] = time_zone
        end_field["timeZone"] = time_zone

    body: dict[str, Any] = {
        "summary": summary,
        "start": start_field,
        "end": end_field,
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": address} for address in attendees]
    return body


def _quote(calendar_id: str) -> str:
    """Percent-encode a calendar id — they are email addresses.

    Checks the type first: ``quote(None)`` raises ``quote_from_bytes() expected
    bytes``, which tells a caller nothing about which argument was wrong.
    """
    if not isinstance(calendar_id, str) or not calendar_id:
        raise ValueError(
            f"calendar_id must be a non-empty string, got {calendar_id!r}. "
            'Use "primary" for the default calendar.'
        )

    from urllib.parse import quote

    return quote(calendar_id, safe="")


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default_client: CalendarClient | None = None


def get_default_client() -> CalendarClient:
    """Return (or build) the module-level client from environment credentials."""
    global _default_client
    if _default_client is None:
        _default_client = CalendarClient()
    return _default_client


def reset_default_client() -> None:
    """Drop the cached client. For tests, and after a credential change."""
    global _default_client
    _default_client = None
