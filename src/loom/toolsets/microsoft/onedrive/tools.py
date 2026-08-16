"""OneDrive step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.onedrive.tools import onedrive_list_children

    files = await ctx.step(onedrive_list_children, path="Reports/2024")

**Every item is addressable two ways**, and each tool takes both: by ``item_id``
(what a previous call returned) or by ``path`` relative to the drive root
(``"Reports/2024/Q3.xlsx"``). Pass one. Passing neither means the drive root,
which is what makes ``onedrive_list_children()`` with no arguments do the
obvious thing.

**Which drive** is a deployment decision, not a workflow one. Under delegated
credentials these act on the signed-in person's OneDrive. Under an app-only
token there is no signed-in person, so set ``MS_ONEDRIVE_USER`` or
``MS_ONEDRIVE_DRIVE_ID`` — the tools refuse with that instruction rather than
letting Graph answer a confusing 400.

Retries are per operation. Reads retry; **uploading, sharing, and inviting do
not**, because Graph has no idempotency key for them and a retry after a
timeout uploads the file twice or sends a second invitation email.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loom import Retry, step
from loom.toolsets.microsoft.models import (
    DeltaPage,
    Drive,
    DriveItem,
    MicrosoftUser,
    Permission,
    SharingLink,
)
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    from loom.blobs.attachment import Attachment

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- identity and drive ------------------------------------------------------


@step(retry=_READ)
async def onedrive_whoami() -> MicrosoftUser:
    """Return the person these credentials authenticate as.

    Fails under an app-only token, which authenticates the application and has
    no signed-in user; the error says so and names the fix.

    Returns:
        The signed-in user's id, display name, email, and userPrincipalName.
        The last of these is what addresses their drive elsewhere.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().whoami()


@step(retry=_READ)
async def onedrive_get_drive() -> Drive:
    """Fetch the drive itself, including how much of its quota is used.

    Returns:
        Drive with id, name, type, owner, and quota. Check ``quota_state``
        before a bulk upload — ``"exceeded"`` turns every write into a 507.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().get_drive()


# -- browsing and searching --------------------------------------------------


@step(retry=_READ)
async def onedrive_list_children(
    item_id: str = "", path: str = "", limit: int = 200, order_by: str = ""
) -> Results[DriveItem]:
    """List the contents of a folder.

    Args:
        item_id: Folder id. Takes precedence over ``path``.
        path: Folder path relative to the drive root, e.g. ``"Reports/2024"``.
            Omit both arguments to list the root.
        limit: Maximum items to collect across pages. Defaults to 200.
        order_by: Graph ``$orderby`` clause, e.g. ``"lastModifiedDateTime desc"``
            or ``"name asc"``.

    Returns:
        Results of DriveItem. ``.complete`` is False when ``limit`` cut the
        listing short — check it before reporting a count.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().list_children(
        item_id, path, limit=limit, order_by=order_by
    )


@step(retry=_READ)
async def onedrive_get_item(item_id: str = "", path: str = "") -> DriveItem:
    """Fetch one file or folder's metadata.

    Args:
        item_id: Item id. Takes precedence over ``path``.
        path: Item path relative to the drive root, e.g. ``"Reports/Q3.xlsx"``.

    Returns:
        DriveItem with id, name, size, folder flag, web URL, and who last
        changed it.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().get_item(item_id, path)


@step(retry=_READ)
async def onedrive_search_items(
    query: str, item_id: str = "", path: str = "", limit: int = 200
) -> Results[DriveItem]:
    """Search for files and folders by name, metadata, or content.

    This is a content search, not a name glob — Graph matches "across several
    fields including filename, metadata, and file content".

    Args:
        query: Text to search for. Quotes are escaped for you.
        item_id: Restrict the search to this folder's subtree.
        path: Restrict the search to this folder path's subtree. Omit both to
            search the whole drive.
        limit: Maximum items to collect across pages. Defaults to 200.

    Returns:
        Results of DriveItem. Each carries ``drive_id``, because a search from
        the drive root can return items shared from other drives.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().search(
        query, item_id=item_id, path=path, limit=limit
    )


@step(retry=_READ)
async def onedrive_list_recent(limit: int = 50) -> Results[DriveItem]:
    """List files this account touched recently, newest first.

    Args:
        limit: Maximum items. Defaults to 50.

    Returns:
        Results of DriveItem, spanning drives — a recently-edited file in a
        SharePoint library appears here too, with its own ``drive_id``.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().list_recent(limit=limit)


@step(retry=_READ)
async def onedrive_list_shared_with_me(limit: int = 100) -> Results[DriveItem]:
    """List files and folders other people have shared with this user.

    Args:
        limit: Maximum items across pages. Defaults to 100.

    Returns:
        Results of DriveItem. Each lives in **another person's drive**, so use
        the ``drive_id`` on the item to address it — this toolset's own drive
        does not contain it.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().list_shared_with_me(limit=limit)


