"""Toolset registration and entry-point discovery.

Provides a module-level catalog and convenience functions for registering
toolsets manually or via pip entry points (``loom_toolset`` group).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from workflow_builder.toolsets.catalog import ToolsetCatalog
from workflow_builder.toolsets.manifest import ToolsetManifest

if TYPE_CHECKING:
    pass

logger = logging.getLogger("workflow.toolsets")

# Module-level singleton catalog
_catalog = ToolsetCatalog()


def get_catalog() -> ToolsetCatalog:
    """Return the global toolset catalog."""
    return _catalog


def register_toolset(manifest: ToolsetManifest) -> None:
    """Register a toolset manifest with the global catalog."""
    _catalog.register(manifest)
    logger.info("Registered toolset: %s v%s", manifest.id, manifest.version)


def unregister_toolset(toolset_id: str) -> None:
    """Remove a toolset from the global catalog."""
    _catalog.unregister(toolset_id)


def discover_entry_points() -> int:
    """Auto-discover toolsets installed via pip entry points.

    Scans the ``loom_toolset`` entry point group.  Each entry point
    should resolve to a ``ToolsetManifest`` instance.

    Returns the number of toolsets discovered.
    """
    from importlib.metadata import entry_points

    count = 0
    for ep in entry_points(group="loom_toolset"):
        try:
            obj = ep.load()
            if isinstance(obj, ToolsetManifest):
                register_toolset(obj)
                count += 1
            else:
                logger.warning(
                    "Entry point '%s' did not resolve to a "
                    "ToolsetManifest (got %s)",
                    ep.name,
                    type(obj).__name__,
                )
        except Exception:
            logger.exception("Failed to load toolset entry point: %s", ep.name)
    return count
