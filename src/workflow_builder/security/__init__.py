"""Security primitives — grant derivation, authorization, and governance."""

from __future__ import annotations

from workflow_builder.security.grants import GrantSet, derive_grants

__all__ = ["GrantSet", "derive_grants"]
