"""SharePoint Online step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.sharepoint.tools import sharepoint_list_items

    rows = await ctx.step(sharepoint_list_items, list_id=list_id, site=site)

**Naming a site.** Every tool takes ``site``, and accepts any of the four forms
Graph does — ``"root"``, ``"contoso.sharepoint.com"``,
``"contoso.sharepoint.com:/teams/hr"``, or the compound
``"contoso.sharepoint.com,{guid},{guid}"``. The path form is the one a human
can read off a browser URL, so a workflow can be written against what someone
pasted. Omit it to use ``MS_SHAREPOINT_SITE``, or the tenant's default site.

**Resolve a column before you write one.** A list item's values are keyed by a
column's *internal* name, not the name the site displays. Writing the display
name is not an error — SharePoint accepts the request and does not set the
column, so the row is created and the value is quietly missing. Call
``sharepoint_list_columns`` first and use the ``name`` it returns.

**A document library is a drive**, so the file tools here return the same
``DriveItem`` the OneDrive toolset returns, and a file moved between them keeps
one shape.

Retries are per operation. Reads retry; **uploading and creating a list item do
not**, because Graph has no idempotency key for either and a retry after a
timeout leaves a duplicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom import Retry, step
from loom.toolsets.microsoft.models import Drive, DriveItem, SharingLink
from loom.toolsets.microsoft.sharepoint.models import (
    ListColumn,
    ListItem,
    SharePointList,
    Site,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- sites -------------------------------------------------------------------


@step(retry=_READ)
async def sharepoint_get_site(site: str = "") -> Site:
    """Fetch one site by id, hostname, or server-relative path.

    Resolve a site here before working in it: the rest of the toolset takes the
    compound ``id`` this returns, which is not something anyone types from
    memory.

    Args:
        site: ``"root"`` for the tenant default, a hostname
            (``"contoso.sharepoint.com"``), a hostname and path
            (``"contoso.sharepoint.com:/teams/hr"``), or the compound id.
            Omit to use ``MS_SHAREPOINT_SITE`` or the tenant default.

    Returns:
        Site with its compound id, display name, web URL, hostname, and path.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().get_site(site)


@step(retry=_READ)
async def sharepoint_search_sites(query: str, limit: int = 50) -> Results[Site]:
    """Search the tenant for sites matching free text.

    Resolve a site name from a spec here rather than guessing its URL.

    Not available under the ``Sites.Selected`` application permission — Graph
    returns 403 rather than an empty list, so an empty result means no match
    and not a permissions problem.

    Args:
        query: Free text matched across several site properties.
        limit: Maximum sites. Defaults to 50.

    Returns:
        Results of Site.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().search_sites(query, limit=limit)


@step(retry=_READ)
async def sharepoint_list_subsites(site: str = "", limit: int = 100) -> Results[Site]:
    """List the sub-sites beneath a site.

    Args:
        site: Parent site. Omit for the configured default.
        limit: Maximum sub-sites. Defaults to 100.

    Returns:
        Results of Site.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_subsites(site, limit=limit)


# -- document libraries ------------------------------------------------------


@step(retry=_READ)
async def sharepoint_list_drives(site: str = "", limit: int = 100) -> Results[Drive]:
    """List a site's document libraries.

    Resolve a library here before reading or writing files in it: most sites
    have more than one, and the file tools default to the site's *default*
    library when no ``drive_id`` is given.

    Args:
        site: Site to look in. Omit for the configured default.
        limit: Maximum libraries. Defaults to 100.

    Returns:
        Results of Drive, each with its id, name, and quota.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_drives(site, limit=limit)


@step(retry=_READ)
async def sharepoint_list_drive_items(
    site: str = "",
    drive_id: str = "",
    item_id: str = "",
    path: str = "",
    limit: int = 200,
) -> Results[DriveItem]:
    """List the contents of a folder in a document library.

    Args:
        site: Site to look in. Omit for the configured default.
        drive_id: Library id from ``sharepoint_list_drives``. Omit to use the
            site's default library, the one shown as "Documents".
        item_id: Folder id. Takes precedence over ``path``.
        path: Folder path within the library, e.g. ``"Policies/2024"``. Omit
            both to list the library root.
        limit: Maximum items across pages. Defaults to 200.

    Returns:
        Results of DriveItem. Check ``.complete`` before reporting a count.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_drive_items(
        site=site, drive_id=drive_id, item_id=item_id, path=path, limit=limit
    )


