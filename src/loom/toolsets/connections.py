"""Connection broker — resolves connection IDs to scoped credentials.

In **embedded mode** the broker reads credentials from environment variables
or a configuration dict.  In **gateway mode** (Phase 5) it calls the
gateway API for short-lived, scoped tokens.

The worker never sees raw long-lived secrets — credentials are always
short-lived and scope-limited.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.core.exceptions import CredentialNotFound
from loom.runtime.clock import Clock, SystemClock


class Credential(BaseModel):
    """A scoped, short-lived credential."""

    token: str
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def expired(self, clock: Clock | None = None) -> bool:
        """Whether ``expires_at`` has passed, as of *clock*.

        Takes a :class:`~loom.runtime.clock.Clock` rather than
        reading the wall clock directly, so expiry is exercisable with
        ``ManualClock`` — a test that wants to assert "this credential is
        stale" should not have to wait for it to actually become stale, and
        a determinism-sensitive path (this is read from inside a run) should
        not touch ``datetime.now()`` at all. Defaults to :class:`SystemClock`
        for callers outside a run (e.g. ``loom whoami``).
        """
        if self.expires_at is None:
            return False
        now = (clock or SystemClock()).now()
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            # Naive timestamps come from config/env sources that never set
            # one; treat them as UTC rather than raising on comparison.
            expires_at = expires_at.replace(tzinfo=UTC)
        return now >= expires_at


@runtime_checkable
class CredentialResolver(Protocol):
    """Where credentials actually come from.

    Extracted so a host with a credential service, a secrets manager, or a
    connector platform plugs in rather than monkey-patching. The default
    adapter reads the environment and an explicit config dict, which is the
    right answer for a developer and the wrong one for a deployment.

    Two methods, because a token that can expire is the normal case and a
    resolver that could only ever hand back a stale one would push refresh
    logic into every caller.
    """

    async def resolve(
        self, connection_id: str, scopes: list[str] | None = None
    ) -> Credential:
        """The credential for *connection_id*, or raise ``CredentialNotFound``."""
        ...

    async def refresh(self, connection_id: str) -> Credential:
        """A freshly-minted credential, ignoring any cached one."""
        ...

    def has_connection(self, connection_id: str) -> bool:
        """Whether one is available, without minting it."""
        ...


class ConnectionBroker:
    """Exchanges a connection ID for scoped credentials.

    Delegates to a :class:`CredentialResolver`. The default is
    :class:`EnvCredentialResolver`, so the behaviour with no arguments is
    exactly what it has always been::

        LOOM_CONN_{CONNECTION_ID}_TOKEN
        LOOM_CONN_{CONNECTION_ID}_KEY

    A host supplies its own::

        ConnectionBroker(resolver=PlatformCredentials(config_service))
    """

    def __init__(
        self,
        *,
        config: dict[str, dict[str, Any]] | None = None,
        clock: Clock | None = None,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self._config = config or {}
        self._resolver = resolver or EnvCredentialResolver(config=self._config)
        self.clock = clock or SystemClock()
        """Passed through to :meth:`Credential.expired` by callers that check
        expiry against this broker's resolutions, so a test using
        ``ConnectionBroker(clock=ManualClock(...))`` gets deterministic
        expiry without touching the wall clock."""

    async def resolve(
        self,
        connection_id: str,
        scopes: list[str] | None = None,
    ) -> Credential:
        """Resolve a connection ID to a credential.

        Checks the explicit config first, then falls back to environment
        variables.

        Raises :class:`~loom.core.exceptions.CredentialNotFound`
        if no credential is found — the taxonomy every other credential path
        in the SDK raises, so a caller can catch one exception type rather
        than knowing this broker alone raises ``KeyError``.
        """
        return await self._resolver.resolve(connection_id, scopes)

    async def refresh(self, connection_id: str) -> Credential:
        """Mint a fresh credential, bypassing anything cached."""
        return await self._resolver.refresh(connection_id)

    def has_connection(self, connection_id: str) -> bool:
        """Check if a credential is available (without resolving)."""
        return self._resolver.has_connection(connection_id)


class EnvCredentialResolver:
    """The default resolver: an explicit config dict, then the environment.

    Unchanged behaviour, now with a name — which is what makes it replaceable.
    """

    def __init__(self, *, config: dict[str, dict[str, Any]] | None = None) -> None:
        self._config = config or {}

    async def refresh(self, connection_id: str) -> Credential:
        """Environment variables do not refresh; re-reading them is the best
        this resolver can honestly do."""
        return await self.resolve(connection_id)

    async def resolve(
        self,
        connection_id: str,
        scopes: list[str] | None = None,
    ) -> Credential:
        # Check explicit config
        if connection_id in self._config:
            entry = self._config[connection_id]
            return Credential(
                token=str(entry.get("token", "")),
                expires_at=_parse_expiry(entry.get("expires_at")),
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
        raise CredentialNotFound(msg)

    def has_connection(self, connection_id: str) -> bool:
        """Check if a credential is available (without resolving)."""
        if connection_id in self._config:
            return True
        env_prefix = f"LOOM_CONN_{connection_id.upper().replace('-', '_')}"
        return bool(
            os.environ.get(f"{env_prefix}_TOKEN")
            or os.environ.get(f"{env_prefix}_KEY")
        )


def _parse_expiry(value: Any) -> datetime | None:
    """Accept an ``expires_at`` from config as a ``datetime`` or ISO string.

    Config is typically hand-written JSON/YAML, where a datetime can only
    ever arrive as a string — without this, every config-sourced credential
    silently never expires, which is the exact bug this exists to close.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    msg = f"expires_at must be a datetime or ISO string, got {type(value).__name__}"
    raise TypeError(msg)
