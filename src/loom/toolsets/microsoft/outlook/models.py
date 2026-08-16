"""Typed shapes shared by the Outlook mail and calendar toolsets.

The two toolsets are separately grantable but they share Exchange's vocabulary:
a recipient is an ``emailAddress`` wrapper in both, and an event's organiser is
the same shape as a message's sender. Modelling that twice would let the two
drift over what "a person" looks like.

Flattened as everywhere else here. Graph nests a recipient two levels
(``{"emailAddress": {"name": ..., "address": ...}}``) and a date three
(``{"start": {"dateTime": ..., "timeZone": ...}}``), and a workflow comparing a
meeting against ``ctx.now()`` should not have to unwrap either.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "Calendar",
    "CalendarEvent",
    "MailFolder",
    "MeetingSuggestion",
    "OutlookMessage",
    "Recipient",
    "ScheduleSlot",
]


class Recipient(BaseModel):
    """A person on a message or a meeting."""

    name: str = ""
    address: str = ""

    @classmethod
    def from_api(cls, raw: Any) -> Recipient:
        """Read Graph's two-level recipient wrapper.

        Accepts either the wrapper (``{"emailAddress": {...}}``) or the inner
        object, because Graph uses the first on messages and the second inside
        some calendar payloads.
        """
        if not isinstance(raw, dict):
            return cls()
        inner = raw.get("emailAddress")
        source = inner if isinstance(inner, dict) else raw
        return cls(
            name=str(source.get("name") or ""),
            address=str(source.get("address") or ""),
        )

    def to_api(self) -> dict[str, Any]:
        """Back to Graph's wrapper shape, for a send or an invite."""
        entry: dict[str, Any] = {"address": self.address}
        if self.name:
            entry["name"] = self.name
        return {"emailAddress": entry}


def _people(raw: Any) -> list[Recipient]:
    return [Recipient.from_api(item) for item in (raw or []) if isinstance(item, dict)]


def _when(raw: Any) -> tuple[str, str]:
    """Unwrap Graph's ``dateTimeTimeZone`` into (timestamp, timezone).

    Graph returns a local-looking timestamp with no offset plus a separate
    ``timeZone`` name — ``"2026-01-05T09:00:00.0000000"`` with
    ``"Pacific Standard Time"``. Reading only the first and treating it as UTC
    shifts every meeting by the offset, silently, and that is exactly the kind
    of wrong-but-plausible answer this package keeps designing against. Both
    halves are kept.
    """
    if not isinstance(raw, dict):
        return "", ""
    return str(raw.get("dateTime") or ""), str(raw.get("timeZone") or "")


class MailFolder(BaseModel):
    """A mail folder."""

    id: str
    display_name: str = ""
    parent_id: str = ""
    total_items: int = 0
    unread_items: int = 0
    child_folder_count: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> MailFolder:
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            parent_id=str(raw.get("parentFolderId") or ""),
            total_items=int(raw.get("totalItemCount") or 0),
            unread_items=int(raw.get("unreadItemCount") or 0),
            child_folder_count=int(raw.get("childFolderCount") or 0),
        )


class OutlookMessage(BaseModel):
    """One mail message."""

    id: str
    subject: str = ""
    body: str = ""
    """Text by default. Graph returns HTML unless the client asks for text via
    ``Prefer: outlook.body-content-type``, which it does — HTML spends tokens
    on markup and buries the content."""
    body_preview: str = ""
    """Graph's own short plain-text summary. Present whatever the body format,
    so it is the cheap thing to read when triaging."""
    from_: Recipient = Field(default_factory=Recipient, alias="from")
    """Named ``from_`` because ``from`` is a Python keyword; the alias keeps the
    wire name so a round-trip is faithful."""
    to: list[Recipient] = Field(default_factory=list)
    cc: list[Recipient] = Field(default_factory=list)
    bcc: list[Recipient] = Field(default_factory=list)
    reply_to: list[Recipient] = Field(default_factory=list)
    received: str = ""
    sent: str = ""
    is_read: bool = False
    is_draft: bool = False
    has_attachments: bool = False
    importance: str = ""
    conversation_id: str = ""
    """Groups a thread. Outlook's UI shows conversations, so acting on one
    message of a thread often looks like nothing happened."""
    folder_id: str = ""
    web_link: str = ""
    categories: list[str] = Field(default_factory=list)
    internet_message_id: str = ""

    model_config = {"populate_by_name": True}

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> OutlookMessage:
        body = raw.get("body") or {}
        return cls(
            id=str(raw.get("id", "")),
            subject=raw.get("subject") or "",
            body=str(body.get("content") or ""),
            body_preview=raw.get("bodyPreview") or "",
            **{"from": Recipient.from_api(raw.get("from") or raw.get("sender"))},
            to=_people(raw.get("toRecipients")),
            cc=_people(raw.get("ccRecipients")),
            bcc=_people(raw.get("bccRecipients")),
            reply_to=_people(raw.get("replyTo")),
            received=raw.get("receivedDateTime") or "",
            sent=raw.get("sentDateTime") or "",
            is_read=bool(raw.get("isRead", False)),
            is_draft=bool(raw.get("isDraft", False)),
            has_attachments=bool(raw.get("hasAttachments", False)),
            importance=raw.get("importance") or "",
            conversation_id=str(raw.get("conversationId") or ""),
            folder_id=str(raw.get("parentFolderId") or ""),
            web_link=raw.get("webLink") or "",
            categories=[str(c) for c in (raw.get("categories") or [])],
            internet_message_id=str(raw.get("internetMessageId") or ""),
        )


