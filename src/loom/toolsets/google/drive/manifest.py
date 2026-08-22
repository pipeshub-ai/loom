"""Google Drive toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from loom.toolsets.google.drive.models import (
    DriveFile,
    DrivePermission,
    SharedDrive,
)
from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GOOGLE_DRIVE_MANIFEST"]

_file = DriveFile.model_json_schema()
_file_list = {"type": "array", "items": _file}
_permission = DrivePermission.model_json_schema()
_file_id = {
    "type": "object",
    "properties": {"file_id": {"type": "string"}},
    "required": ["file_id"],
}
_content = {
    "anyOf": [{"type": "string"}, {"type": "string", "contentEncoding": "base64"}]
}

GOOGLE_DRIVE_MANIFEST = ToolsetManifest(
    id="google_drive",
    version="1.0.0",
    provider="loom",
    summary=(
        "Google Drive — find, read, upload, organise and share files and folders."
    ),
    description=(
        "Google Drive API v3 over REST. Searches files and folders across My "
        "Drive and shared drives, downloads content as LOOM Attachments, "
        "exports Google Docs/Sheets/Slides into real file formats, uploads and "
        "replaces content, creates folders, moves, copies, bins and permanently "
        "deletes, and manages who has access. Notification email is off by "
        "default so a bulk share does not mail everyone as a side effect of a "
        "default, and uploads are not auto-retried because Drive has no "
        "idempotency key and a retry would duplicate the file."
    ),
    base_url="https://www.googleapis.com/drive/v3",
    auth=AuthSpec(
        # What *this* toolset needs, which is narrower than the account's.
        # Read from the client's own SCOPES until now, where nothing outside
        # that module could see it — and `build_client` has to, because a
        # service account bakes scopes into the assertion it signs.
        scopes=(
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.file",
        ),
        client="loom.toolsets.google.drive.client:DriveClient",
        credentials="loom.toolsets.google.auth:GoogleAuth",
        # One credential across the five Google toolsets: `GoogleAuth`
        # caches a single token and merges each toolset's scopes into it,
        # so connecting once serves the set — and a second credential
        # would be a second token with a narrower scope set, which is the
        # 403 that reads as a broken credential.
        kind="oauth2",
        credential="google",
        provider="google",
        fields=(
            # Three alternatives, mirroring `GoogleCredentials.mode`. The
            # refresh trio wins over a ready-made access token when both are
            # set — an access token lives about an hour and a refresh token
            # mints them indefinitely.
            AuthField(name="GOOGLE_ACCESS_TOKEN", label="Access token", mode="token"),
            AuthField(name="GOOGLE_CLIENT_ID", label="OAuth client id", secret=False,
                      mode="refresh"),
            AuthField(name="GOOGLE_CLIENT_SECRET", label="OAuth client secret",
                      mode="refresh"),
            AuthField(name="GOOGLE_REFRESH_TOKEN", label="Refresh token", mode="refresh"),
            AuthField(name="GOOGLE_SERVICE_ACCOUNT_FILE", label="Service account JSON",
                      secret=False, mode="service_account"),
            AuthField(name="GOOGLE_IMPERSONATE_SUBJECT", label="User to impersonate",
                      secret=False, required=False),
        ),
        setup_url="https://console.cloud.google.com/apis/credentials",
        docs_url="https://developers.google.com/drive/api/guides/api-specific-auth",
    ),
    tools_module="loom.toolsets.google.drive.tools",
    egress_hosts=["www.googleapis.com", "oauth2.googleapis.com"],
    rate_limits={
        "model": (
            "per-project quota units configured in the Google Cloud console; "
            "no fixed per-second rate is published per method"
        ),
    },
    groups={
        "files": [
            OperationSpec(
                id="files.list",
                function="drive_list_files",
                summary="Search files and folders with Drive query syntax.",
                description=(
                    "Follows every page up to max_results and reports whether "
                    "it saw everything. Shared drives are included. Trashed "
                    "files are excluded unless asked for."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 100},
                        "folder_id": {"type": "string"},
                        "order_by": {
                            "type": "string",
                            "default": "modifiedTime desc",
                        },
                        "drive_id": {"type": "string"},
                        "include_trashed": {"type": "boolean", "default": False},
                    },
                },
                output_schema=_file_list,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="files.get",
                function="drive_get_file",
                summary="Fetch one file's metadata by id.",
                effect=EffectClass.READ,
                input_schema=_file_id,
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.find_folder",
                function="drive_find_folder",
                summary="Resolve a folder name to a folder id — exact match.",
                description=(
                    "Call once, then pass the id. Matching on the name inside "
                    "every query is how a search returns the wrong folder's "
                    "contents, or none, without an error."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "parent_id": {"type": "string"},
                    },
                    "required": ["name"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                idempotent=True,
                resolves="folder",
            ),
            OperationSpec(
                id="files.download",
                function="drive_download_file",
                summary="Download a file's bytes as a LOOM Attachment.",
                description=(
                    "Fails on Google-native docs, which store no bytes; the "
                    "error names the export operation instead."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["file_id"],
                },
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.export",
                function="drive_export_file",
                summary="Export a Google Doc/Sheet/Slides into a file format.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "export_mime": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["file_id"],
                },
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.upload",
                function="drive_upload_file",
                summary="Upload content as a new file.",
                description=(
                    "Not idempotent and not automatically retried — Drive "
                    "offers no idempotency key, so a retry duplicates the file."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "content": _content,
                        "mime_type": {"type": "string"},
                        "folder_id": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "content"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            ),
            OperationSpec(
                id="files.update_content",
                function="drive_update_file_content",
                summary="Replace a file's content, keeping id, links and sharing.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "content": _content,
                        "mime_type": {"type": "string"},
                    },
                    "required": ["file_id", "content"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            ),
            OperationSpec(
                id="files.create_folder",
                function="drive_create_folder",
                summary="Create a folder.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "parent_id": {"type": "string"},
                    },
                    "required": ["name"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            ),
            OperationSpec(
                id="files.rename",
                function="drive_rename_file",
                summary="Rename a file or folder.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["file_id", "name"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.move",
                function="drive_move_file",
                summary="Move a file into a folder.",
                description=(
                    "Drive has no move: this adds the new parent and removes "
                    "the old ones, so the file does not end up in both."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "folder_id": {"type": "string"},
                        "remove_from": {"type": "string"},
                    },
                    "required": ["file_id", "folder_id"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.copy",
                function="drive_copy_file",
                summary="Copy a file.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "name": {"type": "string"},
                        "folder_id": {"type": "string"},
                    },
                    "required": ["file_id"],
                },
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            ),
            OperationSpec(
                id="files.trash",
                reversible=True,
                undone_by="files.restore",
                function="drive_trash_file",
                summary="Move a file to the bin. Recoverable for 30 days.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_file_id,
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.restore",
                function="drive_restore_file",
                summary="Take a file back out of the bin.",
                effect=EffectClass.WRITE,
                input_schema=_file_id,
                output_schema=_file,
                scopes=["https://www.googleapis.com/auth/drive.file"],
                idempotent=True,
            ),
            OperationSpec(
                id="files.delete",
                                function="drive_delete_file",
                summary="Delete permanently, skipping the bin. Not recoverable.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_file_id,
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/drive"],
            ),
        ],
        "storage": [
            OperationSpec(
                id="storage.quota",
                function="drive_get_storage_quota",
                summary="Drive storage used and available, in bytes.",
                effect=EffectClass.READ,
                output_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                        "usage": {"type": "integer"},
                        "usage_in_drive": {"type": "integer"},
                    },
                },
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                idempotent=True,
            ),
        ],
        "sharing": [
            OperationSpec(
                id="sharing.share",
                idempotent=True,
                reversible=True,
                undone_by="sharing.remove_permission",
                access_control=True,
                function="drive_share_file",
                summary="Grant someone access to a file or folder.",
                description=(
                    "Notification email is off by default. type='anyone' makes "
                    "the file public to anyone with the link."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "email": {"type": "string", "format": "email"},
                        "role": {
                            "type": "string",
                            "enum": [
                                "reader",
                                "commenter",
                                "writer",
                                "fileOrganizer",
                                "organizer",
                                "owner",
                            ],
                            "default": "reader",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["user", "group", "domain", "anyone"],
                            "default": "user",
                        },
                        "domain": {"type": "string"},
                        "notify": {"type": "boolean", "default": False},
                        "message": {"type": "string"},
                    },
                    "required": ["file_id"],
                },
                output_schema=_permission,
                scopes=["https://www.googleapis.com/auth/drive"],
            ),
            OperationSpec(
                id="sharing.list_permissions",
                function="drive_list_permissions",
                summary="List who currently has access to a file.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "max_results": {"type": "integer", "default": 100},
                    },
                    "required": ["file_id"],
                },
                output_schema={"type": "array", "items": _permission},
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="sharing.remove_permission",
                idempotent=True,
                access_control=True,
                function="drive_remove_permission",
                summary="Revoke one person's access to a file.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "permission_id": {"type": "string"},
                    },
                    "required": ["file_id", "permission_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/drive"],
            ),
        ],
        "drives": [
            OperationSpec(
                id="drives.list",
                function="drive_list_shared_drives",
                summary="List the shared drives this account can see.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "default": 100},
                    },
                },
                output_schema={
                    "type": "array",
                    "items": SharedDrive.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                pagination=True,
                idempotent=True,
            ),
        ],
    },
)
