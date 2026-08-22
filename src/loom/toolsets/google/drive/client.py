"""Google Drive API v3 client.

Pure httpx. Three things about Drive are unlike the other Google APIs, and each
one is a silent wrong answer rather than an error if you get it wrong.

**Nothing comes back unless you ask for it.** A Drive read returns ``id``,
``name``, ``mimeType`` and nothing else unless the request carries a ``fields``
mask. Omitting it does not fail — it returns a file whose ``modifiedTime`` is
empty, so a workflow filtering on "changed since yesterday" matches none of
them. :data:`FILE_FIELDS` is that mask, and it is derived from the model.

**Shared drives are invisible by default.** ``files.list`` searches My Drive
only unless ``includeItemsFromAllDrives`` and ``supportsAllDrives`` are set, and
a team whose files all live on a shared drive gets an empty result and a 200.
Both flags are on for every call here.

**A Google Doc has no bytes.** Downloading one is a 403; it has to be exported
into a format you name. :meth:`DriveClient.download_file` says which method to
use rather than passing the raw API error through.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loom.toolsets.google.drive.models import (
    FOLDER_MIME,
    DriveFile,
    DrivePermission,
    SharedDrive,
)
from loom.toolsets.google.errors import GooglePermanentError
from loom.toolsets.google.http import (
    DEFAULT_TIMEOUT,
    DEFAULT_TRANSFER_TIMEOUT,
    GoogleSession,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

    from loom.blobs.attachment import Attachment
    from loom.toolsets.google.auth import GoogleAuth

__all__ = [
    "EXPORT_FORMATS",
    "FILE_FIELDS",
    "DriveClient",
    "flatten_file",
]

API_BASE = "https://www.googleapis.com/drive/v3"

#: Uploads go to a different host path than everything else. Posting file
#: content to the normal endpoint creates a *metadata-only* file — an empty
#: document with the right name, which is the failure that looks like success.
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]

#: Exactly the fields :func:`flatten_file` reads. Kept beside it so adding a
#: field to the model and forgetting to request it cannot happen quietly.
FILE_FIELDS = (
    "id,name,mimeType,size,parents,description,starred,trashed,shared,"
    "owners(emailAddress),driveId,webViewLink,webContentLink,md5Checksum,"
    "createdTime,modifiedTime"
)

_PERMISSION_FIELDS = (
    "id,type,role,emailAddress,domain,displayName,deleted,pendingOwner"
)

#: What a Google-native type can be exported as. The default is first: the
#: format that keeps the most structure and that a human can open.
EXPORT_FORMATS: dict[str, str] = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "image/png",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}

#: Filename suffix per export type, so a downloaded export is named something
#: an operating system will open.
_EXPORT_SUFFIX: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/html": ".html",
    "image/png": ".png",
    "application/rtf": ".rtf",
    "application/zip": ".zip",
}

#: Drive's own ceiling for ``files.list``. Asking for more is a 400.
_FILE_PAGE = 1000
#: ``permissions.list`` caps at 100, and exceeding it is also a 400.
_PERMISSION_PAGE = 100
_DRIVE_PAGE = 100

#: Every mutating call carries this: without it a write against a file on a
#: shared drive fails with ``teamDrivesSharingRestriction``-style errors that
#: name nothing a caller can act on.
_ALL_DRIVES = {"supportsAllDrives": "true"}


class DriveClient:
    """Async Google Drive client returning typed models."""

    def __init__(
        self,
        auth: GoogleAuth | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT,
    ) -> None:
        from loom.toolsets.google.auth import get_default_auth

        resolved = auth or get_default_auth(SCOPES)
        self._session = GoogleSession(
            resolved, API_BASE, transport=transport, timeout=timeout
        )
        # Downloads and exports move whole files, so they get the longer
        # budget — a 30-second API timeout would fail every large export and
        # look like a Drive problem rather than a client setting.
        self._transfers = GoogleSession(
            resolved, API_BASE, transport=transport, timeout=transfer_timeout
        )
        # A second session rather than a per-call base override: uploads are a
        # different host path, and threading that through every JSON helper
        # would put an unused argument on all of them.
        self._uploads = GoogleSession(
            resolved, UPLOAD_BASE, transport=transport, timeout=transfer_timeout
        )

    # -- reading -------------------------------------------------------------

    async def list_files(
        self,
        query: str = "",
        max_results: int = 100,
        *,
        order_by: str = "modifiedTime desc",
        folder_id: str = "",
        drive_id: str = "",
        include_trashed: bool = False,
    ) -> Results[DriveFile]:
        """Search Drive, following every page up to *max_results*.

        ``query`` is Drive query syntax — ``name contains 'report'``,
        ``mimeType = 'application/pdf'``, ``modifiedTime > '2026-01-01'``. A
        *fuzzy* match on a word a human supplied is exactly the resolution
        failure the manifest's ``resolves`` marking exists to prevent, so prefer
        :meth:`find_folder` and then filter by that folder's id.

        Trashed files are excluded by default. Drive does not do that on its own,
        so "list everything in this folder" otherwise includes files someone
        deleted, and a workflow that processes them re-processes the bin.
        """
        clauses = [query] if query else []
        if folder_id:
            clauses.append(f"'{_escape(folder_id)}' in parents")
        if not include_trashed:
            clauses.append("trashed = false")

        params: dict[str, Any] = {
            "q": " and ".join(f"({clause})" for clause in clauses) or None,
            "orderBy": order_by or None,
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            # Without a corpora naming the drive, a driveId is ignored rather
            # than honoured, and the search silently runs against My Drive.
            "corpora": "drive" if drive_id else "allDrives",
            "driveId": drive_id or None,
        }
        items = await self._session.paginate(
            "files",
            items_key="files",
            limit=max_results,
            params=params,
            size_param="pageSize",
            page_size=_FILE_PAGE,
        )
        return items.mapped(flatten_file)

    async def get_file(self, file_id: str) -> DriveFile:
        """Fetch one file's metadata."""
        data = await self._session.get(
            f"files/{_quote(file_id, 'file_id')}",
            fields=FILE_FIELDS,
            **_ALL_DRIVES,
        )
        return flatten_file(data)

    async def find_folder(self, name: str, parent_id: str = "") -> DriveFile | None:
        """The folder with exactly this name, or ``None``.

        An *exact* name match, deliberately: ``name contains`` would return the
        "Reports Archive" folder for a spec that said "Reports", and quietly
        writing to the wrong folder is worse than reporting that nothing matched.
        Returns ``None`` rather than raising, so a caller can create it.
        """
        clauses = [f"mimeType = '{FOLDER_MIME}'", f"name = '{_escape(name)}'"]
        if parent_id:
            clauses.append(f"'{_escape(parent_id)}' in parents")
        found = await self.list_files(" and ".join(clauses), max_results=2)
        return found[0] if found else None

    # -- content -------------------------------------------------------------

    async def download_file(self, file_id: str, filename: str = "") -> Attachment:
        """Download a file's bytes as a LOOM :class:`Attachment`.

        Refuses a Google-native doc up front and names the method that does work.
        The raw API answer is a 403 ``fileNotDownloadable``, which reads like a
        permissions problem and sends the reader to the wrong place entirely.
        """
        from loom.blobs.attachment import Attachment

        meta = await self.get_file(file_id)
        if meta.is_google_doc:
            raise GooglePermanentError(
                f"'{meta.name}' is a Google {meta.mime_type.rsplit('.', 1)[-1]} "
                "and stores no bytes to download. Use drive_export_file to "
                "convert it — e.g. export_mime='application/pdf'.",
                status=400,
                reason="fileNotDownloadable",
            )
        if meta.is_folder:
            raise GooglePermanentError(
                f"'{meta.name}' is a folder, not a file. List its contents with "
                "drive_list_files(folder_id=...).",
                status=400,
                reason="fileNotDownloadable",
            )

        content = await self._transfers.download(
            f"files/{_quote(file_id, 'file_id')}", alt="media", **_ALL_DRIVES
        )
        return Attachment.from_bytes(
            filename or meta.name or "download",
            content,
            mime=meta.mime_type or None,
            file_id=file_id,
        )

    async def export_file(
        self, file_id: str, export_mime: str = "", filename: str = ""
    ) -> Attachment:
        """Export a Google-native doc into a real file format.

        ``export_mime`` defaults per source type — PDF for a Doc or Slides,
        XLSX for a Sheet — because the sensible default differs by type and
        making the caller supply one turns every export into a lookup.
        """
        from loom.blobs.attachment import Attachment

        meta = await self.get_file(file_id)
        if not meta.is_google_doc:
            raise GooglePermanentError(
                f"'{meta.name}' is not a Google-native document, so there is "
                "nothing to export. Use drive_download_file for its bytes.",
                status=400,
                reason="cannotExportFile",
            )

        target = export_mime or EXPORT_FORMATS.get(meta.mime_type, "application/pdf")
        content = await self._transfers.download(
            f"files/{_quote(file_id, 'file_id')}/export", mimeType=target
        )
        name = filename or f"{meta.name or 'export'}{_EXPORT_SUFFIX.get(target, '')}"
        return Attachment.from_bytes(name, content, mime=target, file_id=file_id)

    async def upload_file(
        self,
        name: str,
        content: bytes | str,
        *,
        mime_type: str = "",
        folder_id: str = "",
        description: str = "",
    ) -> DriveFile:
        """Create a new file with content, in one multipart request.

        Multipart rather than resumable: a resumable upload is three round trips
        and only earns them past ~5MB, and a step that has to be re-driven after
        a crash re-uploads from the start either way, because the upload session
        id is not journaled.
        """
        payload = content.encode() if isinstance(content, str) else content
        metadata: dict[str, Any] = {"name": name}
        if folder_id:
            metadata["parents"] = [folder_id]
        if description:
            metadata["description"] = description
        if mime_type:
            metadata["mimeType"] = mime_type

        body, boundary = _multipart(metadata, payload, mime_type or _guess(name))
        data = await self._uploads.send_bytes(
            "POST",
            "files",
            content=body,
            content_type=f"multipart/related; boundary={boundary}",
            params={
                "uploadType": "multipart",
                "fields": FILE_FIELDS,
                **_ALL_DRIVES,
            },
        )
        return flatten_file(data)

    async def update_file_content(
        self, file_id: str, content: bytes | str, *, mime_type: str = ""
    ) -> DriveFile:
        """Replace a file's content, keeping its id, name, and sharing.

        A new revision of the same file — which is what "update the report"
        means to whoever holds the link. Uploading a replacement instead would
        break every existing link and drop every permission.
        """
        payload = content.encode() if isinstance(content, str) else content
        data = await self._uploads.send_bytes(
            "PATCH",
            f"files/{_quote(file_id, 'file_id')}",
            content=payload,
            content_type=mime_type or "application/octet-stream",
            params={"uploadType": "media", "fields": FILE_FIELDS, **_ALL_DRIVES},
        )
        return flatten_file(data)

    # -- writing -------------------------------------------------------------

    async def create_folder(self, name: str, parent_id: str = "") -> DriveFile:
        """Create a folder. Drive allows duplicate names — check first if that
        matters, with :meth:`find_folder`."""
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        data = await self._session.post(
            "files", body, fields=FILE_FIELDS, **_ALL_DRIVES
        )
        return flatten_file(data)

    async def update_file(
        self,
        file_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        add_parents: list[str] | None = None,
        remove_parents: list[str] | None = None,
    ) -> DriveFile:
        """Patch metadata — rename, re-describe, star, or re-parent.

        The argument is ``metadata`` and not ``fields`` because in Drive
        ``fields`` already means the response field mask, and one name for the
        thing you send and the thing you ask back is a guaranteed mix-up.
        """
        data = await self._session.patch(
            f"files/{_quote(file_id, 'file_id')}",
            metadata or {},
            fields=FILE_FIELDS,
            addParents=",".join(add_parents) if add_parents else None,
            removeParents=",".join(remove_parents) if remove_parents else None,
            **_ALL_DRIVES,
        )
        return flatten_file(data)

    async def move_file(
        self, file_id: str, folder_id: str, *, remove_from: str = ""
    ) -> DriveFile:
        """Move a file into a folder.

        Drive has no move: a move is adding a parent and removing the old one,
        and skipping the removal leaves the file in both places. When
        ``remove_from`` is not given the current parents are read and removed,
        so the default is the move a caller meant rather than a copy-by-linking.
        """
        old = [remove_from] if remove_from else (await self.get_file(file_id)).parents
        return await self.update_file(
            file_id,
            add_parents=[folder_id],
            remove_parents=[p for p in old if p != folder_id] or None,
        )

    async def copy_file(
        self, file_id: str, name: str = "", folder_id: str = ""
    ) -> DriveFile:
        """Copy a file. Google-native docs copy fine; only *downloading* them
        is special."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if folder_id:
            body["parents"] = [folder_id]
        data = await self._session.post(
            f"files/{_quote(file_id, 'file_id')}/copy",
            body,
            fields=FILE_FIELDS,
            **_ALL_DRIVES,
        )
        return flatten_file(data)

    async def trash_file(self, file_id: str) -> DriveFile:
        """Move a file to the bin. Recoverable for 30 days."""
        data = await self._session.patch(
            f"files/{_quote(file_id, 'file_id')}",
            {"trashed": True},
            fields=FILE_FIELDS,
            **_ALL_DRIVES,
        )
        return flatten_file(data)

    async def restore_file(self, file_id: str) -> DriveFile:
        """Take a file back out of the bin."""
        data = await self._session.patch(
            f"files/{_quote(file_id, 'file_id')}",
            {"trashed": False},
            fields=FILE_FIELDS,
            **_ALL_DRIVES,
        )
        return flatten_file(data)

    async def delete_file(self, file_id: str) -> None:
        """Delete permanently, skipping the bin. Not recoverable by anyone."""
        await self._session.delete(
            f"files/{_quote(file_id, 'file_id')}", **_ALL_DRIVES
        )

    # -- sharing -------------------------------------------------------------

    async def share_file(
        self,
        file_id: str,
        *,
        email: str = "",
        role: str = "reader",
        type: str = "user",
        domain: str = "",
        notify: bool = False,
        message: str = "",
    ) -> DrivePermission:
        """Grant access to a file or folder.

        ``notify`` defaults to ``False`` so a workflow sharing two hundred files
        does not send two hundred emails as a side effect of a default — the
        same rule as Calendar's ``send_updates``.

        ``type="anyone"`` makes the file public to anyone with the link. It
        needs no email and is worth a ``ctx.wait_for_approval()`` when an agent
        chose the arguments.
        """
        body: dict[str, Any] = {"role": role, "type": type}
        if email:
            body["emailAddress"] = email
        if domain:
            body["domain"] = domain

        data = await self._session.post(
            f"files/{_quote(file_id, 'file_id')}/permissions",
            body,
            fields=_PERMISSION_FIELDS,
            sendNotificationEmail="true" if notify else "false",
            emailMessage=message or None,
            **_ALL_DRIVES,
        )
        return _permission(data)

    async def list_permissions(
        self, file_id: str, max_results: int = 100
    ) -> Results[DrivePermission]:
        """Who currently has access, following every page."""
        items = await self._session.paginate(
            f"files/{_quote(file_id, 'file_id')}/permissions",
            items_key="permissions",
            limit=max_results,
            params={
                "fields": f"nextPageToken,permissions({_PERMISSION_FIELDS})",
                **_ALL_DRIVES,
            },
            size_param="pageSize",
            page_size=_PERMISSION_PAGE,
        )
        return items.mapped(_permission)

    async def remove_permission(self, file_id: str, permission_id: str) -> None:
        """Revoke one grant. The owner's permission cannot be removed."""
        await self._session.delete(
            f"files/{_quote(file_id, 'file_id')}/permissions/{permission_id}",
            **_ALL_DRIVES,
        )

    async def list_shared_drives(self, max_results: int = 100) -> Results[SharedDrive]:
        """Shared drives this account can see, following every page."""
        items = await self._session.paginate(
            "drives",
            items_key="drives",
            limit=max_results,
            params={"fields": "nextPageToken,drives(id,name,createdTime,hidden)"},
            size_param="pageSize",
            page_size=_DRIVE_PAGE,
        )
        return items.mapped(
            lambda item: SharedDrive(
                id=item.get("id", ""),
                name=item.get("name", ""),
                created_time=item.get("createdTime", ""),
                hidden=bool(item.get("hidden", False)),
            )
        )

    async def get_storage_quota(self) -> dict[str, int]:
        """Bytes used and available — the check before a bulk upload."""
        data = await self._session.get("about", fields="storageQuota")
        quota = (data or {}).get("storageQuota") or {}
        return {
            # Absent rather than zero on an unlimited account, which is why
            # every one of these is read with a default rather than indexed.
            "limit": int(quota.get("limit", 0) or 0),
            "usage": int(quota.get("usage", 0) or 0),
            "usage_in_drive": int(quota.get("usageInDrive", 0) or 0),
        }


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def flatten_file(raw: dict[str, Any]) -> DriveFile:
    """Flatten a Drive file resource into :class:`DriveFile`."""
    return DriveFile(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        mime_type=raw.get("mimeType", ""),
        # Drive sends size as a *string* of int64 — and only for files that
        # have bytes, so a folder has no key at all rather than a zero.
        size=int(raw.get("size", 0) or 0),
        parents=list(raw.get("parents") or []),
        description=raw.get("description", ""),
        starred=bool(raw.get("starred", False)),
        trashed=bool(raw.get("trashed", False)),
        shared=bool(raw.get("shared", False)),
        owners=[
            owner.get("emailAddress", "")
            for owner in raw.get("owners") or []
            if owner.get("emailAddress")
        ],
        drive_id=raw.get("driveId", ""),
        web_view_link=raw.get("webViewLink", ""),
        web_content_link=raw.get("webContentLink", ""),
        md5_checksum=raw.get("md5Checksum", ""),
        created_time=raw.get("createdTime", ""),
        modified_time=raw.get("modifiedTime", ""),
    )


