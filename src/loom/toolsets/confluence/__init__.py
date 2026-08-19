"""Confluence toolset for loomsdk.

Lazy-loaded: importing this package does not require Confluence credentials.
The ConfluenceClient reads CONFLUENCE_URL, CONFLUENCE_EMAIL,
CONFLUENCE_API_TOKEN from the environment only when a tool is first called.
"""

from __future__ import annotations

from loom.toolsets.confluence.manifest import (
    CONFLUENCE_MANIFEST,
)
from loom.toolsets.confluence.models import (
    ConfluenceComment,
    ConfluencePage,
    ConfluenceSpace,
    ConfluenceUser,
    CreatedPage,
    PageBody,
    SearchResult,
)

__all__ = [
    "CONFLUENCE_MANIFEST",
    "ConfluenceComment",
    "ConfluencePage",
    "ConfluenceSpace",
    "ConfluenceUser",
    "CreatedPage",
    "PageBody",
    "SearchResult",
]
