"""Toolset registration and entry-point discovery.

Provides a module-level catalog and convenience functions for registering
toolsets manually or via pip entry points (``loom_toolset`` group).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from workflow_builder.toolsets.manifest import ToolsetManifest

if TYPE_CHECKING:
    from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry

logger = logging.getLogger("workflow.toolsets")

# Module-level singleton. A ToolsetRegistry rather than a bare catalog, so a
# toolset registered here once is both discoverable by the coding agent and
# callable by ctx.agent() — the two used to be separate stores, and a toolset
# registered in one was invisible to the other.
_catalog: ToolsetRegistry | None = None


def get_catalog() -> ToolsetRegistry:
    """Return the process-global toolset registry."""
    global _catalog
    if _catalog is None:
        from workflow_builder.agents.tool_registry import ToolsetRegistry

        _catalog = ToolsetRegistry()
    return _catalog


def register_toolset(manifest: ToolsetManifest | Toolset) -> None:
    """Register a toolset (or a bare manifest) with the global registry.

    Registering a :class:`Toolset` makes it both discoverable *and* callable.
    Registering a bare :class:`ToolsetManifest` makes it discoverable only.
    """
    get_catalog().register(manifest)
    resolved = manifest if isinstance(manifest, ToolsetManifest) else manifest.manifest
    logger.info(
        "Registered toolset: %s v%s (%s)",
        resolved.id,
        resolved.version,
        resolved.qualified_id,
    )


def unregister_toolset(toolset_id: str) -> None:
    """Remove a toolset from the global registry."""
    get_catalog().unregister(toolset_id)


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
