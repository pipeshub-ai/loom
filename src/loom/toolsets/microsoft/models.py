"""Typed shapes shared by the OneDrive and SharePoint toolsets.

A SharePoint document library *is* a ``drive`` and its files *are*
``driveItem``s — the same resources OneDrive serves, from the same endpoints.
So the file models live here rather than in either product's package, and both
toolsets return identical types for identical things. A workflow that moves a
file out of OneDrive into a team library reads one shape throughout.

Flattened deliberately. Graph nests the interesting parts: whether an item is a
folder is the *presence* of a ``folder`` key, who last touched it is
``lastModifiedBy.user.displayName``, and where it lives is
``parentReference.path`` with a ``/drive/root:`` prefix still attached. Handing
that to a model to reason about spends tokens on structure nobody asked about.

Every model tolerates missing fields, because Graph varies what it returns by
endpoint: an item from ``delta`` omits ``cTag``, a deleted one omits ``name``,
and ``$select`` can narrow any of them to almost nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "DeltaPage",
    "Drive",
    "DriveItem",
    "MicrosoftUser",
    "Permission",
    "SharingLink",
]


def _person(raw: Any) -> str:
    """Pull a display name out of an ``identitySet``.

    Graph wraps every actor as ``{"user": {...}}``, ``{"application": {...}}``
    or ``{"device": {...}}``, and which key is present depends on what did the
    thing. A file uploaded by a daemon has no ``user`` at all, so falling back
    across the set is what stops a legitimate item from reporting an empty
    author.
    """
    if not isinstance(raw, dict):
        return ""
    for key in ("user", "application", "device"):
        entry = raw.get(key)
        if isinstance(entry, dict):
            name = entry.get("displayName") or entry.get("email") or ""
            if name:
                return str(name)
    return ""


def _folder_path(parent: Any) -> str:
    """Normalise ``parentReference.path`` to a plain folder path.

    Graph returns ``/drive/root:/Reports/2024`` — an addressing form with the
    colon escape still in it. What a workflow wants to print, log, or compare
    is ``/Reports/2024``.
    """
    if not isinstance(parent, dict):
        return ""
    path = str(parent.get("path") or "")
    if ":" in path:
        path = path.split(":", 1)[1]
    return path or "/"


class MicrosoftUser(BaseModel):
    """A person, as Microsoft Entra ID reports them."""

    id: str = ""
    display_name: str = ""
    email: str = ""
    user_principal_name: str = ""
    """The sign-in name. This is what ``/users/{id}/drive`` accepts, so it is
    the field to carry forward when addressing someone else's OneDrive."""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> MicrosoftUser:
        raw = raw or {}
        return cls(
            id=str(raw.get("id", "") or ""),
            display_name=raw.get("displayName") or "",
            email=raw.get("mail") or raw.get("userPrincipalName") or "",
            user_principal_name=raw.get("userPrincipalName") or "",
        )


class Drive(BaseModel):
    """A drive: someone's OneDrive, or a SharePoint document library."""

    id: str
    name: str = ""
    drive_type: str = ""
    """``personal``, ``business``, or ``documentLibrary``."""
    owner: str = ""
    web_url: str = ""
    quota_used: int = 0
    quota_total: int = 0
    quota_remaining: int = 0
    quota_state: str = ""
    """``normal``, ``nearing``, ``critical``, or ``exceeded``. Worth branching
    on before a bulk upload: ``exceeded`` turns every write into a 507."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Drive:
        quota = raw.get("quota") or {}
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            drive_type=raw.get("driveType") or "",
            owner=_person(raw.get("owner")),
            web_url=raw.get("webUrl") or "",
            quota_used=int(quota.get("used") or 0),
            quota_total=int(quota.get("total") or 0),
            quota_remaining=int(quota.get("remaining") or 0),
            quota_state=quota.get("state") or "",
        )


class DriveItem(BaseModel):
    """One file or folder, flattened to what a workflow reasons about."""

    id: str
    name: str = ""
    is_folder: bool = False
    """Derived from the presence of Graph's ``folder`` facet, which is how the
    API says it — there is no ``type`` field to read."""
    size: int = 0
    mime_type: str = ""
    web_url: str = ""
    folder_path: str = ""
    """The containing folder, colon-escape stripped: ``/Reports/2024``."""
    drive_id: str = ""
    """Which drive this item lives in. Needed to address it later when the item
    came from a search or a ``sharedWithMe`` listing, since those span drives."""
    child_count: int = 0
    created: str = ""
    modified: str = ""
    created_by: str = ""
    last_modified_by: str = ""
    etag: str = ""
    deleted: bool = False
    """Only ever true in a ``delta`` result. Graph reports a deletion as a
    normal entry carrying a ``deleted`` facet, not as an absence, so a caller
    distinguishes them by this flag rather than by diffing."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> DriveItem:
        folder = raw.get("folder")
        file = raw.get("file") or {}
        parent = raw.get("parentReference") or {}
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            is_folder=isinstance(folder, dict),
            size=int(raw.get("size") or 0),
            mime_type=file.get("mimeType") or "",
            web_url=raw.get("webUrl") or "",
            folder_path=_folder_path(parent),
            drive_id=str(parent.get("driveId") or ""),
            child_count=int((folder or {}).get("childCount") or 0)
            if isinstance(folder, dict)
            else 0,
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            created_by=_person(raw.get("createdBy")),
            last_modified_by=_person(raw.get("lastModifiedBy")),
            etag=raw.get("eTag") or "",
            deleted=raw.get("deleted") is not None,
        )


