"""Typed Pydantic response models for the Confluence toolset.

Single source of truth for all Confluence response shapes.
The client, tools, manifest, and auto-generated docs all reference
these models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ConfluencePage(BaseModel):
    """A Confluence page or blogpost."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str = ""
    status: str = ""
    space_id: str = ""
    space_key: str = ""
    parent_id: str = ""
    author_id: str = ""
    version: int = 1
    created_at: str = ""
    url: str = ""


class PageBody(BaseModel):
    """The rendered body content of a page."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str = ""
    body: str = ""
    representation: str = "storage"


class CreatedPage(BaseModel):
    """Response from creating or updating a page."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str = ""
    version: int = 1
    url: str = ""


class ConfluenceComment(BaseModel):
    """A comment on a page."""

    model_config = ConfigDict(frozen=True)

    id: str
    body: str = ""
    author_id: str = ""
    created_at: str = ""
    page_id: str = ""


class ConfluenceSpace(BaseModel):
    """A Confluence space."""

    model_config = ConfigDict(frozen=True)

    id: str
    key: str
    name: str = ""
    type: str = ""
    status: str = ""
    description: str = ""


class ConfluenceUser(BaseModel):
    """Authenticated Confluence user profile."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    email: str = ""


class SearchResult(BaseModel):
    """A single CQL search result."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    title: str = ""
    type: str = ""
    space_key: str = ""
    excerpt: str = ""
    url: str = ""
    last_modified: str = ""


__all__ = [
    "ConfluenceComment",
    "ConfluencePage",
    "ConfluenceSpace",
    "ConfluenceUser",
    "CreatedPage",
    "PageBody",
    "SearchResult",
]