@step(retry=_READ)
async def onedrive_list_changes(
    delta_link: str = "", token: str = "", limit: int = 500
) -> DeltaPage:
    """List what changed in the drive, and return the link to ask again.

    Graph's own throttling guidance names repeated polling as a leading cause
    of being throttled and points at this as the alternative: ask once for a
    link, then ask that link what has changed since.

    Call it three ways:

    * no arguments — enumerate the drive's current state from scratch;
    * ``token="latest"`` — get an empty result plus a link that starts watching
      from now, without enumerating anything first;
    * ``delta_link=<stored link>`` — everything that changed since that link.

    Args:
        delta_link: A ``delta_link`` returned by a previous call.
        token: ``"latest"`` to start watching from now. Ignored when
            ``delta_link`` is given.
        limit: Maximum items to collect across pages. Defaults to 500.

    Returns:
        DeltaPage with ``items`` (a deletion appears as a normal entry carrying
        ``deleted=True``, not as an absence), ``delta_link`` to store for next
        time, and ``complete``.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().list_changes(
        delta_link=delta_link, token=token, limit=limit
    )


@step(retry=_READ)
async def onedrive_download_file(item_id: str = "", path: str = "") -> Attachment:
    """Download a file's bytes as a LOOM Attachment.

    Fails on a folder, which holds no bytes; the error says so.

    Args:
        item_id: File id. Takes precedence over ``path``.
        path: File path relative to the drive root.

    Returns:
        Attachment with filename, mime, size, and content. Pass it to
        ``ctx.put_artifact``, or ``att.offload(blobs)`` to keep the bytes out
        of the journal.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().download_file(item_id, path)


# -- writing -----------------------------------------------------------------


@step(retry=_UNSAFE_WRITE)
async def onedrive_upload_file(
    filename: str,
    content: bytes | str,
    parent_id: str = "",
    parent_path: str = "",
    on_conflict: str = "replace",
) -> DriveItem:
    """Upload a small file in one request.

    Not retried: Graph has no idempotency key for an upload, so a timeout after
    the bytes landed is indistinguishable from a failure and a retry can leave
    two copies when ``on_conflict`` is ``"rename"``.

    Refuses anything over 10 MiB, which is where Microsoft's guidance switches
    to a resumable session; the error names ``onedrive_upload_large_file``.

    Args:
        filename: Name to store it under, with an extension so the type is
            guessed correctly.
        content: Bytes, or text which is encoded as UTF-8.
        parent_id: Destination folder id.
        parent_path: Destination folder path. Omit both for the drive root.
        on_conflict: ``"replace"`` (default), ``"rename"``, or ``"fail"`` when
            something of that name is already there.

    Returns:
        The created DriveItem, including its id and web URL.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().upload_file(
        filename,
        content,
        parent_id=parent_id,
        parent_path=parent_path,
        on_conflict=on_conflict,
    )


@step(retry=_UNSAFE_WRITE)
async def onedrive_upload_large_file(
    filename: str,
    content: bytes,
    parent_id: str = "",
    parent_path: str = "",
    on_conflict: str = "replace",
) -> DriveItem:
    """Upload a large file through a resumable session, in 5 MiB fragments.

    Not retried by the step policy: the upload protocol resumes rather than
    restarts, and a blind step-level retry would throw away a mostly-finished
    transfer and start it again from zero.

    Args:
        filename: Name to store it under, with an extension.
        content: The file's bytes.
        parent_id: Destination folder id.
        parent_path: Destination folder path. Omit both for the drive root.
        on_conflict: ``"replace"`` (default), ``"rename"``, or ``"fail"``.

    Returns:
        The completed DriveItem, from the response to the final fragment.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().upload_large_file(
        filename,
        content,
        parent_id=parent_id,
        parent_path=parent_path,
        on_conflict=on_conflict,
    )


@step(retry=_UNSAFE_WRITE)
async def onedrive_create_folder(
    folder_name: str,
    parent_id: str = "",
    parent_path: str = "",
    on_conflict: str = "fail",
) -> DriveItem:
    """Create a folder.

    Not retried: a retry with ``on_conflict="rename"`` would leave two folders.
    The default is ``"fail"`` so a second attempt is an error rather than a
    silent duplicate.

    Args:
        folder_name: Name for the new folder.
        parent_id: Parent folder id.
        parent_path: Parent folder path. Omit both for the drive root.
        on_conflict: ``"fail"`` (default), ``"rename"``, or ``"replace"``.

    Returns:
        The created folder as a DriveItem.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().create_folder(
        folder_name,
        parent_id=parent_id,
        parent_path=parent_path,
        on_conflict=on_conflict,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def onedrive_move_item(
    item_id: str = "",
    path: str = "",
    parent_id: str = "",
    new_name: str = "",
) -> DriveItem:
    """Move a file or folder, rename it, or both.

    Retried once: moving an item that is already at the destination is the same
    request with the same outcome.

    Args:
        item_id: Item to move. Takes precedence over ``path``.
        path: Path of the item to move.
        parent_id: Destination folder id. Omit to rename in place.
        new_name: New name. Omit to keep the current one.

    Returns:
        The moved DriveItem at its new location.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().move_item(
        item_id, path, parent_id=parent_id, new_name=new_name
    )


