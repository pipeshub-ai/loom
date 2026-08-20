"""Layer 3: turning a catalog entry into a callable node.

The registry is what separates "this node exists" from "this node can run".
Registration is pure data; the module holding the class is imported the first
time somebody actually resolves it, and once per process thereafter.

``parent=`` chains a Runtime's own registry to the process-global one, the same
arrangement ``ToolsetRegistry`` uses: ``@register_node`` and ``loom_node`` entry
points reach every Runtime, while ``rt.nodes.register(...)`` stays local to that
Runtime.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, TypeVar

from loom.core.exceptions import ConfigurationError
from loom.nodes.base import Node, near_matches
from loom.nodes.catalog import NodeCatalog
from loom.nodes.errors import NodeContractError, NodeNotFound
from loom.nodes.spec import NodeSpec

logger = logging.getLogger(__name__)

__all__ = [
    "NodeRegistry",
    "get_node_catalog",
    "load_node_entry_points",
    "register_node",
]

NodeT = TypeVar("NodeT", bound=type[Node[Any, Any]])


class NodeRegistry(NodeCatalog):
    """A catalog that can also hand back the callable node."""

    def __init__(self, parent: NodeCatalog | None = None) -> None:
        super().__init__()
        self._parent = parent
        self._instances: dict[str, Node[Any, Any]] = {}
        self._classes: dict[str, type[Node[Any, Any]]] = {}
        """Classes registered here directly.

        ``register_node(cls)`` already holds the class, so importing it back
        through ``node_class`` is a round trip that can only lose: a node
        defined inside a function or a notebook has a ``<locals>`` qualname that
        no import can resolve, and it would fail at *resolution* — inside
        somebody's run — rather than at registration."""

    # -- lookup, chained ----------------------------------------------------

    def get(self, node_id: str) -> NodeSpec | None:
        own = super().get(node_id)
        if own is not None:
            return own
        return self._parent.get(node_id) if self._parent is not None else None

    def node_ids(self) -> list[str]:
        ids = set(super().node_ids())
        if self._parent is not None:
            ids |= set(self._parent.node_ids())
        return sorted(ids)

    def categories(self) -> dict[Any, int]:
        counts = dict(super().categories())
        if self._parent is not None:
            own = set(super().node_ids())
            for node_id in self._parent.node_ids():
                if node_id in own:
                    continue
                spec = self._parent.get(node_id)
                if spec is not None:
                    counts[spec.category] = counts.get(spec.category, 0) + 1
        return counts

    def search(self, query: str = "", **kwargs: Any) -> list[Any]:
        limit = kwargs.pop("limit", 10)
        found = super().search(query, limit=limit, **kwargs)
        if self._parent is None:
            return found
        seen = {card.id for card in found}
        own = set(super().node_ids())
        for card in self._parent.search(query, limit=limit, **kwargs):
            # A locally-registered node shadows the global one under the same id;
            # returning both would show the agent two contracts for one call.
            if card.id in seen or card.id in own:
                continue
            found.append(card)
        return found[:limit]

    # -- Layer 3 ------------------------------------------------------------

    def resolve(self, node_id: str, *, runtime: Any = None) -> Node[Any, Any]:
        """Import, construct, and cache the node behind *node_id*.

        When *runtime* is given, ``spec.requires`` is checked here — before any
        body runs and, for a suspending node, before the run parks. A run parked
        with nobody listening is the worst outcome available in this design,
        because it is indistinguishable from patience.

        ``ctx.node`` passes no runtime and calls :meth:`check_requirements`
        itself, from inside the journaled call — see there for why.
        """
        spec = self.get(node_id)
        if spec is None:
            raise NodeNotFound(node_id, suggestions=near_matches(node_id, self.node_ids()))

        instance = self._instances.get(node_id)
        if instance is None:
            instance = self._construct_from(node_id, spec)
            self._instances[node_id] = instance

        if runtime is not None:
            self.check_requirements(instance, runtime)
        return instance

    def check_requirements(self, node: Node[Any, Any], runtime: Any) -> None:
        """Raise unless *runtime* satisfies everything *node* declared.

        Separate from :meth:`resolve` because the two questions are asked at
        different moments: what a node *is* is needed to validate a payload,
        while what a Runtime *has* only matters if the node is about to run.
        """
        missing = type(node).missing_requirements(runtime)
        if missing:
            raise self._requirement_error(type(node).spec, missing)

    def _construct_from(self, node_id: str, spec: NodeSpec) -> Node[Any, Any]:
        """The class registered here, a parent's, or the one ``node_class`` names."""
        known = self._classes.get(node_id)
        if known is not None:
            return known()
        parent_classes: dict[str, type[Node[Any, Any]]] = getattr(
            self._parent, "_classes", {}
        )
        parent_class = parent_classes.get(node_id)
        if parent_class is not None:
            return parent_class()
        return self._construct(spec)

    def _construct(self, spec: NodeSpec) -> Node[Any, Any]:
        if not spec.node_class:
            raise NodeContractError(
                f"{spec.id} has no node_class, so it can be described but not run. "
                "Register the class with @register_node rather than the bare spec."
            )
        module_path, _, qualname = spec.node_class.partition(":")
        if not qualname:
            raise NodeContractError(
                f"{spec.id} declares node_class {spec.node_class!r}; expected "
                "'module:QualName'"
            )
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise NodeContractError(
                f"{spec.id} declares node_class {spec.node_class!r} and "
                f"{module_path!r} does not import: {exc}"
            ) from exc

        target: Any = module
        for part in qualname.split("."):
            target = getattr(target, part, None)
            if target is None:
                raise NodeContractError(
                    f"{spec.id} declares node_class {spec.node_class!r} and "
                    f"{module_path!r} has no {qualname!r}"
                )
        return target()  # type: ignore[no-any-return]

    @staticmethod
    def _requirement_error(spec: NodeSpec, missing: list[str]) -> Exception:
        """Name the capability and how to supply it, not just what is absent."""
        if "human_channel" in missing:
            from loom.nodes.errors import HumanChannelMissing

            return HumanChannelMissing(spec.id)
        return ConfigurationError(
            f"{spec.id} requires {', '.join(missing)}, which this Runtime does "
            "not have configured."
        )

    # -- registration -------------------------------------------------------

    def register_node(self, cls: type[Node[Any, Any]], /) -> NodeSpec:
        spec = super().register_node(cls)
        self._classes[spec.id] = cls
        # A re-registered class must not keep serving the old instance.
        self._instances.pop(spec.id, None)
        return spec

    def unregister(self, node_id: str) -> None:
        super().unregister(node_id)
        self._instances.pop(node_id, None)
        self._classes.pop(node_id, None)


