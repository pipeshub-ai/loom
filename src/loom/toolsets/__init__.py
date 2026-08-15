"""Toolset catalog with three-tier lazy disclosure.

The coding agent discovers integrations progressively:

- **Tier 1 — search:** Index cards (~40 tokens each)
- **Tier 2 — show:** Operation table (~300-900 tokens)
- **Tier 3 — stub:** Typed contract (~250-500 tokens)

A typical 3-integration workflow costs ~4.5k tokens of toolset knowledge
vs. millions for eager loading.
"""

from __future__ import annotations

from loom.toolsets.catalog import (
    IndexCard,
    OpContract,
    OpSummary,
    OpTable,
    ToolsetCatalog,
)
from loom.toolsets.connections import ConnectionBroker, Credential
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = [
    "ConnectionBroker",
    "Credential",
    "EffectClass",
    "IndexCard",
    "OpContract",
    "OpSummary",
    "OpTable",
    "OperationSpec",
    "ToolsetCatalog",
    "ToolsetManifest",
]