def _permission(raw: dict[str, Any]) -> DrivePermission:
    return DrivePermission(
        id=raw.get("id", ""),
        type=raw.get("type", "user"),
        role=raw.get("role", "reader"),
        email_address=raw.get("emailAddress", ""),
        domain=raw.get("domain", ""),
        display_name=raw.get("displayName", ""),
        deleted=bool(raw.get("deleted", False)),
        pending_owner=bool(raw.get("pendingOwner", False)),
    )


def _multipart(
    metadata: dict[str, Any], content: bytes, content_type: str
) -> tuple[bytes, str]:
    """Build a ``multipart/related`` upload body: JSON metadata, then bytes.

    Hand-assembled rather than via ``email.mime``: that module encodes binary
    parts as base64 and rewrites line endings, both of which corrupt a file
    Drive stores verbatim. The boundary is fixed rather than random because a
    workflow step must produce the same request on replay — and it is checked
    against the payload below, so a collision cannot pass silently.
    """
    boundary = "loom-drive-boundary"
    while boundary.encode() in content:
        # A file that literally contains the boundary would end the part early
        # and truncate itself. Vanishingly rare, catastrophic, and cheap to rule
        # out — and still deterministic, since it derives from the content.
        boundary += "-x"

    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, boundary


def _guess(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _escape(value: str) -> str:
    """Escape a value going into a Drive query string.

    A folder name with an apostrophe — "Ada's reports" — otherwise closes the
    quote and produces a malformed query, which Drive answers with a 400 naming
    a character position in a string the caller never wrote.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _quote(file_id: str, argument: str) -> str:
    """Validate and percent-encode a path segment.

    Checks the type first for the same reason Calendar does: ``quote(None)``
    raises ``quote_from_bytes() expected bytes``, which names neither the
    argument nor the caller's mistake.
    """
    if not isinstance(file_id, str) or not file_id:
        raise ValueError(f"{argument} must be a non-empty string, got {file_id!r}")

    from urllib.parse import quote

    return quote(file_id, safe="")


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


