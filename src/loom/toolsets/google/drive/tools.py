"""Google Drive steps, for use inside LOOM workflows.

    from loom.toolsets.google.drive.tools import drive_list_files

    reports = await ctx.step(
        drive_list_files, "name contains 'Q3'", 50
    )
    if not reports.complete:
        await ctx.report(f"showing {reports.summary()}")

Credentials come from the environment on first call — see
``loom.toolsets.google.auth``. Importing this module needs none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.google.drive.client import DriveClient
from loom.toolsets.google.drive.models import (
    DriveFile,
    DrivePermission,
    SharedDrive,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

__all__ = [
    "DRIVE_TOOL_DOCS",
    "drive_copy_file",
    "drive_create_folder",
    "drive_delete_file",
    "drive_download_file",
    "drive_export_file",
    "drive_find_folder",
    "drive_get_file",
    "drive_get_storage_quota",
    "drive_list_files",
    "drive_list_permissions",
    "drive_list_shared_drives",
    "drive_move_file",
    "drive_remove_permission",
    "drive_rename_file",
    "drive_restore_file",
    "drive_share_file",
    "drive_trash_file",
    "drive_update_file_content",
    "drive_upload_file",
]

#: Reads are safe to repeat. Google's 4xx errors raise ``NonRetryableError``
#: subclasses, so this stops on a malformed query rather than sleeping through
#: three attempts at it.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Metadata writes are re-appliable — renaming a file to the name it already
#: has is indistinguishable from renaming it once.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)

#: A permanent delete 404s on the second call, so a retry after a timeout
#: that actually succeeded turns a completed delete into a failed run.
#: Trashing does not have this problem — it is recoverable and repeatable.
_PERMANENT_DELETE = Retry(max_attempts=1)

#: Creating content is not idempotent and Drive offers no idempotency key: a
#: timeout after the file was stored is indistinguishable from a failure, and a
#: retry leaves two copies. One attempt, and a failure the workflow can decide
#: about. Journaling already prevents a *replay* from re-uploading.
_CREATE = Retry(max_attempts=1)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def drive_list_files(
    query: str = "",
    max_results: int = 100,
    folder_id: str = "",
    order_by: str = "modifiedTime desc",
    drive_id: str = "",
    include_trashed: bool = False,
) -> Results[DriveFile]:
    """Search Drive for files and folders.

    Args:
        query: Drive query syntax, e.g. ``"name contains 'invoice'"``,
            ``"mimeType = 'application/pdf'"``,
            ``"modifiedTime > '2026-01-01T00:00:00'"``. Combine with ``and``.
        max_results: Maximum files to return (default 100). Pages are followed
            until this is reached.
        folder_id: Restrict to the direct children of this folder. Resolve a
            folder *name* to an id with ``drive_find_folder`` first — matching
            on a name inside the query returns near-misses silently.
        order_by: Sort order, e.g. ``"modifiedTime desc"``, ``"name"``,
            ``"quotaBytesUsed desc"``.
        drive_id: Search one shared drive rather than everything visible.
        include_trashed: Include files in the bin (default False).

    Returns:
        Results[DriveFile] with id, name, mime_type, size, parents,
        web_view_link, modified_time, owners. Check ``.complete`` — a large
        Drive returns a page, and reporting it as a total is the bug this
        return type exists to prevent.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).list_files(
        query,
        max_results,
        order_by=order_by,
        folder_id=folder_id,
        drive_id=drive_id,
        include_trashed=include_trashed,
    )


@step(retry=_READ)
async def drive_get_file(file_id: str) -> DriveFile:
    """Fetch one file's metadata by id.

    Args:
        file_id: Drive file id.

    Returns:
        DriveFile. ``is_folder`` and ``is_google_doc`` say what kind of thing
        it is; the second decides download versus export.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).get_file(file_id)


@step(retry=_READ)
async def drive_find_folder(name: str, parent_id: str = "") -> DriveFile | None:
    """Resolve a folder name to a folder id — exact match only.

    Call this once at the top of a workflow rather than matching on the name in
    every query: a name is what a human wrote and an id is what Drive stores,
    and filtering on the former is how a query returns zero rows and no error.

    Args:
        name: Exact folder name, as it appears in Drive.
        parent_id: Only look inside this folder, for a name used twice.

    Returns:
        The DriveFile for the folder, or None if no folder has exactly that
        name. None means "not found" — create it, or report it; do not fall
        back to a fuzzy match.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).find_folder(name, parent_id)


