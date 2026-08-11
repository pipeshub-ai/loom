"""Connection broker — resolves connection IDs to scoped credentials.

In **embedded mode** the broker reads credentials from environment variables
or a configuration dict.  In **gateway mode** (Phase 5) it calls the
gateway API for short-lived, scoped tokens.

The worker never sees raw long-lived secrets — credentials are always
short-lived and scope-limited.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Credential(BaseModel):
    """A scoped, short-lived credential."""

    token: str
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """Check if this credential has expired."""
        if self.expires_at is None:
            return False
        return datetime.now().astimezone() >= self.expires_at


class ConnectionBroker:
    """Exchanges a connection ID for scoped credentials.

    In embedded mode, credentials are resolved from environment variables
    or an explicit config dict.  The env var naming convention is::

        LOOM_CONN_{CONNECTION_ID}_TOKEN
        LOOM_CONN_{CONNECTION_ID}_KEY
    """

    def __init__(
        self,
        *,
        config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._config = config or {}

    async def resolve(
        self,
        connection_id: str,
        scopes: list[str] | None = None,
    ) -> Credential:
        """Resolve a connection ID to a credential.

        Checks the explicit config first, then falls back to environment
        variables.

        Raises ``KeyError`` if no credential is found.
        """
        # Check explicit config
        if connection_id in self._config:
            entry = self._config[connection_id]
            return Credential(
                token=str(entry.get("token", "")),
                scopes=scopes or entry.get("scopes", []),
                metadata=entry.get("metadata", {}),
            )

        # Fall back to environment variables
        env_prefix = f"LOOM_CONN_{connection_id.upper().replace('-', '_')}"
        token = os.environ.get(f"{env_prefix}_TOKEN") or os.environ.get(
            f"{env_prefix}_KEY"
        )
        if token:
            return Credential(token=token, scopes=scopes or [])

        msg = (
            f"No credential found for connection '{connection_id}'. "
            f"Set {env_prefix}_TOKEN or configure via ConnectionBroker(config=...)"
        )
        raise KeyError(msg)

    def has_connection(self, connection_id: str) -> bool:
        """Check if a credential is available (without resolving)."""
        if connection_id in self._config:
            return True
        env_prefix = f"LOOM_CONN_{connection_id.upper().replace('-', '_')}"
        return bool(
            os.environ.get(f"{env_prefix}_TOKEN")
            or os.environ.get(f"{env_prefix}_KEY")
        )
