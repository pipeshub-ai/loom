"""``IdentitySettings`` — env-driven configuration for verifying bearer tokens.

Read once at server startup (`loom mcp`, `loom serve`), not per request. The
whole point of a settings object rather than scattered ``os.environ.get()``
calls the way :mod:`loom.connectors.credentials` reads
``LOOM_CREDENTIAL_KEY`` is that :func:`~loom.identity.verifier.build_verifier`
needs several fields together and consistently — a JWKS verifier with an
issuer but no resource cannot bind an audience, and that must be caught in
one place with one clear message, not three separate ``None`` checks
scattered across call sites.

Every field is optional, and :meth:`IdentitySettings.is_configured` is
``False`` by default — an install that sets none of these env vars gets
`loom mcp`/`loom serve` behaving exactly as before this module existed, per
the plan's compatibility requirement.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from loom.core.exceptions import ConfigurationError

__all__ = ["LOOPBACK_HOSTS", "IdentitySettings", "StaticPrincipalToken"]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""Hosts a bind to which stays on this machine. Shared by ``loom mcp``'s and
``loom serve``'s pre-bind guard — see :meth:`IdentitySettings.is_configured`
— so the two commands cannot drift on what counts as "safe to serve
unauthenticated"."""


class StaticPrincipalToken(BaseModel):
    """One entry of a static token file — no IdP, a fixed shared secret.

    For self-hosted deployments and tests, not for anything reachable from
    the open internet: a leaked entry here is a leaked credential with no
    expiry and no revocation short of removing the line.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str
    scopes: list[str] = Field(default_factory=list)
    client_id: str = ""


class IdentitySettings(BaseSettings):
    """How to verify a bearer token presented to LOOM's MCP or HTTP surface.

    Every field maps to an env var under the ``LOOM_AUTH_`` prefix, e.g.
    ``LOOM_AUTH_JWKS_URI``. Three mutually exclusive verifier configurations
    are supported — set at most one of ``jwks_uri``, ``introspection_endpoint``,
    ``static_tokens_file`` — chosen in that order by
    :func:`~loom.identity.verifier.build_verifier` if more than
    one is somehow set (JWKS needs no round trip per request; introspection
    does; static needs neither but authenticates nobody an operator did not
    hand-list).
    """

    model_config = SettingsConfigDict(env_prefix="LOOM_AUTH_", extra="ignore")

    issuer: str | None = None
    """The authorization server's issuer URL. Required together with
    ``resource`` by both the JWKS and introspection verifiers — a token's
    ``iss``/``aud`` are what stop a token minted for a *different* resource
    server from being replayed against this one (RFC 8707)."""

    resource: str | None = None
    """This server's own resource identifier — its externally reachable
    URL. Passed to the MCP SDK's ``AuthSettings.resource_server_url`` and
    checked as the token's audience."""

    jwks_uri: str | None = None
    """A JWKS endpoint for verifying RS/ES-signed access tokens locally, no
    round trip to the authorization server per request."""

    algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    leeway_seconds: float = 60.0
    """Clock-skew tolerance on ``exp``/``nbf`` — matches the skew
    ``toolsets/google``'s existing token handling already allows."""

    required_scopes: list[str] = Field(default_factory=list)
    """Passed straight to ``AuthSettings.required_scopes`` — the MCP SDK
    itself rejects a token missing one of these before a tool ever runs."""

    introspection_endpoint: str | None = None
    """An RFC 7662 introspection endpoint, for opaque tokens a JWKS cannot
    verify locally."""
    introspection_client_id: str | None = None
    introspection_client_secret: str | None = None

    static_tokens_file: str | None = None
    """A JSON file of ``{"<token>": {"subject": ..., "scopes": [...],
    "client_id": ...}}`` — see :class:`StaticPrincipalToken`. Not a secret
    store; the tokens themselves live in this file in cleartext, hence
    "for self-hosted and tests" in every docstring nearby."""

    dns_rebinding_protection: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)

    def is_configured(self) -> bool:
        """Whether *any* verifier can be built from this settings object.

        The gate ``loom mcp``/``loom serve`` check before binding to a
        non-loopback host with no auth — see their corner-case docs."""
        return bool(self.jwks_uri or self.introspection_endpoint or self.static_tokens_file)

    def load_static_tokens(self) -> dict[str, StaticPrincipalToken]:
        """Parse ``static_tokens_file``. Empty when unset.

        Raises :class:`ConfigurationError` rather than a bare ``json`` or
        ``KeyError`` traceback — this runs at startup, and "which file, what
        was wrong with it" is the whole difference between a one-line fix
        and a bisection session.
        """
        if not self.static_tokens_file:
            return {}
        path = Path(self.static_tokens_file)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(
                f"LOOM_AUTH_STATIC_TOKENS_FILE={path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"LOOM_AUTH_STATIC_TOKENS_FILE={path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(
                f"LOOM_AUTH_STATIC_TOKENS_FILE={path} must be a JSON object of "
                "token -> {subject, scopes, client_id}"
            )
        try:
            return {
                token: StaticPrincipalToken.model_validate(entry)
                for token, entry in raw.items()
            }
        except Exception as exc:
            raise ConfigurationError(
                f"LOOM_AUTH_STATIC_TOKENS_FILE={path}: {exc}"
            ) from exc
