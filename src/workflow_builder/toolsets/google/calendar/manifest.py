"""Google Calendar toolset manifest."""

from __future__ import annotations

from workflow_builder.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    CalendarSummary,
)
from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GOOGLE_CALENDAR_MANIFEST"]

_event = CalendarEvent.model_json_schema()
_event_list = {"type": "array", "items": _event}
_calendar_id = {"type": "string", "default": "primary"}
_send_updates = {
    "type": "string",
    "enum": ["none", "all", "externalOnly"],
    "default": "none",
}

GOOGLE_CALENDAR_MANIFEST = ToolsetManifest(
    id="google_calendar",
    version="1.0.0",
    provider="loom",
    summary=(
        "Google Calendar — schedule meetings, read and write events, check "
        "availability."
    ),
    description=(
        "Google Calendar API v3 over REST. Lists events and appointments with "
        "recurring series expanded into instances, schedules new meetings and "
        "invites attendees, patches and cancels them, and reports free/busy "
        "availability for finding a time slot. Attendee notification is off by "
        "default so a bulk workflow does not email everyone as a side effect of "
        "a default."
    ),
    base_url="https://www.googleapis.com/calendar/v3",
    auth={
        "type": "oauth2",
        "fields": [
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
        ],
        "token_url": "https://oauth2.googleapis.com/token",
    },
    tools_module="workflow_builder.toolsets.google.calendar.tools",
    egress_hosts=["www.googleapis.com", "oauth2.googleapis.com"],
    groups={
        "events": [
            OperationSpec(
                id="events.list",
                function="calendar_list_events",
                summary="List events in a time window.",
                description=(
                    "Recurring series are expanded into instances and ordered "
                    "by start time. Windows must derive from ctx.now()."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "calendar_id": _calendar_id,
                        "time_min": {"type": "string", "format": "date-time"},
                        "time_max": {"type": "string", "format": "date-time"},
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 25},
                    },
                },
                output_schema=_event_list,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="events.get",
                function="calendar_get_event",
                summary="Fetch a single event by id.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "calendar_id": _calendar_id,
                    },
                    "required": ["event_id"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="events.create",
                function="calendar_create_event",
                summary="Create an event.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "calendar_id": _calendar_id,
                        "description": {"type": "string"},
                        "location": {"type": "string"},
                        "attendees": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                        },
                        "time_zone": {"type": "string"},
                        "all_day": {"type": "boolean", "default": False},
                        "send_updates": _send_updates,
                    },
                    "required": ["summary", "start", "end"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
            OperationSpec(
                id="events.update",
                function="calendar_update_event",
                summary="Patch fields on an existing event.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "fields": {"type": "object"},
                        "calendar_id": _calendar_id,
                        "send_updates": _send_updates,
                    },
                    "required": ["event_id", "fields"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
            OperationSpec(
                id="events.delete",
                function="calendar_delete_event",
                summary="Delete an event. Not recoverable.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "calendar_id": _calendar_id,
                        "send_updates": _send_updates,
                    },
                    "required": ["event_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
            OperationSpec(
                id="events.quick_add",
                function="calendar_quick_add_event",
                summary="Create an event from a natural-language phrase.",
                description="Google parses the text server-side.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "calendar_id": _calendar_id,
                    },
                    "required": ["text"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
        ],
        "availability": [
            OperationSpec(
                id="availability.free_busy",
                function="calendar_find_busy_periods",
                summary="Busy intervals across one or more calendars.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "time_min": {"type": "string", "format": "date-time"},
                        "time_max": {"type": "string", "format": "date-time"},
                        "calendar_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["time_min", "time_max"],
                },
                output_schema={
                    "type": "array",
                    "items": BusyPeriod.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
            ),
        ],
        "calendars": [
            OperationSpec(
                id="calendars.list",
                function="calendar_list_calendars",
                summary="List the calendars this account can see.",
                effect=EffectClass.READ,
                pagination=True,
                output_schema={
                    "type": "array",
                    "items": CalendarSummary.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
            ),
        ],
    },
)