class Calendar(BaseModel):
    """A calendar."""

    id: str
    name: str = ""
    owner: Recipient = Field(default_factory=Recipient)
    color: str = ""
    is_default: bool = False
    can_edit: bool = False
    can_share: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Calendar:
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            owner=Recipient.from_api(raw.get("owner")),
            color=raw.get("color") or "",
            is_default=bool(raw.get("isDefaultCalendar", False)),
            can_edit=bool(raw.get("canEdit", False)),
            can_share=bool(raw.get("canShare", False)),
        )


class CalendarEvent(BaseModel):
    """One event, or one occurrence of a recurring series."""

    id: str
    subject: str = ""
    body: str = ""
    body_preview: str = ""
    start: str = ""
    """Graph's local-looking timestamp. Read with ``start_timezone`` — on its
    own it is not an instant."""
    start_timezone: str = ""
    end: str = ""
    end_timezone: str = ""
    is_all_day: bool = False
    location: str = ""
    organizer: Recipient = Field(default_factory=Recipient)
    attendees: list[Recipient] = Field(default_factory=list)
    is_cancelled: bool = False
    is_online_meeting: bool = False
    online_meeting_url: str = ""
    """The join link. Empty on an event created without asking for a meeting —
    see ``add_teams_meeting`` on the create tool."""
    web_link: str = ""
    series_master_id: str = ""
    """Set on an occurrence produced by expanding a recurring series. Editing
    an occurrence and editing its series are different calls."""
    event_type: str = ""
    """``singleInstance``, ``occurrence``, ``exception``, or ``seriesMaster``.
    A listing from ``/events`` returns masters; a calendar view returns
    occurrences."""
    response_status: str = ""
    """This account's own response: ``none``, ``accepted``, ``declined``,
    ``tentativelyAccepted``, or ``organizer``."""
    sensitivity: str = ""
    show_as: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> CalendarEvent:
        body = raw.get("body") or {}
        start, start_tz = _when(raw.get("start"))
        end, end_tz = _when(raw.get("end"))
        location = raw.get("location") or {}
        meeting = raw.get("onlineMeeting") or {}
        response = raw.get("responseStatus") or {}
        return cls(
            id=str(raw.get("id", "")),
            subject=raw.get("subject") or "",
            body=str(body.get("content") or ""),
            body_preview=raw.get("bodyPreview") or "",
            start=start,
            start_timezone=start_tz,
            end=end,
            end_timezone=end_tz,
            is_all_day=bool(raw.get("isAllDay", False)),
            location=location.get("displayName") or "",
            organizer=Recipient.from_api(raw.get("organizer")),
            attendees=_people(raw.get("attendees")),
            is_cancelled=bool(raw.get("isCancelled", False)),
            is_online_meeting=bool(raw.get("isOnlineMeeting", False)),
            online_meeting_url=(
                meeting.get("joinUrl") or raw.get("onlineMeetingUrl") or ""
            ),
            web_link=raw.get("webLink") or "",
            series_master_id=str(raw.get("seriesMasterId") or ""),
            event_type=raw.get("type") or "",
            response_status=str(response.get("response") or ""),
            sensitivity=raw.get("sensitivity") or "",
            show_as=raw.get("showAs") or "",
        )


class MeetingSuggestion(BaseModel):
    """A time everyone could meet, as suggested by ``findMeetingTimes``."""

    start: str = ""
    start_timezone: str = ""
    end: str = ""
    end_timezone: str = ""
    confidence: float = 0.0
    """Graph's own 0-100 score for how likely the attendees are to accept."""
    organizer_availability: str = ""
    attendee_availability: list[str] = Field(default_factory=list)
    """One entry per attendee: ``free``, ``tentative``, ``busy``, ``oof``,
    ``workingElsewhere``, or ``unknown``."""
    suggestion_reason: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> MeetingSuggestion:
        slot = raw.get("meetingTimeSlot") or {}
        start, start_tz = _when(slot.get("start"))
        end, end_tz = _when(slot.get("end"))
        return cls(
            start=start,
            start_timezone=start_tz,
            end=end,
            end_timezone=end_tz,
            confidence=float(raw.get("confidence") or 0.0),
            organizer_availability=str(raw.get("organizerAvailability") or ""),
            attendee_availability=[
                str((a or {}).get("availability") or "")
                for a in (raw.get("attendeeAvailability") or [])
            ],
            suggestion_reason=str(raw.get("suggestionReason") or ""),
        )


class ScheduleSlot(BaseModel):
    """One person's free/busy over a window, from ``getSchedule``."""

    address: str = ""
    availability_view: str = ""
    """A string of digits, one per interval: 0 free, 1 tentative, 2 busy,
    3 out of office, 4 working elsewhere. Compact, and the thing to read when
    you only need "is there a gap"."""
    error: str = ""
    """Set when this mailbox could not be read — a mailbox outside the tenant,
    or one the token cannot see. Carried rather than dropped, because a person
    silently missing from a free/busy check reads as a person who is free."""
    busy_periods: list[dict[str, str]] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ScheduleSlot:
        error = raw.get("error") or {}
        periods = []
        for item in raw.get("scheduleItems") or []:
            start, _ = _when(item.get("start"))
            end, _ = _when(item.get("end"))
            periods.append(
                {
                    "start": start,
                    "end": end,
                    "status": str(item.get("status") or ""),
                    "subject": str(item.get("subject") or ""),
                }
            )
        return cls(
            address=str(raw.get("scheduleId") or ""),
            availability_view=str(raw.get("availabilityView") or ""),
            error=str(error.get("message") or "") if error else "",
            busy_periods=periods,
        )
