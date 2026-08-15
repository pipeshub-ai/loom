"""Resource system — dependency injection for external resources.

Resources (database connections, HTTP clients, caches) are declared with
the ``@resource`` decorator and injected into steps via ``Depends``.
Scoping controls lifecycle: per-flow, per-worker, or global.
"""

from __future__ import annotations

from loom.resources.base import (
    Depends,
    ResourceDefinition,
    ResourceScope,
    resource,
)

__all__ = [
    "Depends",
    "ResourceDefinition",
    "ResourceScope",
    "resource",
]