class DeltaPage(BaseModel):
    """Everything that changed, plus the link that asks again.

    A model rather than a dict because the link is the load-bearing half: a
    caller that reads ``items`` and drops ``delta_link`` has to re-enumerate the
    whole drive next time, which is exactly the polling that ``delta`` exists to
    replace. Naming it in the type makes that hard to overlook.
    """

    items: list[DriveItem] = Field(default_factory=list)
    delta_link: str = ""
    """Store this and pass it back next time. Empty only if the enumeration
    stopped early, in which case ``complete`` is False and there is more to
    read before a usable link is issued."""
    complete: bool = True
    """False when ``limit`` cut the enumeration short."""


class SharingLink(BaseModel):
    """A sharing link, as returned by ``createLink``."""

    id: str = ""
    url: str = ""
    type: str = ""
    """``view``, ``edit``, or ``embed``."""
    scope: str = ""
    """``anonymous``, ``organization``, or ``users``."""
    roles: list[str] = Field(default_factory=list)
    expires: str = ""
    has_password: bool = False
    web_html: str = ""
    """The ``<iframe>`` markup, for an ``embed`` link. OneDrive personal only."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SharingLink:
        link = raw.get("link") or {}
        return cls(
            id=str(raw.get("id", "")),
            url=link.get("webUrl") or "",
            type=link.get("type") or "",
            scope=link.get("scope") or "",
            roles=[str(r) for r in (raw.get("roles") or [])],
            expires=raw.get("expirationDateTime") or "",
            has_password=bool(raw.get("hasPassword", False)),
            web_html=link.get("webHtml") or "",
        )


class Permission(BaseModel):
    """Who can do what to an item."""

    id: str = ""
    roles: list[str] = Field(default_factory=list)
    """``read``, ``write``, ``owner``, or ``sp.full control``."""
    granted_to: str = ""
    granted_to_email: str = ""
    link_url: str = ""
    """Set when this permission *is* a sharing link rather than a person."""
    link_scope: str = ""
    inherited: bool = False
    """True when the permission comes from an ancestor folder. Revoking it on
    this item is not possible — it has to be revoked where it was granted."""
    expires: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Permission:
        link = raw.get("link") or {}
        # v1.0 sends a single grantee as `grantedToV2`/`grantedTo`, and multiple
        # ones as `grantedToIdentitiesV2`. Reading only the singular form
        # reports an empty grantee for every link shared with named people.
        grantee = raw.get("grantedToV2") or raw.get("grantedTo") or {}
        if not grantee:
            many = raw.get("grantedToIdentitiesV2") or raw.get("grantedToIdentities")
            if isinstance(many, list) and many:
                grantee = many[0]
        user = grantee.get("user") if isinstance(grantee, dict) else {}
        return cls(
            id=str(raw.get("id", "")),
            roles=[str(r) for r in (raw.get("roles") or [])],
            granted_to=_person(grantee),
            granted_to_email=(user or {}).get("email") or "",
            link_url=link.get("webUrl") or "",
            link_scope=link.get("scope") or "",
            inherited=bool(raw.get("inheritedFrom")),
            expires=raw.get("expirationDateTime") or "",
        )
