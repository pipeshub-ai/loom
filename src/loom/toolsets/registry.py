"""Toolset registration and entry-point discovery.

Provides a module-level catalog and convenience functions for registering
toolsets manually or via pip entry points (``loom_toolset`` group).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from loom.toolsets.catalog import ToolsetCatalog
from loom.toolsets.manifest import ToolsetManifest

if TYPE_CHECKING:
    from loom.agents.tool_registry import Toolset, ToolsetRegistry

logger = logging.getLogger("workflow.toolsets")

# Module-level singleton. A ToolsetRegistry rather than a bare catalog, so a
# toolset registered here once is both discoverable by the coding agent and
# callable by ctx.agent() — the two used to be separate stores, and a toolset
# registered in one was invisible to the other.
_catalog: ToolsetRegistry | None = None


#: The toolsets LOOM ships, as ``(manifest import path, tools module)``.
#:
#: Seeded rather than left to the caller because a *generated* workflow is run
#: by a process nobody wrote — ``python generated_workflow.py`` — and
#: ``ctx.agent(toolsets=["jira"])`` asks the global catalog for something no
#: line in that file registers. It failed with "no executable toolset 'jira' is
#: registered (known: none)", which reads as a broken library rather than a
#: missing call.
_TOOLSETS = "loom.toolsets"
BUILTIN_TOOLSETS: tuple[tuple[str, str], ...] = (
    (f"{_TOOLSETS}.jira.manifest.JIRA_MANIFEST", f"{_TOOLSETS}.jira.tools"),
    (
        f"{_TOOLSETS}.confluence.manifest.CONFLUENCE_MANIFEST",
        f"{_TOOLSETS}.confluence.tools",
    ),
    (
        f"{_TOOLSETS}.google.gmail.manifest.GMAIL_MANIFEST",
        f"{_TOOLSETS}.google.gmail.tools",
    ),
    (
        f"{_TOOLSETS}.google.calendar.manifest.GOOGLE_CALENDAR_MANIFEST",
        f"{_TOOLSETS}.google.calendar.tools",
    ),
    (
        f"{_TOOLSETS}.google.drive.manifest.GOOGLE_DRIVE_MANIFEST",
        f"{_TOOLSETS}.google.drive.tools",
    ),
    (
        f"{_TOOLSETS}.google.meet.manifest.GOOGLE_MEET_MANIFEST",
        f"{_TOOLSETS}.google.meet.tools",
    ),
    (
        f"{_TOOLSETS}.google.sheets.manifest.GOOGLE_SHEETS_MANIFEST",
        f"{_TOOLSETS}.google.sheets.tools",
    ),
    (f"{_TOOLSETS}.slack.manifest.SLACK_MANIFEST", f"{_TOOLSETS}.slack.tools"),
    (f"{_TOOLSETS}.zoom.manifest.ZOOM_MANIFEST", f"{_TOOLSETS}.zoom.tools"),
    (f"{_TOOLSETS}.clickup.manifest.CLICKUP_MANIFEST", f"{_TOOLSETS}.clickup.tools"),
    (f"{_TOOLSETS}.asana.manifest.ASANA_MANIFEST", f"{_TOOLSETS}.asana.tools"),
    (
        f"{_TOOLSETS}.salesforce.manifest.SALESFORCE_MANIFEST",
        f"{_TOOLSETS}.salesforce.tools",
    ),
    (f"{_TOOLSETS}.hubspot.manifest.HUBSPOT_MANIFEST", f"{_TOOLSETS}.hubspot.tools"),
    (f"{_TOOLSETS}.stripe.manifest.STRIPE_MANIFEST", f"{_TOOLSETS}.stripe.tools"),
    (f"{_TOOLSETS}.airtable.manifest.AIRTABLE_MANIFEST", f"{_TOOLSETS}.airtable.tools"),
    (
        f"{_TOOLSETS}.quickbooks.manifest.QUICKBOOKS_MANIFEST",
        f"{_TOOLSETS}.quickbooks.tools",
    ),
    (f"{_TOOLSETS}.github.manifest.GITHUB_MANIFEST", f"{_TOOLSETS}.github.tools"),
    (f"{_TOOLSETS}.gitlab.manifest.GITLAB_MANIFEST", f"{_TOOLSETS}.gitlab.tools"),
    (
        f"{_TOOLSETS}.microsoft.onedrive.manifest.ONEDRIVE_MANIFEST",
        f"{_TOOLSETS}.microsoft.onedrive.tools",
    ),
    (
        f"{_TOOLSETS}.microsoft.sharepoint.manifest.SHAREPOINT_MANIFEST",
        f"{_TOOLSETS}.microsoft.sharepoint.tools",
    ),
    (
        f"{_TOOLSETS}.microsoft.teams.manifest.TEAMS_MANIFEST",
        f"{_TOOLSETS}.microsoft.teams.tools",
    ),
    (
        f"{_TOOLSETS}.microsoft.onenote.manifest.ONENOTE_MANIFEST",
        f"{_TOOLSETS}.microsoft.onenote.tools",
    ),
    (
        f"{_TOOLSETS}.microsoft.outlook.mail.manifest.OUTLOOK_MAIL_MANIFEST",
        f"{_TOOLSETS}.microsoft.outlook.mail.tools",
    ),
    (
        f"{_TOOLSETS}.microsoft.outlook.calendar.manifest.OUTLOOK_CALENDAR_MANIFEST",
        f"{_TOOLSETS}.microsoft.outlook.calendar.tools",
    ),
    (f"{_TOOLSETS}.exa.manifest.EXA_MANIFEST", f"{_TOOLSETS}.exa.tools"),
    (f"{_TOOLSETS}.tavily.manifest.TAVILY_MANIFEST", f"{_TOOLSETS}.tavily.tools"),
    (
        f"{_TOOLSETS}.duckduckgo.manifest.DUCKDUCKGO_MANIFEST",
        f"{_TOOLSETS}.duckduckgo.tools",
    ),
)


def _lazy_toolset(manifest: Any, tools_module: str) -> Toolset:
    """An executable toolset that imports its code only when resolved.

    The three-layer contract in one function: the manifest is metadata and
    costs nothing, and the module — with its httpx, its auth, its models —
    loads on the first ``resolve``. Seeding four of these at import time would
    otherwise undo the lazy catalog it is being added to.
    """
    from loom.agents.tool_registry import Toolset

    by_operation = {op.id: op.function for op in manifest.all_operations() if op.function}

    def resolve(op_id: str) -> Any:
        import importlib

        from loom.agents.tools import coerce_tool

        name = by_operation.get(op_id)
        if name is None:
            known = ", ".join(sorted(by_operation)) or "none"
            raise KeyError(f"unknown operation '{op_id}' in '{manifest.id}' (known: {known})")
        # Read the attribute now, not at seed time: a test that installs fakes
        # over the module must be what a later resolve sees.
        return coerce_tool(getattr(importlib.import_module(tools_module), name))

    return Toolset(manifest=manifest, _resolver=resolve)


def builtin_manifests() -> Iterator[tuple[ToolsetManifest, str]]:
    """Every shipped ``(manifest, tools module)`` this process can import.

    One loader for both halves of the built-in tier — :func:`builtin_toolset`
    resolves one by name, :class:`BuiltinToolsetCatalog` reads them all — so
    the two cannot disagree about which toolsets LOOM ships or about what a
    failed import means.

    Manifests only: a ``manifest`` module imports Pydantic models, never
    ``httpx``, never a vendor SDK, and never the ``tools`` module it names.
    A toolset whose manifest will not import is simply absent, exactly as it
    was before this existed.
    """
    import importlib

    for manifest_path, tools_module in BUILTIN_TOOLSETS:
        module_path, attribute = manifest_path.rsplit(".", 1)
        try:
            manifest = getattr(importlib.import_module(module_path), attribute)
        except Exception:
            logger.debug("builtin toolset %s failed to import", manifest_path, exc_info=True)
            continue
        yield manifest, tools_module


def builtin_toolset(toolset_id: str) -> Toolset | None:
    """A toolset LOOM ships, by id, or ``None``.

    A *fallback*, deliberately, rather than eager registration. Seeding the
    four up front made ``resolve_tools()`` — which sweeps every registered
    toolset when given no ids — hand 46 tools to any prompt-only
    ``ctx.agent("summarise this")``, ``jira_delete_issue`` and
    ``gmail_send_message`` among them. An agent that named no integration
    should gain no integration, and the existing grant tests were right to say
    so.

    Resolving by name leaves that sweep untouched and answers the case that was
    actually broken: a generated workflow saying ``toolsets=["jira"]``, run by
    a process that registered nothing.
    """
    for manifest, tools_module in builtin_manifests():
        if manifest.id == toolset_id:
            return _lazy_toolset(manifest, tools_module)
    return None


class BuiltinToolsetCatalog(ToolsetCatalog):
    """The toolsets LOOM ships, as a **discovery** tier. Manifests only.

    The other half of :func:`builtin_toolset`, and the same distinction it
    already draws one layer down: *asking for one by name gets it; asking for
    "everything" does not*. That rule was applied to resolution
    (``ToolsetRegistry.get_toolset``) and never to discovery, so
    ``search_toolsets("jira")`` on a ``loom author`` run answered nothing while
    ``toolsets=["jira"]`` in the generated file resolved perfectly.

    Deliberately **not** the process-global registry. Registering these there
    would put them in ``list_toolsets()`` and so in ``resolve_tools()``'s no-ids
    sweep — which is how a prompt-only ``ctx.agent("summarise this")`` acquires
    ``jira_delete_issue``. This tier answers "what may be named"; it never
    answers "what may be swept".

    Read-only, and **loaded on first read** rather than on construction — see
    :attr:`_manifests`. A ``ToolsetRegistry`` holds one of these permanently,
    so a Runtime that never browses a catalogue never imports the 27 manifest
    modules.
    """

    def __init__(self) -> None:
        self._store: dict[str, ToolsetManifest] = {}
        self._loaded = False
        # Assigns `self._manifests = {}`, which the setter below absorbs.
        super().__init__()

    @property
    def _manifests(self) -> dict[str, ToolsetManifest]:
        """The shipped manifests, imported on the first read of any kind.

        A property rather than an ``_ensure()`` call at the top of each read,
        because the reads are inherited: every method ``ToolsetCatalog`` has
        now — and every one it grows later — goes through this dict, so laziness
        cannot be forgotten in the way parent-chaining repeatedly was.
        """
        if not self._loaded:
            # Set first: the loader touches no attribute of self, but a partial
            # load must not re-enter and duplicate work if that ever changes.
            self._loaded = True
            for manifest, _tools_module in builtin_manifests():
                self._store[manifest.id] = manifest
        return self._store

    @_manifests.setter
    def _manifests(self, value: dict[str, ToolsetManifest]) -> None:
        # Only ``ToolsetCatalog.__init__`` assigns this, and only ``{}``.
        if value:
            raise TypeError("the built-in toolset tier is read-only")

    def register(self, manifest: ToolsetManifest, /) -> None:
        raise TypeError(
            "the built-in toolset tier is read-only — register with "
            "register_toolset() (process-global) or rt.toolsets.register() (local)"
        )

    def unregister(self, toolset_id: str) -> None:
        raise TypeError("the built-in toolset tier is read-only")


_builtin_catalog: BuiltinToolsetCatalog | None = None


def builtin_catalog() -> BuiltinToolsetCatalog:
    """The process-wide built-in discovery tier. Cheap; loads on first read."""
    global _builtin_catalog
    if _builtin_catalog is None:
        _builtin_catalog = BuiltinToolsetCatalog()
    return _builtin_catalog


def get_catalog() -> ToolsetRegistry:
    """Return the process-global toolset registry."""
    global _catalog
    if _catalog is None:
        from loom.agents.tool_registry import ToolsetRegistry

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


def register_available_toolsets() -> list[str]:
    """Put every toolset this process can see on the global catalog.

    Called by ``loom mcp`` so ``search_toolsets`` / ``show_toolset`` list the
    integrations LOOM ships (and any ``loom_toolset`` entry points) without
    the operator having to register them by hand. Builtins stay lazy: the
    tools module is imported on first resolve, not at registration.

    Idempotent, and it never overwrites an id the caller already registered.
    A bare ``Runtime()`` does *not* call this — seeding the four here would
    put ``jira_delete_issue`` on every unscoped ``ctx.agent()``. MCP is an
    integration surface; that default is the other way around.
    """
    catalog = get_catalog()
    # `list_toolsets()` is the *registered* set, and that is the question here.
    # Neither `get()` nor `get_toolset()` answers it any more: both reach the
    # built-in tier, where every one of these already resolves — so asking
    # either one registers nothing at all.
    registered = set(catalog.list_toolsets())
    for manifest, tools_module in builtin_manifests():
        if manifest.id not in registered:
            register_toolset(_lazy_toolset(manifest, tools_module))
    discover_entry_points()
    return sorted(catalog.list_toolsets())


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
