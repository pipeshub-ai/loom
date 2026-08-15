"""``TokenVerifier`` implementations — the one method LOOM had to write.

`Section 1 of the plan <../../docs/implementation-plan.md>`_: the installed
``mcp`` SDK already implements OAuth 2.1's protocol surface — discovery
metadata, the authorization endpoints, bearer extraction. The single seam
it leaves for a resource server to fill in is
``TokenVerifier.verify_token(token) -> VerifiedToken | None``, and that is
all three classes below implement.

``VerifiedToken`` below is LOOM's own type, shaped identically to
``mcp.server.auth.provider.AccessToken`` but not that class — the MCP SDK's
own consumers (``BearerAuthBackend``, ``AuthenticatedUser``) never do an
``isinstance`` check, only attribute access, so a structurally-matching
value works for ``FastMCP(token_verifier=...)`` without this module (or its
callers) importing ``mcp`` at all. That is what lets ``server/auth.py``
(the plain HTTP surface, needing only the ``identity`` extra) share these
verifiers with ``mcp_server/auth.py`` — one implementation of "check a
bearer token", used by both surfaces that need one.

Three verifiers, chosen by whichever of :class:`IdentitySettings`'s fields
is set:

- :class:`JWKSTokenVerifier` — RS/ES-signed access tokens, verified locally
  against a JWKS endpoint. No round trip to the authorization server per
  request. The reference for most real deployments.
- :class:`IntrospectionTokenVerifier` — RFC 7662, for opaque tokens a JWKS
  cannot check locally. One HTTP round trip per verification.
- :class:`StaticTokenVerifier` — a fixed token -> principal map, no network
  at all. For self-hosted setups and tests, documented everywhere as such.

Every one of them fails closed: a network error, a malformed response, or
a token missing a claim this needs all return ``None`` rather than raise,
because both surfaces turn ``None`` into a 401 and a raised exception would
otherwise surface as an unhandled 500 for something that is, from the
caller's side, just an invalid token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from workflow_builder.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from workflow_builder.identity.config import IdentitySettings

__all__ = [
    "IntrospectionTokenVerifier",
    "JWKSTokenVerifier",
    "StaticTokenVerifier",
    "TokenVerifier",
    "VerifiedToken",
    "build_verifier",
]


@dataclass
class VerifiedToken:
    """A bearer token, checked out. Shaped like ``mcp.server.auth.provider.AccessToken``
    on purpose — see the module docstring for why that shape and not that class.

    Not frozen, to match: ``AccessToken`` is a mutable pydantic ``BaseModel``,
    and :class:`~workflow_builder.identity.principal.Principal.from_access_token`'s
    ``_VerifiedToken`` protocol is checked structurally — a frozen dataclass's
    read-only attributes do not satisfy a protocol whose members mypy treats
    as assignable, even though nothing here actually reassigns one."""

    token: str
    client_id: str
    scopes: list[str] = field(default_factory=list)
    expires_at: int | None = None
    resource: str | None = None
    subject: str | None = None
    claims: dict[str, Any] | None = None


@runtime_checkable
class TokenVerifier(Protocol):
    """What every verifier below implements, and all ``mcp_server/auth.py``
    or ``server/auth.py`` need to accept one — neither has to know which
    concrete verifier it was handed."""

    async def verify_token(self, token: str) -> VerifiedToken | None: ...


def _scopes_from_claims(claims: Mapping[str, Any]) -> list[str]:
    """OAuth2's ``scope`` (space-separated string) or the ``scp`` some IdPs
    (Auth0, Okta) use instead, as either a string or a list."""
    scope = claims.get("scope")
    if isinstance(scope, str):
        return scope.split()
    scp = claims.get("scp")
    if isinstance(scp, str):
        return scp.split()
    if isinstance(scp, list):
        return [str(item) for item in scp]
    return []


class JWKSTokenVerifier:
    """Verifies a locally-signed access token against a JWKS endpoint.

    Signature verification, key rotation, and JWKS caching (default 300s)
    are all ``PyJWT``'s ``PyJWKClient`` — needs ``pip install
    'workflow-builder[identity]'``. The one thing this class owns is what
    the plan calls out as the piece implementers most often get wrong:
    checking the token's audience against *this* resource server (RFC 8707)
    rather than trusting that a validly-signed token was meant for it.
    """

    def __init__(
        self,
        *,
        jwks_uri: str,
        issuer: str,
        resource: str,
        algorithms: list[str] | None = None,
        leeway: float = 60.0,
        http_timeout: float = 10.0,
    ) -> None:
        self._issuer = issuer
        self._resource = resource
        self._algorithms = list(algorithms or ["RS256", "ES256"])
        self._leeway = leeway
        try:
            import jwt
        except ImportError:
            raise ConfigurationError(
                "verifying a JWKS-signed token needs PyJWT: "
                "pip install 'workflow-builder[identity]'"
            ) from None
        self._jwt = jwt
        self._jwks_client = jwt.PyJWKClient(
            jwks_uri, cache_keys=True, lifespan=300, timeout=http_timeout
        )

    async def verify_token(self, token: str) -> VerifiedToken | None:
        try:
            signing_key = await asyncio.to_thread(
                self._jwks_client.get_signing_key_from_jwt, token
            )
            claims = await asyncio.to_thread(
                self._jwt.decode,
                token,
                signing_key.key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._resource,
                leeway=self._leeway,
            )
        except self._jwt.PyJWTError:
            # Covers signature failure, expiry, wrong issuer/audience, and a
            # JWKS fetch/parse failure alike (`PyJWKClientError` and
            # `PyJWKClientConnectionError` both subclass `PyJWTError`) — all
            # of them mean "cannot vouch for this token", which is a 401,
            # not a 500.
            return None

        subject = claims.get("sub")
        if not subject:
            return None
        return VerifiedToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=_scopes_from_claims(claims),
            expires_at=claims.get("exp"),
            resource=self._resource,
            subject=subject,
            claims=claims,
        )


class IntrospectionTokenVerifier:
    """Verifies an opaque token via RFC 7662 introspection.

    One HTTP round trip per call — no local caching of the *result*, since
    an introspection response has no signature to trust once cached; a
    revoked token must show up on the very next check. ``httpx`` is not a
    core dependency (see ``connectors/oauth_client.py`` for the same
    choice), so it is imported lazily here too.
    """

    def __init__(
        self,
        *,
        introspection_endpoint: str,
        client_id: str,
        client_secret: str,
        resource: str | None = None,
        http_timeout: float = 10.0,
    ) -> None:
        self._endpoint = introspection_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._resource = resource
        self._timeout = http_timeout

    async def verify_token(self, token: str) -> VerifiedToken | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._endpoint,
                    data={"token": token},
                    auth=(self._client_id, self._client_secret),
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        if not isinstance(body, dict) or not body.get("active"):
            return None
        if self._resource is not None:
            audience = body.get("aud")
            audiences = audience if isinstance(audience, list) else [audience]
            if self._resource not in audiences:
                return None

        subject = body.get("sub")
        if not subject:
            return None
        return VerifiedToken(
            token=token,
            client_id=str(body.get("client_id") or body.get("azp") or ""),
            scopes=_scopes_from_claims(body),
            expires_at=body.get("exp"),
            resource=self._resource,
            subject=subject,
            claims=body,
        )


class StaticTokenVerifier:
    """A fixed token -> principal map. No network, no expiry, no IdP.

    For self-hosted deployments that are not fronted by a real
    authorization server, and for tests — never for a token reachable from
    the open internet, since revoking one means editing the file and
    restarting the process.
    """

    def __init__(
        self,
        tokens: Mapping[str, Any],
        *,
        resource: str | None = None,
    ) -> None:
        self._tokens = dict(tokens)
        self._resource = resource

    async def verify_token(self, token: str) -> VerifiedToken | None:
        entry = self._tokens.get(token)
        if entry is None:
            return None
        return VerifiedToken(
            token=token,
            client_id=entry.client_id,
            scopes=list(entry.scopes),
            resource=self._resource,
            subject=entry.subject,
        )


def build_verifier(settings: IdentitySettings) -> TokenVerifier | None:
    """The one place that turns settings into a verifier.

    ``None`` when nothing is configured — the caller (`mcp_server/auth.py`,
    later `server/auth.py`) then wires no auth at all, which is what keeps
    an unconfigured install behaving exactly as it did before this module
    existed. Order matters when more than one is set: static needs no
    network and is checked first, then JWKS (no per-request round trip),
    then introspection.
    """
    if settings.static_tokens_file:
        tokens = settings.load_static_tokens()
        return StaticTokenVerifier(tokens, resource=settings.resource)

    if settings.jwks_uri:
        if not settings.issuer or not settings.resource:
            raise ConfigurationError(
                "LOOM_AUTH_JWKS_URI needs LOOM_AUTH_ISSUER and LOOM_AUTH_RESOURCE "
                "set too — a resource server checks both a token's issuer and its "
                "audience, and cannot check what it was never told to expect."
            )
        return JWKSTokenVerifier(
            jwks_uri=settings.jwks_uri,
            issuer=settings.issuer,
            resource=settings.resource,
            algorithms=settings.algorithms,
            leeway=settings.leeway_seconds,
        )

    if settings.introspection_endpoint:
        if not settings.introspection_client_id or not settings.introspection_client_secret:
            raise ConfigurationError(
                "LOOM_AUTH_INTROSPECTION_ENDPOINT needs "
                "LOOM_AUTH_INTROSPECTION_CLIENT_ID and "
                "LOOM_AUTH_INTROSPECTION_CLIENT_SECRET too — introspection is "
                "itself an authenticated call."
            )
        return IntrospectionTokenVerifier(
            introspection_endpoint=settings.introspection_endpoint,
            client_id=settings.introspection_client_id,
            client_secret=settings.introspection_client_secret,
            resource=settings.resource,
        )

    return None