@step(retry=_READ)
async def sharepoint_search_drive_items(
    query: str, site: str = "", drive_id: str = "", limit: int = 200
) -> Results[DriveItem]:
    """Search a document library by filename, metadata, or file content.

    Args:
        query: Text to search for. Quotes are escaped for you.
        site: Site to search in. Omit for the configured default.
        drive_id: Library to search. Omit for the site's default library.
        limit: Maximum items across pages. Defaults to 200.

    Returns:
        Results of DriveItem.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().search_drive_items(
        query, site=site, drive_id=drive_id, limit=limit
    )


@step(retry=_READ)
async def sharepoint_download_file(
    site: str = "", drive_id: str = "", item_id: str = "", path: str = ""
) -> Attachment:
    """Download a file from a document library as a LOOM Attachment.

    Fails on a folder, which holds no bytes; the error says so.

    Args:
        site: Site the file is in. Omit for the configured default.
        drive_id: Library id. Omit for the site's default library.
        item_id: File id. Takes precedence over ``path``.
        path: File path within the library.

    Returns:
        Attachment with filename, mime, size, and content. Pass it to
        ``ctx.put_artifact``, or ``att.offload(blobs)`` to keep the bytes out
        of the journal.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().download_file(
        site=site, drive_id=drive_id, item_id=item_id, path=path
    )