@step(retry=_READ)
async def drive_download_file(file_id: str, filename: str = "") -> Attachment:
    """Download a file's bytes as a LOOM Attachment.

    Fails on a Google Doc, Sheet, or Slide — those hold no bytes. Use
    ``drive_export_file`` for those; the error says so.

    Args:
        file_id: Drive file id.
        filename: Override the name on the returned Attachment.

    Returns:
        Attachment with filename, mime, size, and the content. Pass it to
        ``ctx.put_artifact`` or ``att.offload(blobs)` to keep it out of the
        journal.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).download_file(file_id, filename)


@step(retry=_READ)
async def drive_export_file(
    file_id: str, export_mime: str = "", filename: str = ""
) -> Attachment:
    """Export a Google Doc, Sheet, or Slides into a real file format.

    Args:
        file_id: Drive file id of a Google-native document.
        export_mime: Target format. Defaults per source type — PDF for a Doc
            or Slides, XLSX for a Sheet. Others: ``"text/csv"``,
            ``"text/plain"``, ``"text/html"``,
            ``"application/vnd.openxmlformats-officedocument.wordprocessingml.document"``.
        filename: Override the name on the returned Attachment. The default
            appends the extension matching export_mime.

    Returns:
        Attachment holding the exported bytes.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("google_drive", DriveClient)
    return await client.export_file(file_id, export_mime, filename)


@step(retry=_READ)
async def drive_get_storage_quota() -> dict[str, int]:
    """Drive storage used and available, in bytes.

    Returns:
        Dict with ``limit``, ``usage``, ``usage_in_drive``. ``limit`` is 0 on
        an account with unlimited storage — it is absent from the API response
        rather than large.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).get_storage_quota()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@step(retry=_CREATE)
async def drive_upload_file(
    name: str,
    content: bytes | str,
    mime_type: str = "",
    folder_id: str = "",
    description: str = "",
) -> DriveFile:
    """Upload content as a new Drive file.

    Not retried: Drive has no idempotency key, so a timeout after the file was
    stored would leave two copies. A failure surfaces to the workflow instead.

    Args:
        name: Filename, including an extension so the type is guessed right.
        content: Bytes, or text which is encoded as UTF-8.
        mime_type: Content type. Guessed from the filename when omitted.
        folder_id: Folder to create it in. Root when omitted.
        description: Free-text description shown in the Drive UI.

    Returns:
        The created DriveFile, including its id and web_view_link.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).upload_file(
        name,
        content,
        mime_type=mime_type,
        folder_id=folder_id,
        description=description,
    )


@step(retry=_CREATE)
async def drive_update_file_content(
    file_id: str, content: bytes | str, mime_type: str = ""
) -> DriveFile:
    """Replace a file's content, keeping its id, links, and sharing.

    Args:
        file_id: Drive file id.
        content: Replacement bytes, or text encoded as UTF-8.
        mime_type: Content type of the new content.

    Returns:
        The updated DriveFile. Every existing link still resolves — which is
        the reason to update rather than upload a replacement.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).update_file_content(
        file_id, content, mime_type=mime_type
    )


@step(retry=_CREATE)
async def drive_create_folder(name: str, parent_id: str = "") -> DriveFile:
    """Create a folder.

    Drive permits two folders with the same name in one parent, so check with
    ``drive_find_folder`` first if the workflow is meant to be re-runnable.

    Args:
        name: Folder name.
        parent_id: Parent folder id. Root when omitted.

    Returns:
        The created DriveFile, with ``is_folder`` True.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).create_folder(name, parent_id)


