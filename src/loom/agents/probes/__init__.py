"""Read-only observation of the systems a workflow is written against.

Imported from ``loom.agents.probes``, and deliberately not re-exported at
``loom`` top level — the same boundary ``loom.nodes`` and ``loom.toolsets``
keep. A probe is authoring-time machinery: a workflow body must not be able to
reach one, because anything a workflow does has to go through the journal, and
looking is not something a workflow does.

Concrete probes import lazily so that reading this package costs no playwright
and no network stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.agents.probes.base import Observation, Probe, ProbeError
from loom.agents.probes.registry import (
    ProbeRegistry,
    get_probe_registry,
    load_probe_entry_points,
    register_probe,
)

if TYPE_CHECKING:
    from loom.agents.probes.browser import BrowserProbe
    from loom.agents.probes.http import HttpProbe

__all__ = [
    "BrowserProbe",
    "HttpProbe",
    "Observation",
    "Probe",
    "ProbeError",
    "ProbeRegistry",
    "default_probes",
    "get_probe_registry",
    "load_probe_entry_points",
    "register_probe",
]


def __getattr__(name: str) -> Any:
    if name == "HttpProbe":
        from loom.agents.probes.http import HttpProbe

        return HttpProbe
    if name == "BrowserProbe":
        from loom.agents.probes.browser import BrowserProbe

        return BrowserProbe
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_probes(*, browser: bool = True) -> ProbeRegistry:
    """The probes this environment can actually run.

    ``BrowserProbe`` is included only when playwright imports, because a probe
    that always raises is worse than an absent one: the registry's emptiness is
    what decides whether the agent is offered the tool at all, and a probe that
    is present and broken turns that decision into a runtime error the model has
    to interpret.
    """
    from loom.agents.probes.http import HttpProbe

    registry = ProbeRegistry()
    registry.register(HttpProbe())

    if browser:
        try:
            import playwright.async_api  # noqa: F401

            from loom.agents.probes.browser import BrowserProbe

            registry.register(BrowserProbe())
        except ImportError:
            pass
    return registry
