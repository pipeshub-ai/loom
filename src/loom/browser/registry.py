"""Which browser providers this process can reach.

``parent=`` chains a Runtime's own registry to the process-global one — the
arrangement ``ToolsetRegistry``, ``NodeRegistry`` and ``ProbeRegistry`` all use.
``register_browser_provider`` and ``loom_browser_provider`` entry points reach
every Runtime, while a locally-constructed registry stays local.

**This is how Stagehand and browser-use arrive without LOOM importing either.**
A host installs `loom-browser-stagehand`, its entry point registers, and
``verify_browser_session`` is what says the adapter is correct. LOOM keeps one
Apache-2.0 dependency and no opinion about which vendor won.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from loom.browser.base import BrowserProvider

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserRegistry",
    "get_browser_registry",
    "load_browser_entry_points",
    "register_browser_provider",
]

ENTRY_POINT_GROUP = "loom_browser_provider"


class BrowserRegistry:
    """The providers available, in registration order."""

    def __init__(self, parent: BrowserRegistry | None = None) -> None:
        self._providers: dict[str, BrowserProvider] = {}
        self._parent = parent

    def register(self, provider: BrowserProvider) -> None:
        self._providers[provider.id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def __len__(self) -> int:
        return len(self.all())

    def __bool__(self) -> bool:
        """Whether anything can drive a page at all.

        The question every caller actually asks, and the reason the
        ``browser.*`` nodes can report "no provider" rather than failing five
        frames into a run.
        """
        return bool(self.all())

    def all(self) -> list[BrowserProvider]:
        """Every provider, local ones first.

        Local wins on id: a caller registering its own ``local`` provider means
        to replace the shipped one, not to be shadowed by it.
        """
        merged: dict[str, BrowserProvider] = {}
        if self._parent is not None:
            merged.update({p.id: p for p in self._parent.all()})
        merged.update(self._providers)
        return list(merged.values())

    def get(self, provider_id: str) -> BrowserProvider | None:
        return next((p for p in self.all() if p.id == provider_id), None)

    def default(self) -> BrowserProvider | None:
        """The first registered provider, or ``None``.

        First rather than best: ranking them would make the answer depend on an
        ordering nobody declared. A host that wants a specific one names it.
        """
        found = self.all()
        return found[0] if found else None


_registry: BrowserRegistry | None = None


def get_browser_registry() -> BrowserRegistry:
    global _registry
    if _registry is None:
        _registry = BrowserRegistry()
    return _registry


def register_browser_provider(provider: BrowserProvider) -> None:
    get_browser_registry().register(provider)
    logger.info("Registered browser provider: %s", provider.id)


def load_browser_entry_points() -> int:
    """Import providers published under ``loom_browser_provider``.

    A provider whose module will not import is skipped and logged, exactly as
    the toolset, node and probe loaders do: one broken package must not take
    the others with it.
    """
    from importlib.metadata import entry_points

    loaded = 0
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register_browser_provider(entry.load()())
            loaded += 1
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("browser provider %s failed to load: %s", entry.name, exc)
    return loaded