@step(retry=_IDEMPOTENT_WRITE)
async def drive_rename_file(file_id: str, name: str) -> DriveFile:
    """Rename a file or folder.

    Args:
        file_id: Drive file id.
        name: New name.

    Returns:
        The updated DriveFile.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("google_drive", DriveClient)
    return await client.update_file(file_id, {"name": name})


@step(retry=_IDEMPOTENT_WRITE)
async def drive_move_file(
    file_id: str, folder_id: str, remove_from: str = ""
) -> DriveFile:
    """Move a file into a folder.

    Drive has no move operation — it adds a parent and removes the old one.
    This reads the current parents and removes them, so the result is a move
    and not a file that appears in two places.

    Args:
        file_id: Drive file id.
        folder_id: Destination folder id.
        remove_from: Remove only this parent, for a file deliberately in
            several folders.

    Returns:
        The updated DriveFile, with ``parents`` reflecting the move.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).move_file(
        file_id, folder_id, remove_from=remove_from
    )


@step(retry=_CREATE)
async def drive_copy_file(
    file_id: str, name: str = "", folder_id: str = ""
) -> DriveFile:
    """Copy a file.

    Args:
        file_id: Drive file id to copy.
        name: Name for the copy. Drive's default is "Copy of ...".
        folder_id: Folder to put the copy in. Same as the original when
            omitted.

    Returns:
        The new DriveFile.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).copy_file(file_id, name, folder_id)


@step(retry=_IDEMPOTENT_WRITE)
async def drive_trash_file(file_id: str) -> DriveFile:
    """Move a file to the bin. Recoverable for 30 days.

    Args:
        file_id: Drive file id.

    Returns:
        The updated DriveFile, with ``trashed`` True.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).trash_file(file_id)


@step(retry=_IDEMPOTENT_WRITE)
async def drive_restore_file(file_id: str) -> DriveFile:
    """Take a file back out of the bin.

    Args:
        file_id: Drive file id.

    Returns:
        The updated DriveFile, with ``trashed`` False.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).restore_file(file_id)


@step(retry=_PERMANENT_DELETE)
async def drive_delete_file(file_id: str) -> str:
    """Delete a file permanently, skipping the bin. Not recoverable.

    Worth a ``ctx.wait_for_approval()`` when an agent chose the id.

    Args:
        file_id: Drive file id.

    Returns:
        The deleted file id, so the journal records what was removed.
    """
    from loom.toolsets.factory import client_for

    await (await client_for("google_drive", DriveClient)).delete_file(file_id)
    return file_id


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


@step(retry=_IDEMPOTENT_WRITE)
async def drive_share_file(
    file_id: str,
    email: str = "",
    role: str = "reader",
    type: str = "user",
    domain: str = "",
    notify: bool = False,
    message: str = "",
) -> DrivePermission:
    """Grant someone access to a file or folder.

    Args:
        file_id: Drive file or folder id. Sharing a folder shares its contents.
        email: Who to share with, for type ``"user"`` or ``"group"``.
        role: ``"reader"`` (default), ``"commenter"``, ``"writer"``,
            ``"fileOrganizer"``, ``"organizer"``, or ``"owner"``.
        type: ``"user"`` (default), ``"group"``, ``"domain"``, or ``"anyone"``.
            ``"anyone"`` makes it public to anyone with the link.
        domain: The domain, for type ``"domain"``.
        notify: Whether Google emails the person (default False, so a bulk
            share does not send hundreds of emails).
        message: Note included in that email, when notify is True.

    Returns:
        The created DrivePermission, including its id — which is what
        ``drive_remove_permission`` takes.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).share_file(
        file_id,
        email=email,
        role=role,
        type=type,
        domain=domain,
        notify=notify,
        message=message,
    )


@step(retry=_READ)
async def drive_list_permissions(
    file_id: str, max_results: int = 100
) -> Results[DrivePermission]:
    """List who currently has access to a file.

    Args:
        file_id: Drive file id.
        max_results: Maximum permissions to return (default 100).

    Returns:
        Results[DrivePermission] with id, type, role, email_address.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("google_drive", DriveClient)
    return await client.list_permissions(file_id, max_results)


@step(retry=_IDEMPOTENT_WRITE)
async def drive_remove_permission(file_id: str, permission_id: str) -> str:
    """Revoke one person's access to a file.

    Args:
        file_id: Drive file id.
        permission_id: Permission id from ``drive_list_permissions``.

    Returns:
        The revoked permission id.
    """
    from loom.toolsets.factory import client_for

    await (await client_for("google_drive", DriveClient)).remove_permission(file_id, permission_id)
    return permission_id


@step(retry=_READ)
async def drive_list_shared_drives(max_results: int = 100) -> Results[SharedDrive]:
    """List the shared drives this account can see.

    Args:
        max_results: Maximum drives to return (default 100).

    Returns:
        Results[SharedDrive] with id, name, created_time. The id goes to
        ``drive_list_files(drive_id=...)``.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("google_drive", DriveClient)).list_shared_drives(max_results)


