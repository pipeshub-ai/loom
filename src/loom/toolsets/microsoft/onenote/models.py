"""Typed shapes for OneNote.

The models cover a page's *metadata* only. A page's **content is HTML**, fetched
from a separate endpoint and returned as a string rather than a model — see
``OneNoteClient.get_page_content``. Modelling HTML as a field would suggest it
round-trips like the rest of the payload, and it does not: it is a document.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = ["Notebook", "OneNotePage", "OneNoteSection", "SectionGroup"]


def _links(raw: dict[str, Any]) -> tuple[str, str]:
    """Pull the two "open this" URLs out of OneNote's nested links object."""
    links = raw.get("links") or {}
    client = (links.get("oneNoteClientUrl") or {}).get("href") or ""
    web = (links.get("oneNoteWebUrl") or {}).get("href") or ""
    return str(client), str(web)


class Notebook(BaseModel):
    """A OneNote notebook."""

    id: str
    display_name: str = ""
    created: str = ""
    modified: str = ""
    is_default: bool = False
    """The notebook OneNote opens by default for this user."""
    is_shared: bool = False
    web_url: str = ""
    """Opens the notebook in OneNote on the web."""
    client_url: str = ""
    """Opens it in the installed OneNote client, if there is one."""
    sections_url: str = ""
    """Graph's own link to this notebook's sections. Kept because OneNote
    returns absolute URLs for its relationships and following one is cheaper
    than reconstructing the path."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Notebook:
        client_url, web_url = _links(raw)
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            is_default=bool(raw.get("isDefault", False)),
            is_shared=bool(raw.get("isShared", False)),
            web_url=web_url,
            client_url=client_url,
            sections_url=str(raw.get("sectionsUrl") or ""),
        )


class OneNoteSection(BaseModel):
    """A section inside a notebook."""

    id: str
    display_name: str = ""
    created: str = ""
    modified: str = ""
    is_default: bool = False
    web_url: str = ""
    client_url: str = ""
    notebook_id: str = ""
    notebook_name: str = ""
    """Carried because a section listed from ``/onenote/sections`` — every
    section the user can reach — is ambiguous without knowing which notebook it
    came from."""
    pages_url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> OneNoteSection:
        client_url, web_url = _links(raw)
        notebook = raw.get("parentNotebook") or {}
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            is_default=bool(raw.get("isDefault", False)),
            web_url=web_url,
            client_url=client_url,
            notebook_id=str(notebook.get("id", "") or ""),
            notebook_name=notebook.get("displayName") or "",
            pages_url=str(raw.get("pagesUrl") or ""),
        )


class SectionGroup(BaseModel):
    """A folder of sections. Most tenants never create one."""

    id: str
    display_name: str = ""
    created: str = ""
    modified: str = ""
    sections_url: str = ""
    section_groups_url: str = ""
    """Section groups nest, so this is how a caller walks further down."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SectionGroup:
        return cls(
            id=str(raw.get("id", "")),
            display_name=raw.get("displayName") or "",
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            sections_url=str(raw.get("sectionsUrl") or ""),
            section_groups_url=str(raw.get("sectionGroupsUrl") or ""),
        )


class OneNotePage(BaseModel):
    """A page's metadata. The content is HTML and is fetched separately."""

    id: str
    title: str = ""
    """Set from the HTML document's ``<title>`` when the page was created —
    there is no title field to write directly."""
    created: str = ""
    modified: str = ""
    level: int = 0
    """Indentation within the section; 0 is top level."""
    order: int = 0
    web_url: str = ""
    client_url: str = ""
    content_url: str = ""
    """Where the HTML lives. ``get_page_content`` uses the page id rather than
    this, but it is carried because it is what OneNote itself hands back."""
    section_id: str = ""
    section_name: str = ""
    notebook_id: str = ""
    notebook_name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> OneNotePage:
        client_url, web_url = _links(raw)
        section = raw.get("parentSection") or {}
        notebook = raw.get("parentNotebook") or {}
        return cls(
            id=str(raw.get("id", "")),
            title=raw.get("title") or "",
            created=raw.get("createdDateTime") or "",
            modified=raw.get("lastModifiedDateTime") or "",
            level=int(raw.get("level") or 0),
            order=int(raw.get("order") or 0),
            web_url=web_url,
            client_url=client_url,
            content_url=str(raw.get("contentUrl") or ""),
            section_id=str(section.get("id", "") or ""),
            section_name=section.get("displayName") or "",
            notebook_id=str(notebook.get("id", "") or ""),
            notebook_name=notebook.get("displayName") or "",
        )