@step(retry=_UNSAFE_WRITE)
async def sharepoint_upload_file(
    filename: str,
    content: bytes | str,
    site: str = "",
    drive_id: str = "",
    parent_id: str = "",
    parent_path: str = "",
    on_conflict: str = "replace",
) -> DriveItem:
    """Upload a file into a document library.

    Not retried: Graph has no idempotency key for an upload, so a timeout after
    the bytes landed is indistinguishable from a failure.

    Args:
        filename: Name to store it under, with an extension.
        content: Bytes, or text which is encoded as UTF-8.
        site: Site to upload into. Omit for the configured default.
        drive_id: Library id. Omit for the site's default library.
        parent_id: Destination folder id.
        parent_path: Destination folder path. Omit both for the library root.
        on_conflict: ``"replace"`` (default), ``"rename"``, or ``"fail"``.

    Returns:
        The created DriveItem, including its id and web URL.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().upload_file(
        filename,
        content,
        site=site,
        drive_id=drive_id,
        parent_id=parent_id,
        parent_path=parent_path,
        on_conflict=on_conflict,
    )


@step(retry=_UNSAFE_WRITE)
async def sharepoint_create_folder(
    folder_name: str,
    site: str = "",
    drive_id: str = "",
    parent_id: str = "",
    parent_path: str = "",
    on_conflict: str = "fail",
) -> DriveItem:
    """Create a folder in a document library.

    Not retried: a retry with ``on_conflict="rename"`` would leave two
    folders. The default is ``"fail"`` so a second attempt errors rather than
    silently duplicating.

    Args:
        folder_name: Name for the new folder.
        site: Site to create it in. Omit for the configured default.
        drive_id: Library id. Omit for the site's default library.
        parent_id: Parent folder id.
        parent_path: Parent folder path. Omit both for the library root.
        on_conflict: ``"fail"`` (default), ``"rename"``, or ``"replace"``.

    Returns:
        The created folder as a DriveItem.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().create_folder(
        folder_name,
        site=site,
        drive_id=drive_id,
        parent_id=parent_id,
        parent_path=parent_path,
        on_conflict=on_conflict,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def sharepoint_delete_file(
    site: str = "", drive_id: str = "", item_id: str = "", path: str = ""
) -> bool:
    """Move a file or folder in a document library to the recycle bin.

    Recoverable — a site administrator can restore it.

    Retried once: deleting something already deleted is a 404, not a second
    deletion.

    Args:
        site: Site the file is in. Omit for the configured default.
        drive_id: Library id. Omit for the site's default library.
        item_id: Item id. Takes precedence over ``path``.
        path: Item path within the library. One of the two is required —
            passing neither would address the library root.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().delete_item(
        site=site, drive_id=drive_id, item_id=item_id, path=path
    )


@step(retry=_UNSAFE_WRITE)
async def sharepoint_create_share_link(
    site: str = "",
    drive_id: str = "",
    item_id: str = "",
    path: str = "",
    link_type: str = "view",
    scope: str = "organization",
    expires: str = "",
) -> SharingLink:
    """Create a sharing link for a file in a document library.

    Defaults to ``"organization"`` scope rather than ``"anonymous"``: a link
    that works for anyone on the internet is not a safe thing to produce by
    omission.

    Args:
        site: Site the file is in. Omit for the configured default.
        drive_id: Library id. Omit for the site's default library.
        item_id: File id. Takes precedence over ``path``.
        path: File path within the library.
        link_type: ``"view"`` or ``"edit"``.
        scope: ``"organization"`` (default), ``"anonymous"``, or ``"users"``.
        expires: ISO-8601 expiry, e.g. ``"2026-01-31T00:00:00Z"``.

    Returns:
        SharingLink with the URL, type, scope, and roles granted.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().create_share_link(
        site=site,
        drive_id=drive_id,
        item_id=item_id,
        path=path,
        link_type=link_type,
        scope=scope,
        expires=expires,
    )


# -- lists -------------------------------------------------------------------


@step(retry=_READ)
async def sharepoint_list_lists(
    site: str = "", limit: int = 100, include_hidden: bool = False
) -> Results[SharePointList]:
    """List a site's lists and document libraries.

    Resolve a list here before reading or writing its items.

    Args:
        site: Site to look in. Omit for the configured default.
        limit: Maximum lists. Defaults to 100.
        include_hidden: True also returns SharePoint's internal lists, which
            are hidden from the UI and are almost never what a workflow means.

    Returns:
        Results of SharePointList. ``template`` says which kind each is — a
        ``"documentLibrary"`` is better reached through the file tools than
        the list-item ones.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_lists(
        site, limit=limit, include_hidden=include_hidden
    )


@step(retry=_READ)
async def sharepoint_get_list(list_id: str, site: str = "") -> SharePointList:
    """Fetch one list by id or name.

    Args:
        list_id: List id, or its name.
        site: Site the list is in. Omit for the configured default.

    Returns:
        SharePointList with id, display name, template, and web URL.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().get_list(list_id, site)


@step(retry=_READ)
async def sharepoint_list_columns(
    list_id: str, site: str = "", limit: int = 100
) -> Results[ListColumn]:
    """List a list's columns, with both their internal and display names.

    **Call this before writing an item.** A ``fields`` dict is keyed by the
    internal ``name``, which is not what the site shows — a column displayed as
    "Due Date" is keyed ``DueDate`` or ``Due_x0020_Date``. Writing the display
    name is accepted and silently ignored, so the row is created with the value
    missing and nothing reports an error.

    Args:
        list_id: List id, or its name.
        site: Site the list is in. Omit for the configured default.
        limit: Maximum columns. Defaults to 100.

    Returns:
        Results of ListColumn with ``name`` (use this as the field key),
        ``display_name``, ``type``, ``required``, ``read_only``, and for a
        choice column the accepted ``choices``.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_columns(list_id, site, limit=limit)


@step(retry=_READ)
async def sharepoint_list_items(
    list_id: str,
    site: str = "",
    limit: int = 200,
    filter_query: str = "",
    order_by: str = "",
) -> Results[ListItem]:
    """List a list's items, with their column values.

    Field values are always expanded — Graph hides them by default, and an item
    without them is ids and timestamps and no data.

    Args:
        list_id: List id, or its name.
        site: Site the list is in. Omit for the configured default.
        limit: Maximum items across pages. Defaults to 200.
        filter_query: OData ``$filter``, written against internal column names
            under ``fields/``, e.g. ``"fields/Status eq 'Open'"``. Resolve the
            column names with ``sharepoint_list_columns`` first.
        order_by: OData ``$orderby``, e.g. ``"fields/Created desc"``.

    Returns:
        Results of ListItem, each with a ``fields`` dict keyed by internal
        column name.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().list_items(
        list_id, site, limit=limit, filter_query=filter_query, order_by=order_by
    )


@step(retry=_READ)
async def sharepoint_get_list_item(
    list_id: str, item_id: str, site: str = ""
) -> ListItem:
    """Fetch one list item, with its column values.

    Args:
        list_id: List id, or its name.
        item_id: Item id — the integer SharePoint shows as ``ID``.
        site: Site the list is in. Omit for the configured default.

    Returns:
        ListItem with a ``fields`` dict keyed by internal column name.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().get_list_item(list_id, item_id, site)


@step(retry=_UNSAFE_WRITE)
async def sharepoint_create_list_item(
    list_id: str, fields: dict[str, Any], site: str = ""
) -> ListItem:
    """Add a row to a list.

    Not retried: SharePoint has no idempotency key for a list item, so a retry
    after a timeout adds a second row.

    Args:
        list_id: List id, or its name.
        fields: Column values keyed by **internal** column name, from
            ``sharepoint_list_columns`` — not the names the site displays.
        site: Site the list is in. Omit for the configured default.

    Returns:
        The created ListItem.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().create_list_item(list_id, fields, site)


@step(retry=_IDEMPOTENT_WRITE)
async def sharepoint_update_list_item(
    list_id: str, item_id: str, fields: dict[str, Any], site: str = ""
) -> ListItem:
    """Change column values on an existing list item.

    Retried once: setting the same fields on the same item twice leaves the
    same row, so a repeat is safe.

    Args:
        list_id: List id, or its name.
        item_id: Item id.
        fields: Column values to set, keyed by **internal** column name.
            Columns not named are left alone.
        site: Site the list is in. Omit for the configured default.

    Returns:
        The updated ListItem.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().update_list_item(
        list_id, item_id, fields, site
    )


@step(retry=_IDEMPOTENT_WRITE)
async def sharepoint_delete_list_item(
    list_id: str, item_id: str, site: str = ""
) -> bool:
    """Delete a list item.

    Retried once: deleting something already deleted is a 404, not a second
    deletion.

    Args:
        list_id: List id, or its name.
        item_id: Item id.
        site: Site the list is in. Omit for the configured default.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.sharepoint.client import get_default_client

    return await get_default_client().delete_list_item(list_id, item_id, site)
