"""Zoom toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.zoom.models import (
    ZoomMeeting,
    ZoomParticipant,
    ZoomRecording,
    ZoomUser,
)

__all__ = ["ZOOM_MANIFEST"]

_meeting = ZoomMeeting.model_json_schema()
_user = ZoomUser.model_json_schema()
_recording = ZoomRecording.model_json_schema()
_meeting_id = {
    "type": ["integer", "string"],
    "description": "The NUMERIC meeting id — the series, not one occurrence.",
}
_meeting_uuid = {
    "type": "string",
    "description": (
        "meeting.uuid — ONE occurrence. Not the numeric id, which answers for "
        "whichever occurrence Zoom picks."
    ),
}
_user_id = {"type": "string", "default": "me"}

ZOOM_MANIFEST = ToolsetManifest(
    id="zoom",
    version="1.0.0",
    provider="loom",
    summary=(
        "Zoom — schedule and manage meetings, and read attendance and "
        "recordings afterwards."
    ),
    description=(
        "Zoom API v2 over REST. Schedules, updates and cancels meetings, lists "
        "what is on a host's calendar, and reads what a finished meeting left "
        "behind: who attended, for how long, and the cloud recordings with "
        "their download URLs. Resolves an email address to a Zoom user. Note "
        "that a meeting carries two identifiers — a numeric id for the series "
        "and a uuid for one occurrence — and past-meeting operations take the "
        "uuid. Creating a meeting is deliberately not retried: Zoom offers no "
        "idempotency key, so a retry after a timeout schedules a second "
        "meeting with a different join link."
    ),
    base_url="https://api.zoom.us/v2",
    auth=AuthSpec(
        client="loom.toolsets.zoom.client:ZoomClient",
        credentials="loom.toolsets.zoom.auth:ZoomAuth",
        # The default grant is Server-to-Server, which has **no refresh
        # token**: the client id and secret are the durable credential and an
        # hourly token is minted from them. The `zoom` provider covers the
        # user-delegated case, which is the one a browser flow can do.
        kind="oauth2",
        credential="zoom",
        provider="zoom",
        fields=(
            # Two flows, and `ZoomCredentials.mode` has always known it:
            # Server-to-Server mints hourly tokens from the trio, or a
            # ready-made access token is used as-is. Declared flatly required
            # with no mode, the trio made a token-only deployment report as
            # missing three variables — which was merely a wrong message until
            # `build_client` began refusing to construct on it.
            AuthField(name="ZOOM_ACCOUNT_ID", label="Account id", secret=False,
                      mode="server_to_server"),
            AuthField(name="ZOOM_CLIENT_ID", label="Client id", secret=False,
                      mode="server_to_server"),
            AuthField(name="ZOOM_CLIENT_SECRET", label="Client secret",
                      mode="server_to_server"),
            AuthField(name="ZOOM_ACCESS_TOKEN", label="Access token",
                      mode="token"),
        ),
        docs_url="https://developers.zoom.us/docs/integrations/oauth/",
    ),
    tools_module="loom.toolsets.zoom.tools",
    egress_hosts=["api.zoom.us", "zoom.us"],
    rate_limits={"per_second_and_daily": "tiered by endpoint weight"},
    groups={
        "meetings": [
            OperationSpec(
                id="meetings.list",
                function="zoom_list_meetings",
                summary="List a user's meetings.",
                description=(
                    "'scheduled' lists what is set up rather than what is "
                    "happening: it excludes instant meetings and includes "
                    "recurring ones with no near occurrence."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "user_id": _user_id,
                        "meeting_type": {
                            "type": "string",
                            "enum": [
                                "scheduled",
                                "live",
                                "upcoming",
                                "upcoming_meetings",
                                "previous_meetings",
                            ],
                            "default": "scheduled",
                        },
                        "max_results": {"type": "integer", "default": 100},
                    },
                },
                output_schema={"type": "array", "items": _meeting},
                scopes=["meeting:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="meetings.get",
                function="zoom_get_meeting",
                summary="Fetch one meeting by its numeric id.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"meeting_id": _meeting_id},
                    "required": ["meeting_id"],
                },
                output_schema=_meeting,
                scopes=["meeting:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="meetings.create",
                function="zoom_create_meeting",
                summary="Schedule a meeting.",
                description=(
                    "Not idempotent and not automatically retried — a retry "
                    "schedules a second meeting with a different join link. "
                    "Send the returned join_url; start_url is a host "
                    "credential."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "start_time": {
                            "type": "string",
                            "description": "YYYY-MM-DDTHH:MM:SSZ, from ctx.now().",
                        },
                        "duration": {"type": "integer", "default": 30},
                        "user_id": _user_id,
                        "timezone": {"type": "string", "default": "UTC"},
                        "agenda": {"type": "string"},
                        "password": {"type": "string"},
                        "meeting_type": {"type": "integer", "default": 2},
                        "settings": {"type": "object"},
                    },
                    "required": ["topic"],
                },
                output_schema=_meeting,
                scopes=["meeting:write"],
            ),
            OperationSpec(
                id="meetings.update",
                function="zoom_update_meeting",
                summary="Patch fields on an existing meeting.",
                description="Zoom answers 204, so only the id comes back.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "meeting_id": _meeting_id,
                        "fields": {"type": "object"},
                    },
                    "required": ["meeting_id", "fields"],
                },
                output_schema={"type": "string"},
                scopes=["meeting:write"],
                idempotent=True,
            ),
            OperationSpec(
                id="meetings.delete",
                idempotent=True,
                function="zoom_delete_meeting",
                summary="Cancel a meeting. Not recoverable.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "meeting_id": _meeting_id,
                        "notify": {"type": "boolean", "default": False},
                    },
                    "required": ["meeting_id"],
                },
                output_schema={"type": "string"},
                scopes=["meeting:write"],
            ),
        ],
        "past": [
            OperationSpec(
                id="past.get",
                function="zoom_get_past_meeting",
                summary="Details of one finished meeting occurrence.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"meeting_uuid": _meeting_uuid},
                    "required": ["meeting_uuid"],
                },
                output_schema={"type": "object"},
                scopes=["meeting:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="past.participants",
                function="zoom_list_participants",
                summary="Who attended a finished meeting.",
                description=(
                    "Takes the occurrence uuid, not the numeric id. One row "
                    "per session, so someone who rejoined appears twice — "
                    "group before counting."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "meeting_uuid": _meeting_uuid,
                        "max_results": {"type": "integer", "default": 300},
                    },
                    "required": ["meeting_uuid"],
                },
                output_schema={
                    "type": "array",
                    "items": ZoomParticipant.model_json_schema(),
                },
                scopes=["meeting:read", "report:read:admin"],
                pagination=True,
                idempotent=True,
            ),
        ],
        "recordings": [
            OperationSpec(
                id="recordings.list",
                function="zoom_list_recordings",
                summary="List a user's cloud recordings.",
                description=(
                    "Zoom spans at most one month per query and silently "
                    "narrows a wider window, so a year-long range returns one "
                    "month and no error."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "user_id": _user_id,
                        "start": {"type": "string", "description": "YYYY-MM-DD"},
                        "end": {"type": "string", "description": "YYYY-MM-DD"},
                        "max_results": {"type": "integer", "default": 100},
                    },
                },
                output_schema={"type": "array", "items": _recording},
                scopes=["recording:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="recordings.get",
                function="zoom_get_recording",
                summary="Recordings for one meeting.",
                description=(
                    "Check each file's is_ready — Zoom lists a file while it "
                    "is still processing it."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"meeting_id": _meeting_id},
                    "required": ["meeting_id"],
                },
                output_schema=_recording,
                scopes=["recording:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="recordings.download",
                function="zoom_download_recording",
                summary="Download a recording file as a LOOM Attachment.",
                description=(
                    "The URL needs the bearer token; a plain fetch of it does "
                    "not work."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "download_url": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["download_url", "filename"],
                },
                scopes=["recording:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="recordings.delete",
                function="zoom_delete_recording",
                summary="Delete a meeting's cloud recordings.",
                description="Goes to the account trash for 30 days.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"meeting_id": _meeting_id},
                    "required": ["meeting_id"],
                },
                output_schema={"type": "string"},
                scopes=["recording:write"],
                idempotent=True,
            ),
        ],
        "users": [
            OperationSpec(
                id="users.list",
                function="zoom_list_users",
                summary="List account members.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["active", "inactive", "pending"],
                            "default": "active",
                        },
                        "max_results": {"type": "integer", "default": 300},
                    },
                },
                output_schema={"type": "array", "items": _user},
                scopes=["user:read"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="users.get",
                function="zoom_get_user",
                summary="Fetch one user. 'me' is the authenticated account.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"user_id": _user_id},
                },
                output_schema=_user,
                scopes=["user:read"],
                idempotent=True,
            ),
            OperationSpec(
                id="users.find_by_email",
                function="zoom_find_user_by_email",
                summary="Resolve an email address to a Zoom user.",
                description=(
                    "Hosting a meeting takes a user id. Resolve once rather "
                    "than passing an address into every call and discovering "
                    "at the end that nobody matched."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"email": {"type": "string", "format": "email"}},
                    "required": ["email"],
                },
                output_schema=_user,
                scopes=["user:read"],
                idempotent=True,
                resolves="user",
            ),
        ],
    },
)
