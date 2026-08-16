"""SharePoint Online ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.microsoft.models import Drive, DriveItem, SharingLink
from loom.toolsets.microsoft.sharepoint.models import (
    ListColumn,
    ListItem,
    SharePointList,
    Site,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


SHAREPOINT_MANIFEST = ToolsetManifest(
    id="sharepoint",
    version="1.0.0",
    summary="SharePoint Online — sites, document libraries, and lists.",
    description=(
        "Microsoft Graph v1.0. Find sites, browse and search document "
        "libraries, upload and download files, and read and write list items. "
        "A site is named by 'root', a hostname, a hostname and path "
        "('contoso.sharepoint.com:/teams/hr'), or its compound id — resolve one "
        "with sharepoint_search_sites before working in it. A document library "
        "is a drive, so file operations return the same DriveItem the OneDrive "
        "toolset returns. List item values are keyed by a column's INTERNAL "
        "name: call sharepoint_list_columns first, because writing a display "
        "name is accepted and silently sets nothing."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth={
        "type": "oauth2",
        "fields": [
            "MS_TENANT_ID",
            "MS_CLIENT_ID",
            "MS_CLIENT_SECRET",
            "MS_REFRESH_TOKEN",
            "MS_GRAPH_ACCESS_TOKEN",
            # Read by the shared auth layer, so declared here: the Azure SDK
            # trio is what a host already has in its environment, and
            # MS_AUTHORITY_HOST is the only way to reach a national cloud.
            # Omitting them told `loom toolset` users to set MS_* variables
            # they did not need.
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "MS_AUTHORITY_HOST",
            "MS_SHAREPOINT_SITE",
        ],
    },
    tools_module="loom.toolsets.microsoft.sharepoint.tools",
    egress_hosts=[
        "graph.microsoft.com",
        "login.microsoftonline.com",
        "*.sharepoint.com",
    ],
    groups={
        "sites": [
            OperationSpec(
                id="sites.search",
                function="sharepoint_search_sites",
                summary="Search the tenant for sites by free text.",
                description=(
                    "Resolve a site named in a spec here rather than guessing "
                    "its URL. Returns 403, not an empty list, under the "
                    "Sites.Selected application permission."
                ),
                resolves="site",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Site),
            ),
            OperationSpec(
                id="sites.get",
                function="sharepoint_get_site",
                summary="Fetch a site by id, hostname, or path.",
                description=(
                    "Accepts 'root', 'host', 'host:/teams/x', or the compound "
                    "id. Resolve before working in it — the rest of the "
                    "toolset takes the id this returns."
                ),
                resolves="site",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=Site.model_json_schema(),
            ),
            OperationSpec(
                id="sites.list_subsites",
                function="sharepoint_list_subsites",
                summary="List the sub-sites beneath a site.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Site),
            ),
        ],
        "libraries": [
            OperationSpec(
                id="libraries.list",
                function="sharepoint_list_drives",
                summary="List a site's document libraries.",
                description=(
                    "Resolve a library before reading or writing files; the "
                    "file tools fall back to the site's default library."
                ),
                resolves="drive",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Drive),
            ),
            OperationSpec(
                id="libraries.list_items",
                function="sharepoint_list_drive_items",
                summary="List a folder's contents in a document library.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="libraries.search",
                function="sharepoint_search_drive_items",
                summary="Search a library by filename, metadata, or content.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(DriveItem),
            ),
            OperationSpec(
                id="libraries.download",
                function="sharepoint_download_file",
                summary="Download a file as a LOOM Attachment.",
                description="Fails on a folder, which holds no bytes.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema={"type": "object", "title": "Attachment"},
            ),
            OperationSpec(
                id="libraries.upload",
                function="sharepoint_upload_file",
                summary="Upload a file into a document library.",
                description="Not retried: no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="libraries.create_folder",
                function="sharepoint_create_folder",
                summary="Create a folder in a document library.",
                description="Not retried; defaults to failing on a name clash.",
                effect=EffectClass.WRITE,
                output_schema=DriveItem.model_json_schema(),
            ),
            OperationSpec(
                id="libraries.delete",
                function="sharepoint_delete_file",
                summary="Move a library file or folder to the recycle bin.",
                description=(
                    "Recoverable by a site administrator. Needs item_id or "
                    "path — neither would address the library root."
                ),
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="libraries.create_link",
                function="sharepoint_create_share_link",
                summary="Create a sharing link for a file.",
                description=(
                    "Defaults to organization scope, not anonymous — ask for "
                    "anonymous explicitly."
                ),
                effect=EffectClass.WRITE,
                output_schema=SharingLink.model_json_schema(),
            ),
        ],
        "lists": [
            OperationSpec(
                id="lists.list",
                function="sharepoint_list_lists",
                summary="List a site's lists and libraries.",
                description="Resolve a list before reading or writing items.",
                resolves="list",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(SharePointList),
            ),
            OperationSpec(
                id="lists.get",
                function="sharepoint_get_list",
                summary="Fetch one list by id or name.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=SharePointList.model_json_schema(),
            ),
            OperationSpec(
                id="lists.columns",
                function="sharepoint_list_columns",
                summary="List a list's columns, internal and display names.",
                description=(
                    "Call before writing an item. Field values are keyed by the "
                    "INTERNAL name — 'Due Date' is keyed DueDate or "
                    "Due_x0020_Date — and writing a display name is accepted "
                    "and silently sets nothing. Also gives a choice column's "
                    "accepted values."
                ),
                resolves="column",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ListColumn),
            ),
            OperationSpec(
                id="lists.items",
                function="sharepoint_list_items",
                summary="List a list's items with their column values.",
                description=(
                    "$filter and $orderby are written against internal column "
                    "names under 'fields/', e.g. \"fields/Status eq 'Open'\"."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ListItem),
            ),
            OperationSpec(
                id="lists.get_item",
                function="sharepoint_get_list_item",
                summary="Fetch one list item with its column values.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=ListItem.model_json_schema(),
            ),
            OperationSpec(
                id="lists.create_item",
                function="sharepoint_create_list_item",
                summary="Add a row to a list.",
                description=(
                    "fields is keyed by internal column name. Not retried: a "
                    "retry adds a second row."
                ),
                effect=EffectClass.WRITE,
                output_schema=ListItem.model_json_schema(),
            ),
            OperationSpec(
                id="lists.update_item",
                function="sharepoint_update_list_item",
                summary="Change column values on a list item.",
                description="Columns not named are left alone.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=ListItem.model_json_schema(),
            ),
            OperationSpec(
                id="lists.delete_item",
                function="sharepoint_delete_list_item",
                summary="Delete a list item.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
    },
)
