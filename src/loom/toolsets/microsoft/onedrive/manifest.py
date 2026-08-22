"""OneDrive ToolsetManifest — pure metadata, no client import."""

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
from loom.toolsets.microsoft.models import (
    DeltaPage,
    Drive,
    DriveItem,
    MicrosoftUser,
    Permission,
    SharingLink,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


ONEDRIVE_MANIFEST = ToolsetManifest(
    id="onedrive",
    version="1.0.0",
    summary="OneDrive — files, folders, sharing, and change tracking.",
    description=(
        "Microsoft Graph v1.0. Browse, search, upload, download, move, copy and "
        "delete files; create sharing links and invite people; track changes "
        "with delta instead of polling. Every item is addressable either by "
        "item_id or by path relative to the drive root — pass one, or neither "
        "for the root itself. Under an app-only token there is no signed-in "
        "user, so '/me' does not exist: set MS_ONEDRIVE_USER or "
        "MS_ONEDRIVE_DRIVE_ID to say which drive to act on."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth=AuthSpec(
        client="loom.toolsets.microsoft.onedrive.client:OneDriveClient",
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
            AuthField(name="MS_ONEDRIVE_USER", arg="user_id", label="User to act as (app-only)",
                      secret=False, required=False),
            AuthField(name="MS_ONEDRIVE_DRIVE_ID", arg="drive_id", label="Drive id", secret=False,
                      required=False),
        ),
        docs_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
    ),
    tools_module="loom.toolsets.microsoft.onedrive.tools",
    egress_hosts=[
        "graph.microsoft.com",
        "login.microsoftonline.com",
        "*.sharepoint.com",
        "*.up.1drv.com",
    ],
    rate_limits={
        "model": (
            "dynamic per-workload throttling; honour the Retry-After header "
            "on a 429 rather than assuming a fixed rate"
        ),
        "source": "learn.microsoft.com/en-us/graph/throttling",
    },
    groups={
        "drive": [
            OperationSpec(
                id="drive.get",
                function="onedrive_get_drive",
                summary="Fetch the drive, including quota used and remaining.",
                description=(
                    "Check quota_state before a bulk upload: 'exceeded' turns "
                    "every write into a 507."
                ),
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                output_schema=Drive.model_json_schema(),
            ),
            OperationSpec(
                id="drive.whoami",
                function="onedrive_whoami",
                summary="The person these credentials authenticate as.",
                description=(
                    "Resolve a person to their userPrincipalName here; that is "
                    "what addresses their drive elsewhere. Fails under an "
                    "app-only token, which has no signed-in user."
                ),
                resolves="user",
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                output_schema=MicrosoftUser.model_json_schema(),
            ),
        ],
        "files": [
            OperationSpec(
                id="files.list",
                function="onedrive_list_children",
                summary="List a folder's contents, by folder id or path.",
                description="No arguments lists the drive root.",
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="files.get",
                function="onedrive_get_item",
                summary="Fetch one file or folder by id or path.",
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="files.search",
                function="onedrive_search_items",
                summary="Search by filename, metadata, or file content.",
                description=(
                    "A content search, not a name glob. Optionally scoped to a "
                    "folder subtree."
                ),
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="files.recent",
                function="onedrive_list_recent",
                summary="Files this account touched recently, newest first.",
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="files.shared_with_me",
                function="onedrive_list_shared_with_me",
                summary="Files other people have shared with this user.",
                description=(
                    "Each item lives in ANOTHER person's drive — address it "
                    "with the drive_id on the item, not this toolset's drive."
                ),
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="files.changes",
                function="onedrive_list_changes",
                summary="What changed since a stored delta link.",
                description=(
                    "Use instead of polling — Graph names repeated polling as a "
                    "leading cause of throttling. token='latest' returns an "
                    "empty page plus a link that starts watching from now. A "
                    "deletion arrives as an entry with deleted=True."
                ),
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                output_schema=DeltaPage.model_json_schema(),
            ),
            OperationSpec(
                id="files.download",
                function="onedrive_download_file",
                summary="Download a file's bytes as a LOOM Attachment.",
                description="Fails on a folder, which holds no bytes.",
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                output_schema={"type": "object", "title": "Attachment"},
            ),
            OperationSpec(
                id="files.upload",
                function="onedrive_upload_file",
                summary="Upload a small file in one request.",
                description=(
                    "Not retried: no idempotency key. Refuses over 10 MiB and "
                    "names onedrive_upload_large_file."
                ),
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="files.upload_large",
                function="onedrive_upload_large_file",
                summary="Upload a large file through a resumable session.",
                description="Fragments of 5 MiB, sent in order, resumable.",
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="files.create_folder",
                function="onedrive_create_folder",
                summary="Create a folder.",
                description="Not retried; defaults to failing on a name clash.",
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="files.move",
                function="onedrive_move_item",
                summary="Move a file or folder, rename it, or both.",
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                idempotent=True,
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="files.copy",
                function="onedrive_copy_item",
                summary="Start a copy; returns a monitor URL, not an item.",
                description=(
                    "Copying is asynchronous in Graph. The returned URL reports "
                    "progress and, on completion, the new item's id."
                ),
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema={"type": "string"},
            ),
            OperationSpec(
                id="files.delete",
                function="onedrive_delete_item",
                summary="Move a file or folder to the recycle bin.",
                effect=EffectClass.DESTRUCTIVE,
                scopes=["Files.ReadWrite"],
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "sharing": [
            OperationSpec(
                id="sharing.list_permissions",
                function="onedrive_list_permissions",
                summary="List who can reach an item, and how.",
                description=(
                    "inherited=True means the access comes from an ancestor and "
                    "cannot be revoked on this item."
                ),
                effect=EffectClass.READ,
                scopes=["Files.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(Permission),
            ),
            OperationSpec(
                id="sharing.create_link",
                access_control=True,
                function="onedrive_create_share_link",
                summary="Create a sharing link for an item.",
                description=(
                    "Defaults to organization scope, not anonymous — ask for "
                    "anonymous explicitly. Not retried."
                ),
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema=SharingLink.model_json_schema(),
            ),
            OperationSpec(
                id="sharing.invite",
                access_control=True,
                function="onedrive_invite",
                summary="Grant named people access, optionally emailing them.",
                description="Not retried: a retry sends a second invitation.",
                effect=EffectClass.WRITE,
                scopes=["Files.ReadWrite"],
                output_schema=_array(Permission),
            ),
        ],
    },
)
