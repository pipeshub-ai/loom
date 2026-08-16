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
    tools_module="loom.toolsets.google.drive.tools",
    egress_hosts=["www.googleapis.com", "oauth2.googleapis.com"],
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