# ---------------------------------------------------------------------------
# The process-global catalog
# ---------------------------------------------------------------------------

_GLOBAL = NodeRegistry()


def get_node_catalog() -> NodeRegistry:
    """The process-global node registry every Runtime chains to."""
    return _GLOBAL


def register_node(cls: NodeT) -> NodeT:
    """Register a node class globally. Usable as a decorator.

    Derives ``input_schema``, ``output_schema``, and ``node_class`` from the
    class, and validates the contract *here* — where the author sees it — rather
    than at resolution inside somebody else's run.
    """
    _GLOBAL.register_node(cls)
    return cls


#: The modules that register the standard library. Imported once, on first use
#: of a registry rather than at ``import loom``.
#:
#: These *are* imported, unlike toolset manifests, and the difference is worth
#: naming. A toolset ships its manifest in a separate data-only module because
#: the code half pulls in an HTTP client and vendor types. A node's schema lives
#: on the class, so splitting it out would mean maintaining the contract twice —
#: exactly the drift ``tests/test_manifest_imports.py`` exists to catch. The
#: trade is deliberate: these modules are pure Python with no I/O and no vendor
#: SDKs, and ``test_node_catalog`` holds their import cost to a budget.
#:
#: The lazy path is still there for anyone who needs it: ``register(spec)`` with
#: a ``node_class`` imports nothing until the node is actually resolved.
_BUILTIN_NODE_MODULES = (
    "loom.nodes.guard.nodes",
    "loom.nodes.human.nodes",
    "loom.nodes.control",
    "loom.nodes.transform",
    "loom.nodes.documents",
    "loom.nodes.knowledge",
    "loom.nodes.io",
    "loom.nodes.browser.nodes",
    "loom.nodes.agentic",
)

_BUILTINS_LOADED = False
_ENTRY_POINTS_LOADED = False


def load_builtin_nodes() -> int:
    """Import the standard-library node modules. Idempotent.

    A failure here **raises**, unlike a third-party entry point, which is logged
    and skipped. The distinction is who the bug belongs to: one broken package
    must not stop the others from loading, but a built-in module that will not
    import is a defect in LOOM, and swallowing it produces a catalog that is
    quietly missing whole categories while reporting a count that looks fine.

    This is not hypothetical. The first run of this function logged two warnings
    and returned 8 — and 8 was a real number, of the third of the library that
    happened to import. The prompt block rendered three categories and looked
    entirely plausible.
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return 0
    _BUILTINS_LOADED = True
    before = len(_GLOBAL.node_ids())
    for module in _BUILTIN_NODE_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            _BUILTINS_LOADED = False
            raise NodeContractError(
                f"the built-in node module {module!r} failed to import: "
                f"{type(exc).__name__}: {exc}. This is a defect in LOOM, not in "
                "your workflow."
            ) from exc
    return len(_GLOBAL.node_ids()) - before


def load_node_entry_points() -> int:
    """Register nodes advertised by installed packages under ``loom_node``.

    Idempotent and lazy: called the first time a Runtime builds its registry, so
    importing ``loom`` does not import every installed node package.
    A broken entry point is logged and skipped — one bad third-party package
    must not stop the others from loading.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return 0
    _ENTRY_POINTS_LOADED = True

    from importlib.metadata import entry_points

    loaded = 0
    for entry in entry_points(group="loom_node"):
        try:
            target = entry.load()
        except Exception:
            logger.warning("loom_node entry point %r failed to load", entry.name, exc_info=True)
            continue
        try:
            if isinstance(target, type) and issubclass(target, Node):
                _GLOBAL.register_node(target)
            elif isinstance(target, NodeSpec):
                _GLOBAL.register(target)
            else:
                logger.warning(
                    "loom_node entry point %r is %r, expected a Node subclass or NodeSpec",
                    entry.name,
                    type(target).__name__,
                )
                continue
        except NodeContractError:
            logger.warning(
                "loom_node entry point %r is not a valid node", entry.name, exc_info=True
            )
            continue
        loaded += 1
    return loaded
