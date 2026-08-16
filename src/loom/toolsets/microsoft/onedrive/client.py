"""OneDrive over Microsoft Graph — pure httpx, no vendor SDK.

The whole of the drive-addressing problem lives in :meth:`OneDriveClient._root`
and :meth:`OneDriveClient._addr`. Everything else is a thin call.
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
    graph_base_url,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.models import (
    DeltaPage,
    Drive,
    DriveItem,
    MicrosoftUser,
    Permission,
    SharingLink,
)
from loom.toolsets.microsoft.scope import user_root
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment

__all__ = [
    "CHUNK_SIZE",
    "SIMPLE_UPLOAD_MAX",
    "OneDriveClient",
    "get_default_client",
    "reset_default_client",
]

#: Above this, Microsoft's own guidance is to use a resumable upload session:
#: "Use resumable file transfers for files larger than 10 MiB". Configurable per
#: client, because it is a recommendation rather than a hard API limit.
SIMPLE_UPLOAD_MAX = 10 * 1024 * 1024

#: Fragment size for a resumable upload. Must be a multiple of 320 KiB — the
#: reference warns that "failing to use a fragment size that is a multiple of
#: 320 KiB can result in large file transfers failing after the last byte range
#: is uploaded", which is a failure that arrives only at the very end of a long
#: transfer. 320 KiB x 16 = 5 MiB, inside the recommended 5-10 MiB band by
#: construction rather than by arithmetic nobody re-checks.
CHUNK_SIZE = 320 * 1024 * 16


class OneDriveClient:
    """Files in a OneDrive, or in any drive addressable by id."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        user_id: str = "",
        drive_id: str = "",
        timeout: float = 60.0,
        simple_upload_max: int = SIMPLE_UPLOAD_MAX,
        chunk_size: int = CHUNK_SIZE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._user_id = user_id
        self._drive_id = drive_id
        self._simple_upload_max = simple_upload_max
        self._chunk_size = chunk_size
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    # -- addressing ----------------------------------------------------------

    def _root(self) -> str:
        """The drive this client works in.

        A named drive short-circuits the user entirely — a ``drive_id`` is
        addressable under an app-only token, which is why it is checked first.
        Otherwise this reduces to "whose drive", and the app-only refusal is
        the shared one in :mod:`loom.toolsets.microsoft.scope`.
        """
        if self._drive_id:
            return f"/drives/{quote(self._drive_id, safe='')}"
        return (
            user_root(
                self._auth,
                self._user_id,
                workload="a drive",
                env_hint="MS_ONEDRIVE_USER or MS_ONEDRIVE_DRIVE_ID",
                alternatives="pass drive_id= for a specific drive or SharePoint library",
            )
            + "/drive"
        )

    def _addr(self, item_id: str = "", path: str = "", suffix: str = "") -> str:
        """Address an item in this drive by id or by path."""
        return item_address(self._root(), item_id=item_id, path=path, suffix=suffix)

    # -- drive ---------------------------------------------------------------

    async def get_drive(self) -> Drive:
        return Drive.from_api(await self._session.get(self._root()))

    async def whoami(self) -> MicrosoftUser:
        # No user_id here on purpose: "who am I" has no answer for an
        # application, and returning some *other* user would answer a question
        # nobody asked.
        root = user_root(self._auth, workload="a signed-in user")
        return MicrosoftUser.from_api(await self._session.get(root))

    # -- reading -------------------------------------------------------------

    async def list_children(
        self,
        item_id: str = "",
        path: str = "",
        *,
        limit: int = 200,
        order_by: str = "",
    ) -> Results[DriveItem]:
        params: dict[str, Any] = {}
        if order_by:
            params["$orderby"] = order_by
        return await self._session.paginate(
            self._addr(item_id, path, "children"),
            limit=limit,
            params=params,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def get_item(self, item_id: str = "", path: str = "") -> DriveItem:
        return DriveItem.from_api(await self._session.get(self._addr(item_id, path)))

    async def search(
        self, query: str, *, item_id: str = "", path: str = "", limit: int = 200
    ) -> Results[DriveItem]:
        """Search a folder subtree, or the whole drive when no folder is named.

        Values match "across several fields including filename, metadata, and
        file content", so this is a content search, not a name glob.
        """
        return await self._session.paginate(
            self._addr(item_id, path, f"search(q='{_odata_string(query)}')"),
            limit=limit,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def list_recent(self, *, limit: int = 50) -> Results[DriveItem]:
        return await self._session.paginate(
            f"{self._root()}/recent",
            limit=limit,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def list_shared_with_me(self, *, limit: int = 100) -> Results[DriveItem]:
        """Files and folders other people have shared with this user.

        Deliberately reads ``/me/drive/sharedWithMe`` rather than the resolved
        drive root: sharing is a property of the *person*, not of a drive, and
        the endpoint does not exist under ``/drives/{id}``. Every item carries
        its own ``drive_id`` because they live in other people's drives — using
        this client's drive to address one afterwards would 404.
        """
        root = user_root(
            self._auth,
            self._user_id,
            workload="items shared with a user",
            env_hint="MS_ONEDRIVE_USER",
        )
        return await self._session.paginate(
            f"{root}/drive/sharedWithMe",
            limit=limit,
            page_size=200,
            row=DriveItem.from_api,
        )

    async def list_changes(
        self, *, delta_link: str = "", token: str = "", limit: int = 500
    ) -> DeltaPage:
        """Enumerate changes, returning the items and the next delta link.

        Three shapes, all documented on ``driveItem: delta``:

        * nothing passed — enumerate the drive's current state from scratch;
        * ``token="latest"`` — an **empty** page plus a link that starts
          watching from now, without enumerating anything first;
        * ``delta_link=`` — everything that changed since that link was issued.

        The link is the thing to store between runs. Deleted items come back as
        ordinary entries carrying ``deleted=True``, not as absences.
        """
        params: dict[str, Any] = {}
        path = delta_link or self._addr(suffix="delta")
        if token and not delta_link:
            params["token"] = token

        collected: list[DriveItem] = []
        cursor: str | None = path
        next_link = ""
        first = True
        # Not page_through: delta ends on @odata.deltaLink rather than by
        # omitting @odata.nextLink, and that terminal link is the return value
        # a caller needs, not a detail of the loop.
        while cursor and len(collected) < limit:
            body = await self._session.request(
                "GET", cursor, params=params if first else None
            )
            first = False
            for raw in (body or {}).get("value", []) or []:
                collected.append(DriveItem.from_api(raw))
            next_link = str((body or {}).get("@odata.deltaLink") or "")
            cursor = (body or {}).get("@odata.nextLink") or None

        return DeltaPage(
            items=collected[:limit],
            delta_link=next_link,
            complete=not cursor and len(collected) <= limit,
        )

    async def download_file(self, item_id: str = "", path: str = "") -> Attachment:
        item = await self.get_item(item_id, path)
        if item.is_folder:
            raise GraphPermanentError(
                f"'{item.name}' is a folder and holds no bytes. "
                "Use list_children to enumerate it.",
                status=0,
                code="notAFile",
            )
        from loom.blobs.attachment import Attachment

        content = await self._session.download(self._addr(item.id, "", "content"))
        return Attachment.from_bytes(
            item.name, content, mime=item.mime_type or _guess_type(item.name)
        )

    # -- permissions and sharing ---------------------------------------------

    async def list_permissions(
        self, item_id: str = "", path: str = "", *, limit: int = 100
    ) -> Results[Permission]:
        return await self._session.paginate(
            self._addr(item_id, path, "permissions"),
            limit=limit,
            page_size=100,
            row=Permission.from_api,
        )

    async def create_share_link(
        self,
        item_id: str = "",
        path: str = "",
        *,
        link_type: str = "view",
        scope: str = "organization",
        expires: str = "",
        password: str = "",
        retain_inherited_permissions: bool = True,
    ) -> SharingLink:
        body: dict[str, Any] = {"type": link_type, "scope": scope}
        if expires:
            body["expirationDateTime"] = expires
        if password:
            body["password"] = password
        if not retain_inherited_permissions:
            body["retainInheritedPermissions"] = False
        raw = await self._session.post(
            self._addr(item_id, path, "createLink"), json=body
        )
        return SharingLink.from_api(raw or {})

    async def invite(
        self,
        emails: list[str],
        item_id: str = "",
        path: str = "",
        *,
        message: str = "",
        can_edit: bool = False,
        require_sign_in: bool = True,
        send_invitation: bool = True,
        expires: str = "",
    ) -> list[Permission]:
        body: dict[str, Any] = {
            "recipients": [{"email": email} for email in emails],
            "roles": ["write" if can_edit else "read"],
            "requireSignIn": require_sign_in,
            "sendInvitation": send_invitation,
        }
        if message:
            body["message"] = message
        if expires:
            body["expirationDateTime"] = expires
        raw = await self._session.post(self._addr(item_id, path, "invite"), json=body)
        return [Permission.from_api(p) for p in (raw or {}).get("value", []) or []]

    # -- writing -------------------------------------------------------------

    async def create_folder(
        self,
        folder_name: str,
        *,
        parent_id: str = "",
        parent_path: str = "",
        on_conflict: str = "fail",
    ) -> DriveItem:
        body = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": on_conflict,
        }
        raw = await self._session.post(
            self._addr(parent_id, parent_path, "children"), json=body
        )
        return DriveItem.from_api(raw or {})

    async def upload_file(
        self,
        filename: str,
        content: bytes | str,
        *,
        parent_id: str = "",
        parent_path: str = "",
        on_conflict: str = "replace",
    ) -> DriveItem:
        """Upload in one request. Refuses anything the guidance says to stream."""
        data = content.encode() if isinstance(content, str) else content
        if len(data) > self._simple_upload_max:
            raise GraphPermanentError(
                f"{filename} is {len(data)} bytes; Microsoft's guidance is to "
                f"use a resumable upload session above {self._simple_upload_max}. "
                "Call upload_large_file instead — it resumes rather than "
                "restarting when a connection drops.",
                status=0,
                code="useUploadSession",
            )
        raw = await self._session.send_bytes(
            "PUT",
            child_address(
                self._root(),
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

    async def upload_large_file(
        self,
        filename: str,
        content: bytes,
        *,
        parent_id: str = "",
        parent_path: str = "",
        on_conflict: str = "replace",
    ) -> DriveItem:
        """Upload through a resumable session, in fragments.

        Fragments must go up sequentially and each must be a multiple of
        320 KiB except the last; both are guaranteed by construction here.
        """
        session = await self._session.post(
            child_address(
                self._root(),
                filename,
                parent_id=parent_id,
                parent_path=parent_path,
                suffix="createUploadSession",
            ),
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": on_conflict,
                    "name": filename,
                }
            },
        )
        upload_url = str((session or {}).get("uploadUrl") or "")
        if not upload_url:
            raise GraphPermanentError(
                f"createUploadSession returned no uploadUrl: {session}",
                status=0,
                code="noUploadUrl",
            )

        total = len(content)
        finished: Any = None
        for start in range(0, total, self._chunk_size):
            chunk = content[start : start + self._chunk_size]
            end = start + len(chunk) - 1
            status, body = await self._session.put_fragment(
                upload_url,
                content=chunk,
                content_range=f"bytes {start}-{end}/{total}",
            )
            # 202 means "send the next one"; 200/201 arrives with the finished
            # item on the final fragment.
            if status in (200, 201):
                finished = body
        if finished is None:
            raise GraphPermanentError(
                f"Upload of {filename} ended without a completed item.",
                status=0,
                code="uploadIncomplete",
            )
        return DriveItem.from_api(finished)

    async def delete_item(self, item_id: str = "", path: str = "") -> bool:
        await self._session.delete(self._addr(item_id, path))
        return True

    async def move_item(
        self,
        item_id: str = "",
        path: str = "",
        *,
        parent_id: str = "",
        new_name: str = "",
    ) -> DriveItem:
        body: dict[str, Any] = {}
        if parent_id:
            body["parentReference"] = {"id": parent_id}
        if new_name:
            body["name"] = new_name
        if not body:
            raise GraphPermanentError(
                "move_item needs parent_id (where to) or new_name (what to "
                "call it); with neither it would be a no-op that reads as a "
                "successful move.",
                status=0,
                code="nothingToDo",
            )
        return DriveItem.from_api(
            await self._session.patch(self._addr(item_id, path), json=body)
        )

    async def copy_item(
        self,
        item_id: str = "",
        path: str = "",
        *,
        parent_id: str = "",
        new_name: str = "",
    ) -> str:
        """Start a copy and return the monitor URL.

        Copying is asynchronous: Graph answers ``202 Accepted`` with a
        ``Location`` header and no item, because a large folder copy takes
        longer than a request. The monitor URL is the honest return value —
        pretending to hand back a copied item would mean inventing an id that
        does not exist yet.
        """
        body: dict[str, Any] = {}
        if parent_id:
            body["parentReference"] = {"id": parent_id}
        if new_name:
            body["name"] = new_name
        return await self._session.post_monitor(
            self._addr(item_id, path, "copy"), json=body
        )


def _odata_string(value: str) -> str:
    """Escape a value for an OData string literal, then for a URL.

    A single quote inside an OData literal is escaped by doubling it. Skipping
    that turns a filename with an apostrophe into a malformed query, which
    Graph rejects with a parse error that names neither the file nor the quote.
    """
    return quote(value.replace("'", "''"), safe="")


def _guess_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default: OneDriveClient | None = None


def get_default_client() -> OneDriveClient:
    """Return the process-wide client, building it on first use.

    Reads ``MS_ONEDRIVE_USER`` and ``MS_ONEDRIVE_DRIVE_ID`` so an app-only
    deployment can name its drive scope once in the environment rather than on
    every call.
    """
    global _default
    if _default is None:
        import os

        _default = OneDriveClient(
            base_url=graph_base_url(),
            user_id=os.environ.get("MS_ONEDRIVE_USER", ""),
            drive_id=os.environ.get("MS_ONEDRIVE_DRIVE_ID", ""),
        )
    return _default


def reset_default_client() -> None:
    """Drop the process-wide client. For tests, and for a credential rotation."""
    global _default
    _default = None
