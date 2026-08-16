"""Google Calendar toolset manifest."""

from __future__ import annotations

from loom.toolsets.google.calendar.models import (
    BusyPeriod,
    CalendarAccessRule,
    CalendarEvent,
    CalendarSummary,
)
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GOOGLE_CALENDAR_MANIFEST"]

_event = CalendarEvent.model_json_schema()
_event_list = {"type": "array", "items": _event}
_calendar = CalendarSummary.model_json_schema()
_calendar_list = {"type": "array", "items": _calendar}
_access_rule = CalendarAccessRule.model_json_schema()
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
        "Google Calendar — schedule meetings (including Google Meet links), "
        "read and write events, RSVP, check availability, share calendars."
    ),
    description=(
        "Google Calendar API v3 over REST. Lists events and appointments with "
        "recurring series expanded into instances, schedules new meetings and "
        "invites attendees, patches, moves and cancels them, RSVPs to "
        "invitations, and reports free/busy availability for finding a time "
        "slot. This is also where a Google Meet meeting is scheduled: "
        "events.create with add_meet=True provisions the Meet link, which the "
        "Meet API itself cannot do. Attendee notification is off by default so "
        "a bulk workflow does not email everyone as a side effect of a default."
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
    tools_module="loom.toolsets.google.calendar.tools",
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
                        "add_meet": {"type": "boolean", "default": False},
                        "recurrence": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["summary", "start", "end"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
            OperationSpec(
                id="events.add_meet_link",
                function="calendar_add_meet_link",
                summary="Attach a Google Meet link to an existing event.",
                description=(
                    "Calling twice reuses the same conference rather than "
                    "provisioning a second one."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "calendar_id": _calendar_id,
                    },
                    "required": ["event_id"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
                idempotent=True,
            ),
            OperationSpec(
                id="events.list_instances",
                function="calendar_list_event_instances",
                summary="Expand one recurring series into its occurrences.",
                description=(
                    "Each occurrence has its own id, which is what cancels or "
                    "moves a single one. Editing the series master instead "
                    "changes every occurrence, past ones included."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "calendar_id": _calendar_id,
                        "time_min": {"type": "string", "format": "date-time"},
                        "time_max": {"type": "string", "format": "date-time"},
                        "max_results": {"type": "integer", "default": 50},
                    },
                    "required": ["event_id"],
                },
                output_schema=_event_list,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="events.move",
                function="calendar_move_event",
                summary="Move an event to another calendar, keeping its RSVPs.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "destination_calendar_id": {"type": "string"},
                        "calendar_id": _calendar_id,
                        "send_updates": _send_updates,
                    },
                    "required": ["event_id", "destination_calendar_id"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
                idempotent=True,
            ),
            OperationSpec(
                id="events.respond",
                function="calendar_respond_to_event",
                summary="RSVP to an invitation as the authenticated account.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "response": {
                            "type": "string",
                            "enum": [
                                "accepted",
                                "declined",
                                "tentative",
                                "needsAction",
                            ],
                        },
                        "calendar_id": _calendar_id,
                        "comment": {"type": "string"},
                    },
                    "required": ["event_id", "response"],
                },
                output_schema=_event,
                scopes=["https://www.googleapis.com/auth/calendar.events"],
                idempotent=True,
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
                output_schema=_calendar_list,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="calendars.get",
                function="calendar_get_calendar",
                summary="Fetch one calendar's metadata, including its timezone.",
                description=(
                    "The timezone is what an event created without an explicit "
                    "one is interpreted in."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"calendar_id": _calendar_id},
                },
                output_schema=_calendar,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="calendars.find",
                function="calendar_find_calendar",
                summary="Resolve a calendar name to its calendar id.",
                description=(
                    "A secondary calendar's id is an opaque "
                    "...@group.calendar.google.com address nobody types. "
                    "'primary' needs no resolution."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"calendar_name": {"type": "string"}},
                    "required": ["calendar_name"],
                },
                output_schema=_calendar,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                idempotent=True,
                resolves="calendar",
            ),
            OperationSpec(
                id="calendars.create",
                function="calendar_create_calendar",
                summary="Create a secondary calendar.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "time_zone": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["summary"],
                },
                output_schema=_calendar,
                scopes=["https://www.googleapis.com/auth/calendar"],
            ),
            OperationSpec(
                id="calendars.delete",
                function="calendar_delete_calendar",
                summary="Delete a secondary calendar and every event on it.",
                description=(
                    "Not recoverable. Refuses 'primary', which would clear the "
                    "account's main calendar rather than remove a calendar."
                ),
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"calendar_id": {"type": "string"}},
                    "required": ["calendar_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/calendar"],
            ),
        ],
        "sharing": [
            OperationSpec(
                id="sharing.list_acl",
                function="calendar_list_acl",
                summary="List who has standing access to a calendar.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "calendar_id": _calendar_id,
                        "max_results": {"type": "integer", "default": 100},
                    },
                },
                output_schema={"type": "array", "items": _access_rule},
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="sharing.share",
                function="calendar_share_calendar",
                summary="Grant standing access to a whole calendar.",
                description=(
                    "Much wider than inviting someone to one event, and "
                    "permanent until revoked. scope_type='default' makes the "
                    "calendar public."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string"},
                        "email": {"type": "string", "format": "email"},
                        "role": {
                            "type": "string",
                            "enum": [
                                "none",
                                "freeBusyReader",
                                "reader",
                                "writer",
                                "owner",
                            ],
                            "default": "reader",
                        },
                        "scope_type": {
                            "type": "string",
                            "enum": ["user", "group", "domain", "default"],
                            "default": "user",
                        },
                        "domain": {"type": "string"},
                    },
                    "required": ["calendar_id"],
                },
                output_schema=_access_rule,
                scopes=["https://www.googleapis.com/auth/calendar"],
            ),
            OperationSpec(
                id="sharing.unshare",
                function="calendar_unshare_calendar",
                summary="Revoke one person's standing access to a calendar.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string"},
                        "rule_id": {"type": "string"},
                    },
                    "required": ["calendar_id", "rule_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/calendar"],
            ),
        ],
    },
)