# ---------------------------------------------------------------------------
# Docs for the coding agent, derived from the models
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    def fields(model: type[BaseModel]) -> str:
        return ", ".join(model.model_json_schema().get("properties", {}))

    return f"""\
## Available Google Drive Tools

Import: from loom.toolsets.google.drive.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  GOOGLE_ACCESS_TOKEN, or
  GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

All tools return typed Pydantic models. Use attribute access:
file.name, file.web_view_link, file.is_folder.

### Reading

drive_list_files(query="", max_results=100, folder_id="", order_by=..., \
drive_id="", include_trashed=False) -> Results[DriveFile]
  Paged. Check .complete before calling the answer a total.
  DriveFile fields: {fields(DriveFile)}
    files = await ctx.step(drive_list_files, "mimeType = 'application/pdf'", 50)
    await ctx.report(f"found {{files.summary()}}")

drive_get_file(file_id) -> DriveFile

drive_find_folder(name, parent_id="") -> DriveFile | None
  Exact name match. Resolve a folder name ONCE, then pass folder_id.
  Returns None when nothing matches — do not fall back to a fuzzy query.

drive_download_file(file_id, filename="") -> Attachment
  Fails on Google Docs/Sheets/Slides — they store no bytes. Use export.

drive_export_file(file_id, export_mime="", filename="") -> Attachment
  Google-native docs only. Defaults: Doc/Slides -> PDF, Sheet -> XLSX.

drive_get_storage_quota() -> dict  (limit, usage, usage_in_drive; bytes)

### Writing

drive_upload_file(name, content, mime_type="", folder_id="", description="")
    -> DriveFile
  Not retried — Drive has no idempotency key and a retry duplicates the file.

drive_update_file_content(file_id, content, mime_type="") -> DriveFile
  Keeps the id, the links, and the sharing. Prefer this over re-uploading.

drive_create_folder(name, parent_id="") -> DriveFile
drive_rename_file(file_id, name) -> DriveFile
drive_move_file(file_id, folder_id, remove_from="") -> DriveFile
drive_copy_file(file_id, name="", folder_id="") -> DriveFile
drive_trash_file(file_id) -> DriveFile      (recoverable for 30 days)
drive_restore_file(file_id) -> DriveFile
drive_delete_file(file_id) -> str           (permanent, not recoverable)

### Sharing

drive_share_file(file_id, email="", role="reader", type="user", domain="",
                 notify=False, message="") -> DrivePermission
  DrivePermission fields: {fields(DrivePermission)}
  notify defaults to False — pass True to actually email the person.
  type="anyone" makes the file public to anyone with the link.

drive_list_permissions(file_id, max_results=100) -> Results[DrivePermission]
drive_remove_permission(file_id, permission_id) -> str
drive_list_shared_drives(max_results=100) -> Results[SharedDrive]
  SharedDrive fields: {fields(SharedDrive)}

### Notes

- A folder is a file with mime_type application/vnd.google-apps.folder; use
  file.is_folder rather than comparing that string.
- A Google Doc/Sheet/Slide has no bytes: file.is_google_doc is True and only
  export works. Downloading one raises with the export call in the message.
- Resolve a folder NAME to an id once with drive_find_folder, then pass
  folder_id. A "name contains" query silently matches near-misses.
- Deleting permanently and sharing with type="anyone" are the two worth a
  ctx.wait_for_approval() when an agent chose the arguments.
- Timestamps are RFC 3339. Build comparisons from ctx.now(), never
  datetime.now(): a workflow body must be deterministic across replays.
"""


DRIVE_TOOL_DOCS: str = _build_tool_docs()