@step(retry=_UNSAFE_WRITE)
async def onedrive_copy_item(
    item_id: str = "",
    path: str = "",
    parent_id: str = "",
    new_name: str = "",
) -> str:
    """Start copying a file or folder. Returns a URL to poll, not an item.

    Copying is asynchronous in Graph — a large folder takes longer than one
    request — so this answers with the monitor URL rather than inventing an id
    for a copy that does not exist yet.

    Not retried: a retry starts a second copy.

    Args:
        item_id: Item to copy. Takes precedence over ``path``.
        path: Path of the item to copy.
        parent_id: Destination folder id. Omit to copy alongside the original.
        new_name: Name for the copy. Omit to keep the original's.

    Returns:
        The monitor URL from Graph's ``Location`` header. GET it to see
        progress and, on completion, the new item's id.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().copy_item(
        item_id, path, parent_id=parent_id, new_name=new_name
    )


@step(retry=_IDEMPOTENT_WRITE)
async def onedrive_delete_item(item_id: str = "", path: str = "") -> bool:
    """Move a file or folder to the recycle bin.

    Retried once: deleting something already deleted is a 404, not a second
    deletion, so repeating the request cannot destroy more than was asked for.

    Args:
        item_id: Item to delete. Takes precedence over ``path``.
        path: Path of the item to delete.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().delete_item(item_id, path)


# -- sharing -----------------------------------------------------------------


@step(retry=_READ)
async def onedrive_list_permissions(
    item_id: str = "", path: str = "", limit: int = 100
) -> Results[Permission]:
    """List who can reach an item, and how.

    Args:
        item_id: Item id. Takes precedence over ``path``.
        path: Item path.
        limit: Maximum permissions. Defaults to 100.

    Returns:
        Results of Permission. ``inherited=True`` means the access comes from
        an ancestor folder and cannot be revoked on this item — it has to be
        revoked where it was granted.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().list_permissions(item_id, path, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def onedrive_create_share_link(
    item_id: str = "",
    path: str = "",
    link_type: str = "view",
    scope: str = "organization",
    expires: str = "",
    password: str = "",
    retain_inherited_permissions: bool = True,
) -> SharingLink:
    """Create a sharing link for an item.

    The default scope is ``"organization"`` rather than ``"anonymous"``
    deliberately: a link that works for anyone on the internet is not a safe
    thing to produce by omission. Ask for ``"anonymous"`` explicitly.

    Not retried, though Graph returns an existing link of the same type rather
    than making a second one — the retry is withheld because the password and
    expiry arguments make this a write with visible effects.

    Args:
        item_id: Item to share. Takes precedence over ``path``.
        path: Path of the item to share.
        link_type: ``"view"``, ``"edit"``, or ``"embed"`` (personal OneDrive
            only).
        scope: ``"organization"`` (default), ``"anonymous"``, or ``"users"``.
        expires: ISO-8601 expiry, e.g. ``"2026-01-31T00:00:00Z"``.
        password: Password for the link. OneDrive personal only.
        retain_inherited_permissions: False strips existing inherited
            permissions when sharing for the first time.

    Returns:
        SharingLink with the URL, type, scope, and roles granted.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().create_share_link(
        item_id,
        path,
        link_type=link_type,
        scope=scope,
        expires=expires,
        password=password,
        retain_inherited_permissions=retain_inherited_permissions,
    )


@step(retry=_UNSAFE_WRITE)
async def onedrive_invite(
    emails: list[str],
    item_id: str = "",
    path: str = "",
    message: str = "",
    can_edit: bool = False,
    require_sign_in: bool = True,
    send_invitation: bool = True,
    expires: str = "",
) -> list[Permission]:
    """Grant named people access to an item, optionally emailing them.

    Not retried: with ``send_invitation=True`` a retry after a timeout sends a
    second email to everyone on the list.

    Args:
        emails: Addresses to grant access to.
        item_id: Item to share. Takes precedence over ``path``.
        path: Path of the item to share.
        message: Note included in the invitation email.
        can_edit: True grants write access; False (default) grants read.
        require_sign_in: True (default) makes recipients authenticate.
        send_invitation: False grants access silently, sending no email.
        expires: ISO-8601 expiry for the granted access.

    Returns:
        The Permission granted to each recipient.
    """
    from loom.toolsets.microsoft.onedrive.client import get_default_client

    return await get_default_client().invite(
        emails,
        item_id,
        path,
        message=message,
        can_edit=can_edit,
        require_sign_in=require_sign_in,
        send_invitation=send_invitation,
        expires=expires,
    )
