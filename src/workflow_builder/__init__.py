"""Deprecated alias for :mod:`loom`.

The package was renamed to match everything else that already said Loom — the
``loom`` CLI, ``$LOOM_STORE``, the ``loom_toolset`` and ``loom_node`` entry
points, and ``[tool.loom]`` in ``pyproject.toml``. ``workflow_builder`` survived
only in ``import`` lines, which is the two-names-for-one-thing shape this project
keeps finding at the root of its own bugs.

    from loom import Context, Runtime, step, workflow

This module makes the old path keep working, warning once, so a rename is not a
flag day for anyone already importing it. It forwards *submodules too*, so
``from workflow_builder.nodes.human import ApprovalIn`` resolves to the same
objects the new path returns — identity is preserved, which matters because an
``isinstance`` check against a doubly-imported class is the classic way a
compatibility shim breaks the thing it is meant to protect.

Remove in 1.0.
"""

from __future__ import annotations

import importlib
import sys
import warnings
from types import ModuleType
from typing import Any

warnings.warn(
    "workflow_builder has been renamed to loom. Update your imports: "
    "`from loom import ...`. The old name will be removed in 1.0.",
    DeprecationWarning,
    stacklevel=2,
)

import loom as _loom  # noqa: E402 - deliberately after the warning above

__all__ = list(getattr(_loom, "__all__", []))
__version__ = getattr(_loom, "__version__", "")


def __getattr__(name: str) -> Any:
    """Serve any attribute, and any submodule, from :mod:`loom`."""
    try:
        return getattr(_loom, name)
    except AttributeError:
        pass
    try:
        module = importlib.import_module(f"loom.{name}")
    except ImportError as exc:  # pragma: no cover - mirrors the real error
        raise AttributeError(f"module 'workflow_builder' has no attribute {name!r}") from exc
    sys.modules[f"workflow_builder.{name}"] = module
    return module


def __dir__() -> list[str]:
    return dir(_loom)


class _Forwarder:
    """Resolves ``workflow_builder.x.y`` to the already-imported ``loom.x.y``.

    A finder rather than a package of stub modules: stubs would import each
    module a second time under a second name, so ``loom.nodes.Node`` and
    ``workflow_builder.nodes.Node`` would be different classes and every
    ``isinstance`` across the boundary would quietly fail.
    """

    _PREFIX = "workflow_builder."

    def find_module(self, fullname: str, path: Any = None) -> Any:  # pragma: no cover
        return self if fullname.startswith(self._PREFIX) else None

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if not fullname.startswith(self._PREFIX):
            return None
        real = "loom." + fullname[len(self._PREFIX) :]
        module = importlib.import_module(real)
        sys.modules[fullname] = module
        return getattr(module, "__spec__", None)

    def load_module(self, fullname: str) -> ModuleType:  # pragma: no cover
        return sys.modules[fullname]


sys.meta_path.insert(0, _Forwarder())
