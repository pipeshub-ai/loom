"""Turns :class:`IdentitySettings` into a FastAPI dependency returning a
:class:`~workflow_builder.identity.principal.Principal`.

Deliberately does not import ``mcp`` anywhere, even lazily: the plain HTTP
surface only ever needs the ``api`` and ``identity`` extras, and pulling in
the MCP SDK just to build a nine-line JSON body would tie an install that
never touches MCP to an extra it does not use. So this hand-rolls the one
RFC 9728 "protected resource metadata" response that
:mod:`workflow_builder.mcp_server.auth` gets for free from
``mcp.server.auth.routes`` — the SDK is deliberately imported nowhere on
this path.

:func:`build_verifier` — the actual token-checking logic — is shared with
``mcp_server/auth.py`` via :mod:`workflow_builder.identity.verifier`, which
for the same reason returns LOOM's own :class:`~workflow_builder.identity.verifier.VerifiedToken`
rather than an ``mcp`` class. One implementation of "check a bearer token",
two surfaces that need one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.identity.principal import ANONYMOUS, Principal

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from workflow_builder.identity.config import IdentitySettings
    from workflow_builder.identity.verifier import TokenVerifier

__all__ = [
    "HttpAuth",
    "build_http_auth",
    "build_principal_dependency",
    "mount_protected_resource_metadata",
]


def _resource_metadata_url(resource: str) -> str:
    """RFC 9728 §3.1: the well-known segment goes between host and path,
    e.g. ``https://x.com/api`` -> ``https://x.com/.well-known/oauth-protected-resource/api``."""
    parsed = urlparse(resource)
    path = parsed.path if parsed.path not in ("", "/") else ""
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{path}"


@dataclass(frozen=True)
class HttpAuth:
    """Everything the HTTP surface needs to authenticate a request, computed
    once at ``create_app()`` time so no request re-derives it."""

    verifier: TokenVerifier
    resource: str
    issuer: str
    metadata_url: str


def build_http_auth(settings: IdentitySettings) -> HttpAuth | None:
    """``None`` when *settings* configures no verifier.

    That is the compatibility contract, identical to
    ``mcp_server.auth.build_mcp_auth``: an install with no ``LOOM_AUTH_*``
    env vars set gets a ``create_app()`` that behaves exactly as it did
    before this module existed — no dependency wrapping, no 401s, no new
    route.
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
            "are not both set — RFC 9728 protected-resource metadata needs both "
            "to tell a client which authorization server issues tokens for which "
            "resource."
        )
    return HttpAuth(
        verifier=verifier,
        resource=settings.resource,
        issuer=settings.issuer,
        metadata_url=_resource_metadata_url(settings.resource),
    )


def mount_protected_resource_metadata(app: FastAPI, auth: HttpAuth) -> None:
    """Serve RFC 9728 metadata at the URL a 401's ``WWW-Authenticate``
    header points to, so a generic OAuth client can discover the
    authorization server without LOOM having to be one."""
    path = urlparse(auth.metadata_url).path

    @app.get(path, include_in_schema=False)
    async def protected_resource_metadata() -> dict[str, Any]:
        return {
            "resource": auth.resource,
            "authorization_servers": [auth.issuer],
            "bearer_methods_supported": ["header"],
        }


_bearer_scheme = HTTPBearer(auto_error=False)
#: Assigned once rather than called inline in a default: same reasoning as
#: ``server/app.py``'s own ``injected = Depends(_facade)``.
_bearer_injected = Depends(_bearer_scheme)


def _unauthorized(auth: HttpAuth, *, description: str) -> HTTPException:
    """A 401 shaped like the MCP SDK's own ``RequireAuthMiddleware`` —
    same ``WWW-Authenticate`` grammar on both of LOOM's HTTP-ish surfaces,
    so a client written against one recognizes the other."""
    www_authenticate = (
        f'Bearer error="invalid_token", error_description="{description}", '
        f'resource_metadata="{auth.metadata_url}"'
    )
    return HTTPException(
        status_code=401,
        detail={"error": "invalid_token", "error_description": description},
        headers={"WWW-Authenticate": www_authenticate},
    )


def build_principal_dependency(
    auth: HttpAuth | None,
) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """A FastAPI dependency resolving the caller's :class:`Principal`.

    ``auth=None`` (identity not configured) returns a dependency that never
    raises and always answers :data:`ANONYMOUS` — ``create_app()`` never
    wraps the facade in that case, so the value is unused but the shape
    stays uniform for every route that asks for one.
    """

    async def _dependency(
        credentials: HTTPAuthorizationCredentials | None = _bearer_injected,
    ) -> Principal:
        if auth is None:
            return ANONYMOUS
        if credentials is None:
            raise _unauthorized(auth, description="Authentication required")
        verified = await auth.verifier.verify_token(credentials.credentials)
        if verified is None:
            raise _unauthorized(auth, description="Invalid or expired token")
        return Principal.from_access_token(verified)

    return _dependency
