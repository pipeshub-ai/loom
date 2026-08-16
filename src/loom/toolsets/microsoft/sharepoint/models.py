"""Typed shapes specific to SharePoint Online.

Files and drives are **not** here — a SharePoint document library is a ``drive``
and its files are ``driveItem``s, so those models live in
``loom.toolsets.microsoft.models`` and are shared with OneDrive. What is left is
what SharePoint alone has: sites, lists, columns, and list items.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.toolsets.microsoft.models import _person

__all__ = ["ListColumn", "ListItem", "SharePointList", "Site"]

#: Field keys Graph returns inside every ``fields`` bag that are plumbing
#: rather than data. Kept out of ``ListItem.fields`` so a model reading an
#: item's columns is not handed six OData annotations to reason about first.
_FIELD_NOISE = frozenset({"@odata.etag", "@odata.id", "@odata.context"})

#: The type facets Graph may attach to a column, in the order they are checked.
#: A column's type is which of these keys is *present* — there is no ``type``
#: field to read. Module-level rather than a class attribute because Pydantic
#: turns an underscore-prefixed class attribute into a private attribute
#: descriptor, which is not a tuple by the time a classmethod reads it.
_COLUMN_TYPES = (
    "text",
    "number",
    "dateTime",
    "boolean",
    "choice",
    "currency",
    "lookup",
    "personOrGroup",
    "hyperlinkOrPicture",
    "calculated",
    "contentApprovalStatus",
    "geolocation",
    "term",
    "thumbnail",
)


class Site(BaseModel):
    """A SharePoint site (an ``SPWeb``, in the older vocabulary)."""

    id: str
    """The compound id — ``{hostname},{spsite-guid},{spweb-guid}``. Two commas
    inside one string, which is why it is never something anyone types from
    memory: pass it back verbatim from a search or a get."""
    name: str = ""
    display_name: str = ""
    description: str = ""
    web_url: str = ""
    hostname: str = ""
    site_path: str = ""
    """The server-relative path, ``/teams/hr``. Together with ``hostname`` this
    is the human-readable address — ``sharepoint_get_site`` accepts it, so a
    workflow can be written against the URL someone pasted."""
    created: str = ""
    modified: str = ""
    is_site_collection_root: bool = False
    """True when this site is the root of its collection. Graph marks it with a
    ``siteCollection`` facet rather than a boolean."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Site:
        collection = raw.get("siteCollection") or {}
        web_url = raw.get("webUrl") or ""
        hostname = collection.get("hostname") or ""
        path = ""
        if web_url and hostname and hostname in web_url:
            path = web_url.split(hostname, 1)[1]
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            display_name=raw.get("displayName") or raw.get("name") or "",
            description=raw.get("description") or "",
            web_url=web_url,
            hostname=hostname,
            site_path=path,
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            is_site_collection_root=bool(collection),
        )


class SharePointList(BaseModel):
    """A list, or a document library — SharePoint models both as a ``list``."""

    id: str
    name: str = ""
    display_name: str = ""
    description: str = ""
    web_url: str = ""
    template: str = ""
    """``genericList``, ``documentLibrary``, ``events``, ``tasks``… Worth
    branching on: a ``documentLibrary`` is also reachable as a drive, where the
    file operations are far better than the list-item ones."""
    hidden: bool = False
    created: str = ""
    modified: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SharePointList:
        info = raw.get("list") or {}
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            display_name=raw.get("displayName") or raw.get("name") or "",
            description=raw.get("description") or "",
            web_url=raw.get("webUrl") or "",
            template=info.get("template") or "",
            hidden=bool(info.get("hidden", False)),
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
        )


class ListColumn(BaseModel):
    """One column of a list, with both of its names.

    **Both names, always, and that is the point of this model.** A column shown
    as "Due Date" is keyed internally as ``DueDate`` or ``Due_x0020_Date``
    depending on how it was created, and a list item's ``fields`` bag is keyed
    by the *internal* name. Writing the display name into a field set is not an
    error — SharePoint accepts the request and simply does not set the column,
    so the item is created, the workflow reports success, and the value is
    missing. Resolving here is what prevents that.
    """

    name: str = ""
    """The internal name. This is the key to use in a ``fields`` dict."""
    display_name: str = ""
    """What the site shows. This is what a human's spec will call it."""
    description: str = ""
    type: str = ""
    """``text``, ``number``, ``dateTime``, ``choice``, ``lookup``, ``person``…
    Derived from whichever type facet Graph attached."""
    required: bool = False
    read_only: bool = False
    """A read-only column silently ignores writes, so check before setting it."""
    hidden: bool = False
    choices: list[str] = Field(default_factory=list)
    """For a ``choice`` column, the accepted values — another vocabulary that
    has to be matched exactly rather than guessed at."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ListColumn:
        kind = ""
        choices: list[str] = []
        for candidate in _COLUMN_TYPES:
            facet = raw.get(candidate)
            if isinstance(facet, dict):
                kind = candidate
                if candidate == "choice":
                    choices = [str(c) for c in (facet.get("choices") or [])]
                break
        return cls(
            name=raw.get("name") or "",
            display_name=raw.get("displayName") or raw.get("name") or "",
            description=raw.get("description") or "",
            type=kind,
            required=bool(raw.get("required", False)),
            read_only=bool(raw.get("readOnly", False)),
            hidden=bool(raw.get("hidden", False)),
            choices=choices,
        )


class ListItem(BaseModel):
    """One row of a list, with its column values flattened to ``fields``."""

    id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    """Column values, keyed by **internal** column name. Empty when the caller
    forgot ``$expand=fields`` — which the client never does, because Graph
    hides the bag by default and an item without it is ids and timestamps and
    no data at all."""
    web_url: str = ""
    content_type: str = ""
    created: str = ""
    modified: str = ""
    created_by: str = ""
    last_modified_by: str = ""
    etag: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ListItem:
        bag = raw.get("fields")
        fields = (
            {k: v for k, v in bag.items() if k not in _FIELD_NOISE}
            if isinstance(bag, dict)
            else {}
        )
        return cls(
            id=str(raw.get("id", "")),
            fields=fields,
            web_url=raw.get("webUrl") or "",
            content_type=(raw.get("contentType") or {}).get("name") or "",
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            created_by=_person(raw.get("createdBy")),
            last_modified_by=_person(raw.get("lastModifiedBy")),
            etag=raw.get("eTag") or "",
        )
