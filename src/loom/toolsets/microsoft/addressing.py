"""How Graph addresses an item inside a drive.

One function, because the rule is small, exact, and wrong in a way that is hard
to see: Graph escapes a relative path with a colon —
``/root:/Reports/Q3.xlsx`` — and needs a **second** colon when anything follows
the path, as in ``/root:/Reports:/children``. An id-addressed item takes no
colons at all.

Both toolsets need it — a OneDrive folder and a SharePoint document library are
the same ``drive`` resource — and the failure when it is wrong is a 400 that
reads like a bad path rather than a bad URL. So it is written once here rather
than twice, slightly differently, in two clients.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["child_address", "item_address"]


def item_address(
    root: str, *, item_id: str = "", path: str = "", suffix: str = ""
) -> str:
    """Build the Graph address of an item, or of something hanging off it.

    Args:
        root: The drive, already addressed — ``/me/drive``, ``/drives/{id}``,
            ``/sites/{id}/drive``, or ``/users/{id}/drive``.
        item_id: Item id. Takes precedence over ``path`` when both are given,
            because an id is unambiguous and a path is not.
        path: Path relative to the drive root. Empty means the root itself.
        suffix: What hangs off the item — ``children``, ``content``,
            ``createLink``, ``permissions``, ``delta``.

    Returns:
        A path to join to the Graph base URL.

    Examples:
        >>> item_address("/me/drive", suffix="children")
        '/me/drive/root/children'
        >>> item_address("/me/drive", path="Reports/2024", suffix="children")
        '/me/drive/root:/Reports/2024:/children'
        >>> item_address("/me/drive", path="Reports/Q3 2024.xlsx")
        '/me/drive/root:/Reports/Q3%202024.xlsx'
        >>> item_address("/me/drive", item_id="01ABC", suffix="content")
        '/me/drive/items/01ABC/content'
    """
    root = root.rstrip("/")
    if item_id:
        base = f"{root}/items/{quote(item_id, safe='')}"
        return f"{base}/{suffix}" if suffix else base

    trimmed = (path or "").strip("/")
    if not trimmed:
        base = f"{root}/root"
        return f"{base}/{suffix}" if suffix else base

    # safe="/" keeps the path separators readable while escaping spaces and
    # everything else; the colons around the path are structural and are the
    # reason this cannot be one quote() call at the call site.
    base = f"{root}/root:/{quote(trimmed, safe='/')}"
    return f"{base}:/{suffix}" if suffix else base


def child_address(
    root: str,
    child_name: str,
    *,
    parent_id: str = "",
    parent_path: str = "",
    suffix: str = "",
) -> str:
    """Address a **new** child of a folder — the form an upload writes to.

    Separate from :func:`item_address` because Graph spells the two differently
    and the difference is one colon. Naming an existing item under a parent id
    is ``/items/{id}/content``; creating one *there* is
    ``/items/{parent}:/{name}:/content`` — the colon after the parent id is what
    turns "this item" into "a path relative to this item". Getting it wrong
    uploads to the parent folder's own content stream, which for a folder is a
    400 that mentions neither the parent nor the name.

    Args:
        root: The drive, already addressed.
        child_name: Name of the file to create.
        parent_id: Destination folder id.
        parent_path: Destination folder path. Omit both for the drive root.
        suffix: ``content`` or ``createUploadSession``.

    Returns:
        A path to join to the Graph base URL.

    Examples:
        >>> child_address("/me/drive", "a.txt", suffix="content")
        '/me/drive/root:/a.txt:/content'
        >>> child_address("/me/drive", "a.txt", parent_path="Reports", suffix="content")
        '/me/drive/root:/Reports/a.txt:/content'
        >>> child_address("/me/drive", "a.txt", parent_id="01ABC", suffix="content")
        '/me/drive/items/01ABC:/a.txt:/content'
    """
    root = root.rstrip("/")
    name = quote(child_name, safe="")
    if parent_id:
        base = f"{root}/items/{quote(parent_id, safe='')}:/{name}"
    else:
        trimmed = (parent_path or "").strip("/")
        folder = f"{quote(trimmed, safe='/')}/" if trimmed else ""
        base = f"{root}/root:/{folder}{name}"
    return f"{base}:/{suffix}" if suffix else base
