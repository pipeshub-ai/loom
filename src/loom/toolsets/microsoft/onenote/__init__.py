"""OneNote toolset — notebooks, sections, and pages."""

from __future__ import annotations

from loom.toolsets.microsoft.onenote.client import (
    OneNoteClient,
    get_default_client,
    reset_default_client,
)

__all__ = ["OneNoteClient", "get_default_client", "reset_default_client"]
