"""Which probes this process can reach.

``parent=`` chains a caller's own registry to the process-global one, the
arrangement ``ToolsetRegistry`` and ``NodeRegistry`` both use: ``register_probe``
and ``loom_probe`` entry points reach everyone, while a locally-constructed
registry stays local. A host that can observe something loom has never heard of
writes one class and registers it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.agents.probes.base import Probe

logger = logging.getLogger(__name__)

__all__ = [
    "ProbeRegistry",
    "get_probe_registry",
    "load_probe_entry_points",
    "register_probe",
]

ENTRY_POINT_GROUP = "loom_probe"


class ProbeRegistry:
    """The probes available, in registration order."""

    def __init__(self, parent: ProbeRegistry | None = None) -> None:
        self._probes: dict[str, Probe] = {}
        self._parent = parent

    def register(self, probe: Probe) -> None:
        """Add *probe*, replacing any earlier one with the same id."""
        self._probes[probe.id] = probe

    def unregister(self, probe_id: str) -> None:
        self._probes.pop(probe_id, None)

    def __len__(self) -> int:
        return len(self.all())

    def __bool__(self) -> bool:
        """Whether anything can be observed at all.

        The question every caller actually asks. An empty registry means the
        ``observe`` tool is not offered rather than offered and useless.
        """
        return bool(self.all())

    def all(self) -> list[Probe]:
        """Every probe, local ones first.

        Local wins on id: a caller that registers its own ``http`` probe means
        to replace the shipped one, not to be shadowed by it.
        """
        merged: dict[str, Probe] = {}
        if self._parent is not None:
            merged.update({p.id: p for p in self._parent.all()})
        merged.update(self._probes)
        return list(merged.values())

    def get(self, probe_id: str) -> Probe | None:
        return next((p for p in self.all() if p.id == probe_id), None)

    def for_target(self, target: str) -> Probe | None:
        """The first probe that says it can look at *target*.

        First rather than best: ``supports`` is the probe's own claim, and
        ordering it by some notion of quality would make the answer depend on a
        ranking nobody declared. A caller that wants a specific one names it.
        """
        for probe in self.all():
            try:
                if probe.supports(target):
                    return probe
            except Exception:  # a broken probe must not hide the working ones
                logger.warning("probe %s raised from supports()", probe.id)
        return None


    def prompt_block(self) -> str:
        """What can be looked at, and when to bother. Empty when nothing can.

        The tool's own docstring is in the tool list, which is not the same as
        the model knowing this task calls for it. Observed on the task that
        motivated probes: with `observe_target` available and a URL in the spec,
        the agent spent forty turns reading the tool docs of every integration
        it had — quickbooks, sharepoint, teams, tavily — and reached for the
        probe once, on the wrong URL, at the last turn. A capability nothing
        points at is a capability nobody uses.

        O(probes), and probes are a handful by nature — one per way of looking,
        not one per thing that can be looked at.
        """
        probes = self.all()
        if not probes:
            return ""
        listed = ", ".join(f"`{p.id}`" for p in sorted(probes, key=lambda p: p.id))
        return (
            "## Looking before you write\n"
            "\n"
            "`observe_target(target, hint=…, probe=…)` reads a real system and "
            "reports what is actually there. Available probes: " + listed + ".\n"
            "\n"
            "**Use it whenever the spec names a URL or an external system whose "
            "shape you would otherwise guess at** — an API's field names, a "
            "page's controls. Do it before writing the code that depends on "
            "them, not after it fails.\n"
            "\n"
            "Guessing does not fail loudly here. A field name that does not "
            "exist returns null; a selector that matches nothing returns an "
            "empty list and no error. Both produce a workflow that runs, "
            "completes, and is wrong.\n"
            "\n"
            "It is read-only: it cannot submit, click, or write anything.\n"
        )


_registry: ProbeRegistry | None = None


def get_probe_registry() -> ProbeRegistry:
    """The process-global registry every caller chains to."""
    global _registry
    if _registry is None:
        _registry = ProbeRegistry()
    return _registry


def register_probe(probe: Probe) -> None:
    """Register *probe* globally."""
    get_probe_registry().register(probe)
    logger.info("Registered probe: %s", probe.id)


def load_probe_entry_points() -> int:
    """Import probes published under ``loom_probe``. Returns how many loaded.

    A probe whose module will not import is skipped and logged, exactly as the
    toolset and node loaders do: one broken package must not take the others
    with it.
    """
    from importlib.metadata import entry_points

    loaded = 0
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register_probe(entry.load()())
            loaded += 1
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("probe entry point %s failed to load: %s", entry.name, exc)
    return loaded
