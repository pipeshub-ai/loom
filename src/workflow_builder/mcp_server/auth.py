"""Turns :class:`IdentitySettings` into what ``FastMCP`` needs to be a resource server.

Deliberately thin, and deliberately the *second* module (after
``mcp_server/server.py``) allowed to import ``mcp`` — it only reaches for
``AuthSettings``/``TransportSecuritySettings``, never protocol internals, and
:mod:`workflow_builder.identity.verifier` (which this module calls into) does
not import ``mcp`` at all. That split is what keeps the verifier
implementations unit-testable with no ``mcp`` package installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow_builder.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings

    from workflow_builder.identity.config import IdentitySettings
    from workflow_builder.identity.verifier import TokenVerifier

__all__ = ["McpAuth", "build_mcp_auth"]


@dataclass(frozen=True)
class McpAuth:
    """Everything :func:`~workflow_builder.mcp_server.server.build_server`
    passes straight through to ``FastMCP(...)``, computed once at startup so
    no per-request check has to re-derive it."""

    token_verifier: TokenVerifier
    auth_settings: AuthSettings
    transport_security: TransportSecuritySettings


def build_mcp_auth(settings: IdentitySettings) -> McpAuth | None:
    """``None`` when *settings* configures no verifier.

    That is the compatibility contract: `loom mcp` with no ``LOOM_AUTH_*``
    env vars set passes ``None`` through to ``FastMCP`` for ``token_verifier``,
    ``auth``, and ``transport_security`` alike, which is exactly how it
    behaved before this module existed.
    """
    from workflow_builder.identity.verifier import build_verifier

    verifier = build_verifier(settings)
    if verifier is None:
        return None
    if not settings.issuer or not settings.resource:
        # Reachable only via `static_tokens_file`, which needs neither —
        # `build_verifier` already enforces this pair for JWKS/introspection.
        raise ConfigurationError(
            "identity is configured but LOOM_AUTH_ISSUER and LOOM_AUTH_RESOURCE "
            "are not both set — the MCP SDK's AuthSettings needs both to "
            "publish correct RFC 9728 protected-resource metadata."
        )

    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings
    from pydantic import AnyHttpUrl

    return McpAuth(
        token_verifier=verifier,
        auth_settings=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.resource),
            required_scopes=list(settings.required_scopes) or None,
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.dns_rebinding_protection,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
    )
