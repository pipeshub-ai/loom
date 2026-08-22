"""Outlook calendar ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.microsoft.outlook.models import (
    Calendar,
    CalendarEvent,
    MeetingSuggestion,
    ScheduleSlot,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


OUTLOOK_CALENDAR_MANIFEST = ToolsetManifest(
    id="outlook_calendar",
    version="1.0.0",
    summary="Outlook calendar — events, scheduling, and availability.",
    description=(
        "Microsoft Graph v1.0, Exchange Online. Separate from outlook_mail so a "
        "workflow that reads a calendar need not hold a mail-send scope. "
        "USE outlook_list_calendar_view TO SEE WHAT IS HAPPENING: it expands "
        "recurring series into occurrences over a window, while "
        "outlook_list_events returns series MASTERS — a weekly standup appears "
        "there once with a recurrence rule and not on the days it occurs, so "
        "asking 'what is on Tuesday' over the events listing returns a short "
        "answer that looks correct. Times come back in UTC unless a timezone "
        "argument is given, and that argument does NOT reinterpret the window: "
        "include an offset in start/end. Creating an event with attendees "
        "emails them, so those operations are not retried; use "
        "outlook_cancel_event rather than delete when other people were "
        "invited. Set MS_OUTLOOK_USER under app-only credentials."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth=AuthSpec(
        client="loom.toolsets.microsoft.outlook.calendar.client:OutlookCalendarClient",
        credentials="loom.toolsets.microsoft.auth:MicrosoftAuth",
        # One credential across the six Graph toolsets, for the reason the
        # Google five share one. `MS_*_USER` exists because `/me` does not
        # resolve under app-only credentials — see toolsets/CLAUDE.md.
        kind="oauth2",
        credential="microsoft",
        provider="microsoft",
        scopes=("offline_access",),
        fields=(
            # Three alternatives, mirroring `MicrosoftCredentials.mode`. The
            # AZURE_* trio is the same credential under the names the Azure
            # SDKs already put in an environment, so it is a mode rather than
            # three more required variables.
            AuthField(name="MS_TENANT_ID", label="Tenant id", secret=False, mode="app"),
            AuthField(name="MS_CLIENT_ID", label="Application (client) id",
                      secret=False, mode="app"),
            AuthField(name="MS_CLIENT_SECRET", label="Client secret", mode="app"),
            AuthField(name="AZURE_TENANT_ID", label="Tenant id (Azure SDK name)",
                      secret=False, mode="azure"),
            AuthField(name="AZURE_CLIENT_ID", label="Client id (Azure SDK name)",
                      secret=False, mode="azure"),
            AuthField(name="AZURE_CLIENT_SECRET", label="Client secret (Azure SDK name)",
                      mode="azure"),
            AuthField(name="MS_GRAPH_ACCESS_TOKEN", label="Graph access token",
                      mode="token"),
            # Adds delegated identity to the app mode rather than replacing it:
            # without it the same three variables authenticate the application.
            AuthField(name="MS_REFRESH_TOKEN", label="Refresh token (delegated)",
                      required=False),
            AuthField(name="MS_AUTHORITY_HOST", label="Authority host (sovereign cloud)",
                      secret=False, required=False),
            AuthField(name="MS_OUTLOOK_USER", arg="user_id", label="Mailbox to act on (app-only)",
                      secret=False, required=False),
            AuthField(name="MS_OUTLOOK_TIMEZONE", arg="timezone", label="Preferred timezone",
                      secret=False, required=False),
        ),
        docs_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
    ),
    tools_module="loom.toolsets.microsoft.outlook.calendar.tools",
    egress_hosts=["graph.microsoft.com", "login.microsoftonline.com"],
    rate_limits={
        "model": (
            "dynamic per-workload throttling; honour the Retry-After header "
            "on a 429 rather than assuming a fixed rate"
        ),
        "source": "learn.microsoft.com/en-us/graph/throttling",
    },
    groups={
        "calendars": [
            OperationSpec(
                id="calendars.list",
                function="outlook_list_calendars",
                summary="List the calendars this account can reach.",
                description=(
                    "Resolve a non-default calendar here — a secondary "
                    "calendar's id is opaque, not its name. can_edit is worth "
                    "checking before writing."
                ),
                resolves="calendar",
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(Calendar),
            ),
        ],
        "events": [
            OperationSpec(
                id="events.calendar_view",
                function="outlook_list_calendar_view",
                summary="What is on the calendar between two instants.",
                description=(
                    "THE read for 'what is happening': recurrences are "
                    "expanded into occurrences. Include a UTC offset in start "
                    "and end — a naive value is read as UTC whatever timezone "
                    "says, silently shifting the window."
                ),
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(CalendarEvent),
            ),
            OperationSpec(
                id="events.list",
                function="outlook_list_events",
                summary="List event records — series masters, not occurrences.",
                description=(
                    "For reaching a recurring series to edit it. For a day's "
                    "schedule use events.calendar_view instead."
                ),
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(CalendarEvent),
            ),
            OperationSpec(
                id="events.get",
                function="outlook_get_event",
                summary="Fetch one event.",
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                output_schema=CalendarEvent.model_json_schema(),
            ),
            OperationSpec(
                id="events.create",
                function="outlook_create_event",
                summary="Create an event, optionally with a Teams meeting.",
                description=(
                    "add_teams_meeting=True is the only way to get a join "
                    "link — event and meeting are created in one call. Not "
                    "retried: a retry invites everyone to a second meeting."
                ),
                effect=EffectClass.WRITE,
                scopes=["Calendars.ReadWrite"],
                output_schema=CalendarEvent.model_json_schema(),
            ),
            OperationSpec(
                id="events.update",
                function="outlook_update_event",
                summary="Change an event's subject, times, body, or location.",
                effect=EffectClass.WRITE,
                scopes=["Calendars.ReadWrite"],
                idempotent=True,
                output_schema=CalendarEvent.model_json_schema(),
            ),
            OperationSpec(
                id="events.respond",
                function="outlook_respond_to_event",
                summary="Accept, decline, or tentatively accept an invitation.",
                description="Emails the organiser, so not retried.",
                effect=EffectClass.WRITE,
                scopes=["Calendars.ReadWrite"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="events.cancel",
                function="outlook_cancel_event",
                summary="Cancel a meeting and notify the attendees.",
                description=(
                    "Use this rather than delete when other people were "
                    "invited: deleting leaves their invitations in place."
                ),
                effect=EffectClass.WRITE,
                scopes=["Calendars.ReadWrite"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="events.delete",
                function="outlook_delete_event",
                summary="Delete an event from this calendar.",
                description="For a meeting with attendees, cancel instead.",
                effect=EffectClass.DESTRUCTIVE,
                scopes=["Calendars.ReadWrite"],
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "availability": [
            OperationSpec(
                id="availability.find_meeting_times",
                function="outlook_find_meeting_times",
                summary="Ask Exchange when a set of people could meet.",
                description=(
                    "An empty list means no slot met the constraints, which "
                    "is an answer rather than an error."
                ),
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                output_schema=_array(MeetingSuggestion),
            ),
            OperationSpec(
                id="availability.get_schedule",
                function="outlook_get_schedule",
                summary="Read free/busy for a set of mailboxes.",
                description=(
                    "Check the error field: a mailbox the token cannot read "
                    "comes back with an error, and treating it as free would "
                    "schedule over somebody."
                ),
                effect=EffectClass.READ,
                scopes=["Calendars.Read"],
                idempotent=True,
                output_schema=_array(ScheduleSlot),
            ),
        ],
    },
)
