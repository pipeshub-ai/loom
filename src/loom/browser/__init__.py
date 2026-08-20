"""Driving a web page, durably.

**Imported from ``loom.browser``, never re-exported at top level** — the rule
``loom.nodes`` and ``loom.toolsets`` already follow. ``Target`` and
``ActionPlan`` are browser vocabulary, and putting them one autocomplete away
from ``loom.step`` would make an already-crowded namespace worse for no gain.

    from loom.browser import BrowserPolicy, LocalBrowserProvider, Target
    rt = Runtime(store=store, browser=LocalBrowserProvider())

Nothing here is imported by ``import loom``, and the Playwright driver is
imported lazily inside :meth:`LocalBrowserProvider.open` — so a process that
never drives a page pays nothing, and the ``[browser]`` extra stays optional.
"""

from __future__ import annotations

from loom.browser.base import (
    ActionMethod,
    ActionPlan,
    ActResult,
    BrowserPolicy,
    BrowserProvider,
    BrowserSession,
    DriftPolicy,
    PageSnapshot,
    SessionHandle,
    SessionScope,
    Target,
    TreeNode,
)
from loom.browser.errors import (
    ActionFailed,
    AmbiguousTarget,
    BrowserUnavailable,
    SelectorDrift,
    SessionLost,
    TargetNotFound,
)
from loom.browser.fake import FakeBrowserProvider, FakeBrowserSession
from loom.browser.local import LocalBrowserProvider, LocalBrowserSession
from loom.browser.registry import (
    BrowserRegistry,
    get_browser_registry,
    load_browser_entry_points,
    register_browser_provider,
)

__all__ = [
    "ActResult",
    "ActionFailed",
    "ActionMethod",
    "ActionPlan",
    "AmbiguousTarget",
    "BrowserPolicy",
    "BrowserProvider",
    "BrowserRegistry",
    "BrowserSession",
    "BrowserUnavailable",
    "DriftPolicy",
    "FakeBrowserProvider",
    "FakeBrowserSession",
    "LocalBrowserProvider",
    "LocalBrowserSession",
    "PageSnapshot",
    "SelectorDrift",
    "SessionHandle",
    "SessionLost",
    "SessionScope",
    "Target",
    "TargetNotFound",
    "TreeNode",
    "get_browser_registry",
    "load_browser_entry_points",
    "register_browser_provider",
]
