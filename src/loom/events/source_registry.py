"""Which providers this process can accept deliveries from.

The same shape ``ToolsetRegistry`` and ``NodeRegistry`` already have, adopted
unchanged rather than invented in parallel: a per-Runtime registry that chains
to a process-global one, so ``register_event_source(...)`` and a
``loom_event_source`` entry point reach every Runtime while
``rt.sources.register(...)`` stays local to that Runtime.

    # in a third-party package's pyproject.toml
    [project.entry-points.loom_event_source]
    shopify = "acme_shopify.source:ShopifySource"

Nothing in LOOM names Shopify, and nothing has to: new sources arrive by
registration, never by editing a ``match`` statement here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from loom.core.exceptions import ConfigurationError
from loom.events.sources import EventSource

logger = logging.getLogger("workflow.events")

__all__ = [
    "BUILTIN_SOURCES",
    "EventSourceRegistry",
    "discover_source_entry_points",
    "get_source_catalog",
    "register_event_source",
    "unregister_event_source",
]

#: The sources LOOM ships, as ``id -> import path``.
#:
#: Lazy, and for the reason the toolset catalog is: importing every provider's
#: module to answer "is slack registered?" pulls in httpx, models and auth for
#: providers this process will never hear from.
_SOURCES = "loom.toolsets"
BUILTIN_SOURCES: dict[str, str] = {
    "slack": f"{_SOURCES}.slack.source.SlackSource",
    "jira": f"{_SOURCES}.jira.source.JiraSource",
    "gmail": f"{_SOURCES}.google.gmail.source.GmailSource",
}


class EventSourceRegistry:
    """Sources by id, chained to a parent.

    Resolution is local-first: a Runtime that registers its own ``slack`` gets
    it, and the process-global one is the fallback rather than an override. That
    is what makes a test's source local to the test.
    """

    def __init__(self, *, parent: EventSourceRegistry | None = None) -> None:
        self._sources: dict[str, EventSource] = {}
        self._parent = parent

    def register(self, source: EventSource) -> EventSource:
        """Add *source*. Re-registering the same id replaces it.

        Replacement rather than refusal, unlike toolsets: a source has no
        qualified id to distinguish two implementations of one provider, and a
        host swapping in its own Slack verifier is the case that matters more
        than catching a double registration.
        """
        source_id = getattr(source, "id", "")
        if not source_id:
            raise ConfigurationError(
                f"{type(source).__name__} has no `id`. It is what appears in "
                "/hooks/{id} and in every event id the source produces, so it "
                "cannot be derived."
            )
        self._sources[source_id] = source
        return source

    def unregister(self, source_id: str) -> None:
        self._sources.pop(source_id, None)

    def get(self, source_id: str) -> EventSource | None:
        found = self._sources.get(source_id)
        if found is not None:
            return found
        if self._parent is not None:
            return self._parent.get(source_id)
        return builtin_source(source_id)

    def require(self, source_id: str) -> EventSource:
        """Resolve *source_id*, or raise naming what is reachable."""
        found = self.get(source_id)
        if found is not None:
            return found
        known = ", ".join(self.list_sources()) or "none"
        raise ConfigurationError(
            f"no event source '{source_id}' is registered (known: {known}). "
            "Register one with rt.sources.register(...), or install a package "
            "publishing it under the `loom_event_source` entry point."
        )

    def list_sources(self) -> list[str]:
        """Every id reachable from here, including builtins and the parent."""
        ids = set(self._sources)
        if self._parent is not None:
            ids.update(self._parent.list_sources())
        ids.update(BUILTIN_SOURCES)
        return sorted(ids)

    def __contains__(self, source_id: object) -> bool:
        return isinstance(source_id, str) and self.get(source_id) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self.list_sources())

    def __len__(self) -> int:
        return len(self.list_sources())

    def __repr__(self) -> str:
        return f"<EventSourceRegistry {len(self._sources)} local>"


_catalog: EventSourceRegistry | None = None


def get_source_catalog() -> EventSourceRegistry:
    """The process-global source registry."""
    global _catalog
    if _catalog is None:
        _catalog = EventSourceRegistry()
    return _catalog


def register_event_source(source: EventSource) -> EventSource:
    """Register *source* globally, so every Runtime in this process sees it."""
    registered = get_source_catalog().register(source)
    logger.info("Registered event source: %s", getattr(source, "id", "?"))
    return registered


def unregister_event_source(source_id: str) -> None:
    get_source_catalog().unregister(source_id)


def builtin_source(source_id: str) -> EventSource | None:
    """Instantiate a source LOOM ships, by id, or ``None``.

    A fallback rather than eager registration, exactly as ``builtin_toolset``
    is: a process that never receives a Jira delivery should never import the
    Jira source. A builtin whose module will not import — a missing optional
    dependency — is simply absent, which is what it was before it existed.
    """
    import importlib

    path = BUILTIN_SOURCES.get(source_id)
    if path is None:
        return None
    module_path, attribute = path.rsplit(".", 1)
    try:
        factory: Any = getattr(importlib.import_module(module_path), attribute)
    except Exception:
        logger.debug("builtin event source %s failed to import", path, exc_info=True)
        return None
    try:
        return factory()  # type: ignore[no-any-return]
    except Exception:
        logger.debug("builtin event source %s failed to construct", path, exc_info=True)
        return None


def discover_source_entry_points() -> int:
    """Register every ``loom_event_source`` entry point. Returns the count.

    An entry point resolves to either an :class:`EventSource` instance or a
    zero-argument callable producing one — both, because a source with no
    configuration is naturally a class and one with configuration is naturally
    a factory, and forcing either shape makes somebody write a wrapper.
    """
    from importlib.metadata import entry_points

    count = 0
    for ep in entry_points(group="loom_event_source"):
        try:
            obj = ep.load()
            source = obj() if isinstance(obj, type) or callable(obj) else obj
            if not isinstance(source, EventSource):
                logger.warning(
                    "entry point '%s' did not resolve to an EventSource (got %s)",
                    ep.name,
                    type(source).__name__,
                )
                continue
            register_event_source(source)
            count += 1
        except Exception:
            logger.warning(
                "event source entry point '%s' failed to load", ep.name, exc_info=True
            )
    return count
