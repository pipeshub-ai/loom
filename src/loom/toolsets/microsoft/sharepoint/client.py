"""SharePoint Online over Microsoft Graph — pure httpx, no vendor SDK.

Two things are worth reading before the methods.

**A document library is a drive.** File operations here return the same
``Drive``/``DriveItem`` models OneDrive returns, from the same Graph endpoints,
because they *are* the same endpoints. A workflow that moves a file from a
person's OneDrive into a team library reads one shape throughout.

**A site is addressable four ways**, and :meth:`SharePointClient._site` accepts
all of them, because a workflow author has whichever one their browser gave
them and the compound id is not something anyone types from memory.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loom.toolsets.microsoft.addressing import child_address, item_address
from loom.toolsets.microsoft.auth import (
    GRAPH_BASE_URL,
    MicrosoftAuth,
    get_default_auth,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.models import Drive, DriveItem, SharingLink
from loom.toolsets.microsoft.sharepoint.models import (
    ListColumn,
    ListItem,
    SharePointList,
    Site,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment

__all__ = ["SharePointClient"]


class SharePointClient:
    """Sites, document libraries, and lists in SharePoint Online."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        default_site: str = "",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._default_site = default_site
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    # -- addressing ----------------------------------------------------------

    def _site(self, site: str = "") -> str:
        """Address a site by any of the four forms Graph accepts.

        ``""`` or ``"root"``   the tenant's default site
        ``"contoso.sharepoint.com"``           root site of the default collection
        ``"contoso.sharepoint.com:/teams/hr"`` addressed by server-relative path
        ``"contoso.sharepoint.com,{guid},{guid}"`` the full compound id

        The compound id contains two commas and a hostname contains dots, so
        none of these can be told apart by shape alone — which is why they are
        all simply passed through, with only the empty case defaulting. Graph
        itself does the disambiguation.

        Percent-encoding is deliberately *not* applied: the colon in the path
        form and the commas in the compound id are structural, and encoding
        them turns a valid address into a 404 that reads as a missing site.
        """
        target = site or self._default_site or "root"
        if target.startswith("/"):
            target = target.lstrip("/")
        return f"/sites/{target}"

    def _list(self, list_id: str, site: str = "") -> str:
        return f"{self._site(site)}/lists/{quote(list_id, safe='')}"

    def _library(self, site: str = "", drive_id: str = "") -> str:
        """The drive to work in: a named library, or the site's default one.

        ``/sites/{id}/drive`` is the site's *default* document library — the
        one called "Documents" in the UI. Most sites have others, so a workflow
        that means a specific library resolves it with ``list_drives`` and
        passes ``drive_id``; naming no library is a deliberate shorthand rather
        than a guess, and it is documented as such on every tool that takes it.
        """
        if drive_id:
            return f"/drives/{quote(drive_id, safe='')}"
        return f"{self._site(site)}/drive"

    # -- sites ---------------------------------------------------------------

    async def get_site(self, site: str = "") -> Site:
        return Site.from_api(await self._session.get(self._site(site)))

    async def search_sites(self, query: str, *, limit: int = 50) -> Results[Site]:
        """Search the tenant for sites matching free text.

        Not available to the ``Sites.Selected`` application permission — the
        reference says so explicitly, and under that permission this returns a
        403 rather than an empty list.
        """
        return await self._session.paginate(
            "/sites",
            limit=limit,
            params={"search": query},
            page_size=100,
            row=Site.from_api,
        )

    async def list_subsites(self, site: str = "", *, limit: int = 100) -> Results[Site]:
        return await self._session.paginate(
            f"{self._site(site)}/sites",
            limit=limit,
            page_size=100,
            row=Site.from_api,
        )

    # -- document libraries --------------------------------------------------

    async def list_drives(self, site: str = "", *, limit: int = 100) -> Results[Drive]:
        return await self._session.paginate(
            f"{self._site(site)}/drives",
            limit=limit,
            page_size=100,
            row=Drive.from_api,
        )

    async def list_drive_items(
        self,
        *,
        site: str = "",
        drive_id: str = "",
        item_id: str = "",
        path: str = "",
        limit: int = 200,
    ) -> Results[DriveItem]:
        return await self._session.paginate(
            item_address(
                self._library(site, drive_id),
                item_id=item_id,
                path=path,
                suffix="children",
            ),
            limit=limit,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def search_drive_items(
        self, query: str, *, site: str = "", drive_id: str = "", limit: int = 200
    ) -> Results[DriveItem]:
        return await self._session.paginate(
            item_address(
                self._library(site, drive_id),
                suffix=f"search(q='{_odata_string(query)}')",
            ),
            limit=limit,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def download_file(
        self, *, drive_id: str = "", site: str = "", item_id: str = "", path: str = ""
    ) -> Attachment:
        from loom.blobs.attachment import Attachment

        library = self._library(site, drive_id)
        meta = DriveItem.from_api(
            await self._session.get(
                item_address(library, item_id=item_id, path=path)
            )
        )
        if meta.is_folder:
            raise GraphPermanentError(
                f"'{meta.name}' is a folder and holds no bytes. "
                "Use list_drive_items to enumerate it.",
                status=0,
                code="notAFile",
            )
        # Address the content by the id just resolved rather than by the path
        # the caller gave: one form, always correct, and it also means a
        # renamed file downloads the bytes that were actually inspected.
        content = await self._session.download(
            item_address(library, item_id=meta.id, suffix="content")
        )
        return Attachment.from_bytes(
            meta.name, content, mime=meta.mime_type or _guess_type(meta.name)
        )

    async def upload_file(
        self,
        filename: str,
        content: bytes | str,
        *,
        drive_id: str = "",
        site: str = "",
        parent_id: str = "",
        parent_path: str = "",
        on_conflict: str = "replace",
    ) -> DriveItem:
        data = content.encode() if isinstance(content, str) else content
        raw = await self._session.send_bytes(
            "PUT",
            child_address(
                self._library(site, drive_id),
                filename,
                parent_id=parent_id,
                parent_path=parent_path,
                suffix="content",
            ),
            content=data,
            content_type=_guess_type(filename),
            params={"@microsoft.graph.conflictBehavior": on_conflict},
        )
        return DriveItem.from_api(raw or {})

    async def create_folder(
        self,
        folder_name: str,
        *,
        drive_id: str = "",
        site: str = "",
        parent_id: str = "",
        parent_path: str = "",
        on_conflict: str = "fail",
    ) -> DriveItem:
        raw = await self._session.post(
            item_address(
                self._library(site, drive_id),
                item_id=parent_id,
                path=parent_path,
                suffix="children",
            ),
            json={
                "name": folder_name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": on_conflict,
            },
        )
        return DriveItem.from_api(raw or {})

    async def delete_item(
        self, *, drive_id: str = "", site: str = "", item_id: str = "", path: str = ""
    ) -> bool:
        """Move a file or folder in a library to the site's recycle bin.

        Present because without it this toolset could add documents to a
        library and never remove one: the file lifecycle stopped halfway, and
        the only way round it was to hold the OneDrive grant as well — which
        is exactly the widening the split into two toolsets exists to avoid.
        """
        if not item_id and not path:
            raise GraphPermanentError(
                "delete_item needs item_id or path; with neither it would "
                "address the library root.",
                status=0,
                code="nothingToDelete",
            )
        await self._session.delete(
            item_address(
                self._library(site, drive_id), item_id=item_id, path=path
            )
        )
        return True

    async def create_share_link(
        self,
        *,
        drive_id: str = "",
        site: str = "",
        item_id: str = "",
        path: str = "",
        link_type: str = "view",
        scope: str = "organization",
        expires: str = "",
    ) -> SharingLink:
        body: dict[str, Any] = {"type": link_type, "scope": scope}
        if expires:
            body["expirationDateTime"] = expires
        raw = await self._session.post(
            item_address(
                self._library(site, drive_id),
                item_id=item_id,
                path=path,
                suffix="createLink",
            ),
            json=body,
        )
        return SharingLink.from_api(raw or {})

    # -- lists ---------------------------------------------------------------

    async def list_lists(
        self, site: str = "", *, limit: int = 100, include_hidden: bool = False
    ) -> Results[SharePointList]:
        found = await self._session.paginate(
            f"{self._site(site)}/lists",
            limit=limit,
            page_size=100,
            row=SharePointList.from_api,
        )
        if include_hidden:
            return found
        kept: Results[SharePointList] = Results(
            [entry for entry in found if not entry.hidden],
            complete=found.complete,
            total=found.total,
        )
        return kept

    async def get_list(self, list_id: str, site: str = "") -> SharePointList:
        return SharePointList.from_api(
            await self._session.get(self._list(list_id, site))
        )

    async def list_columns(
        self, list_id: str, site: str = "", *, limit: int = 100
    ) -> Results[ListColumn]:
        """List a list's columns, with internal *and* display names.

        The reason this operation exists as a first-class tool rather than a
        detail of the client: a ``fields`` bag is keyed by the internal name,
        and writing the display name is accepted and silently ignored.
        """
        return await self._session.paginate(
            f"{self._list(list_id, site)}/columns",
            limit=limit,
            page_size=100,
            row=ListColumn.from_api,
        )

    async def list_items(
        self,
        list_id: str,
        site: str = "",
        *,
        limit: int = 200,
        filter_query: str = "",
        order_by: str = "",
    ) -> Results[ListItem]:
        """List a list's items, always with their field values expanded.

        ``$expand=fields`` is sent unconditionally because Graph hides the bag
        by default, and an item without it is ids and timestamps and no data —
        a result that looks like an empty list rather than a missing parameter.
        """
        params: dict[str, Any] = {"$expand": "fields"}
        if filter_query:
            params["$filter"] = filter_query
        if order_by:
            params["$orderby"] = order_by
        return await self._session.paginate(
            f"{self._list(list_id, site)}/items",
            limit=limit,
            params=params,
            page_size=200,
            row=ListItem.from_api,
        )

    async def get_list_item(
        self, list_id: str, item_id: str, site: str = ""
    ) -> ListItem:
        raw = await self._session.get(
            f"{self._list(list_id, site)}/items/{quote(item_id, safe='')}",
            **{"$expand": "fields"},
        )
        return ListItem.from_api(raw or {})

    async def create_list_item(
        self, list_id: str, fields: dict[str, Any], site: str = ""
    ) -> ListItem:
        if not fields:
            raise GraphPermanentError(
                "create_list_item needs at least one field; an empty fields "
                "dict creates a blank row that reads as a successful write.",
                status=0,
                code="noFields",
            )
        raw = await self._session.post(
            f"{self._list(list_id, site)}/items", json={"fields": fields}
        )
        return ListItem.from_api(raw or {})

    async def update_list_item(
        self, list_id: str, item_id: str, fields: dict[str, Any], site: str = ""
    ) -> ListItem:
        """Update an item's field values.

        Graph patches the ``fields`` sub-resource rather than the item, and
        answers with the bare field set — so the item id is put back on the
        result here, since a caller that just updated something reasonably
        expects to be handed the thing it updated.
        """
        if not fields:
            raise GraphPermanentError(
                "update_list_item needs at least one field to change.",
                status=0,
                code="noFields",
            )
        raw = await self._session.patch(
            f"{self._list(list_id, site)}/items/{quote(item_id, safe='')}/fields",
            json=fields,
        )
        return ListItem.from_api({"id": item_id, "fields": raw or {}})

    async def delete_list_item(
        self, list_id: str, item_id: str, site: str = ""
    ) -> bool:
        await self._session.delete(
            f"{self._list(list_id, site)}/items/{quote(item_id, safe='')}"
        )
        return True


def _odata_string(value: str) -> str:
    """Escape a value for an OData string literal, then for a URL."""
    return quote(value.replace("'", "''"), safe="")


def _guess_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


