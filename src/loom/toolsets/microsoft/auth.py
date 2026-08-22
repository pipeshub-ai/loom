"""Microsoft Entra ID credentials for Graph, resolved from the environment.

Three modes, tried in order, because the right one depends on what the workflow
is *acting as* and none of them covers every case:

``MS_TENANT_ID`` + ``MS_CLIENT_ID`` + ``MS_CLIENT_SECRET`` + ``MS_REFRESH_TOKEN``
    Delegated. The workflow acts as **a person**, so ``/me`` works and the
    files it sees are that person's. Long-lived and self-renewing.
``MS_TENANT_ID`` + ``MS_CLIENT_ID`` + ``MS_CLIENT_SECRET``
    Client credentials. The workflow acts as **the application**, with the
    application permissions an admin granted. The usual choice for a daemon.
    ``/me`` does not exist here — see :meth:`MicrosoftAuth.is_app_only`.
``MS_GRAPH_ACCESS_TOKEN``
    A token someone else minted. What a test uses and what a gateway hands a
    worker; the only form needing no client secret on the box.

``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` are accepted
as fallbacks, because that is the trio the Azure SDKs already put in an
environment and requiring a second copy under a different prefix is a
configuration bug waiting to happen.

The durable credential outranks the ready-made one, which is the opposite of the
obvious order and the same call ``GoogleAuth`` makes: an access token lives about
an hour, a refresh token or a client secret mints fresh ones indefinitely, and a
workflow that sleeps outlives the access token by design.

Tokens are cached until shortly before they expire and refreshed under a lock,
so a workflow fanning out ten OneDrive steps mints one token, not ten — and
OneDrive and SharePoint share the cache, since they are one API.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.toolsets.microsoft.errors import GraphAuthError, classify

__all__ = [
    "AUTHORITY_HOST",
    "GRAPH_BASE_URL",
    "MicrosoftAuth",
    "MicrosoftCredentials",
    "get_default_auth",
    "graph_base_url",
    "reset_default_auth",
]

#: Public cloud. National clouds (US Gov, 21Vianet) differ in both hosts, so
#: both are constructor arguments and both read an environment override.
AUTHORITY_HOST = "https://login.microsoftonline.com"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

#: Not a scope list. ``/.default`` means "every application permission already
#: granted to this app in this tenant" — naming individual scopes alongside it
#: is an ``invalid_scope`` 400.
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

#: Refresh this many seconds before expiry. A token that expires mid-flight
#: fails a step that had no other reason to fail.
_SKEW = 60.0


@dataclass
class MicrosoftCredentials:
    """Whichever of the three credential shapes the environment supplied."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    access_token: str = ""
    authority_host: str = AUTHORITY_HOST

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MicrosoftCredentials:
        source = os.environ if env is None else env

        def read(*names: str) -> str:
            for name in names:
                value = source.get(name, "")
                if value:
                    return value
            return ""

        return cls(
            tenant_id=read("MS_TENANT_ID", "AZURE_TENANT_ID"),
            client_id=read("MS_CLIENT_ID", "AZURE_CLIENT_ID"),
            client_secret=read("MS_CLIENT_SECRET", "AZURE_CLIENT_SECRET"),
            refresh_token=read("MS_REFRESH_TOKEN"),
            access_token=read("MS_GRAPH_ACCESS_TOKEN"),
            authority_host=read("MS_AUTHORITY_HOST") or AUTHORITY_HOST,
        )

    @property
    def mode(self) -> str:
        """Which flow these credentials select, or ``""`` if none is complete."""
        confidential = bool(self.tenant_id and self.client_id and self.client_secret)
        if confidential and self.refresh_token:
            return "refresh_token"
        if confidential:
            return "client_credentials"
        if self.access_token:
            return "token"
        return ""

    @property
    def token_url(self) -> str:
        return f"{self.authority_host.rstrip('/')}/{self.tenant_id}/oauth2/v2.0/token"


class MicrosoftAuth:
    """Mints and caches an access token for Microsoft Graph."""

    def __init__(
        self,
        credentials: MicrosoftCredentials | None = None,
        *,
        credential_name: str = "microsoft",
        transport: Any = None,
    ) -> None:
        """
        Args:
            credentials: Resolved credentials. Read from the environment when
                omitted.
            credential_name: Key to look for in a run's ``CredentialStore``.
            transport: An ``httpx`` transport for the *token* endpoint.

        ``transport`` exists because the token endpoint is the one call that
        does not go through :class:`GraphSession`, so injecting a transport at
        the client covers every request except the one that authenticates it.
        Without this a test with a fully mocked client still reaches
        ``login.microsoftonline.com`` for real — which is slow, needs network,
        and fails in CI for reasons that have nothing to do with the test.
        """
        self._credentials = credentials or MicrosoftCredentials.from_env()
        self._credential_name = credential_name
        self._transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

        # A CredentialStore bound to the current run might supply what the
        # environment did not, but that can only be checked with an await — so
        # this raises only when nothing could possibly save it.
        if not self._credentials.mode and current_credential_store() is None:
            raise GraphAuthError(_missing_message(credential_name))


    @classmethod
    def from_values(
        cls, values: Mapping[str, str], *, scopes: Sequence[str] = ()
    ) -> MicrosoftAuth:
        """Build from resolved configuration, reading nothing here.

        The single construction path :func:`loom.toolsets.factory.build_client`
        uses. It exists because the factory previously named the *credentials
        holder* and handed that to the client as its ``auth``: that constructs
        without complaint, since nothing checks the type, and raises
        ``AttributeError: no attribute 'headers'`` on the first request.

        *scopes* is accepted and unused — this provider carries them in the
        grant rather than the token request. Taking the argument anyway is what
        lets one factory build all three auth layers without asking which kind
        each one is.
        """
        return cls(credentials=MicrosoftCredentials.from_env(dict(values)))

    @property
    def mode(self) -> str:
        return self._credentials.mode

    @property
    def is_app_only(self) -> bool:
        """True when the token carries no user, so every ``/me`` path fails.

        Client credentials authenticate the *application*. Graph rejects ``/me``
        under such a token with a 400 whose message is roughly "/me request is
        only valid with delegated authentication flow" — an error that surfaces
        from inside whichever step ran first and reads as a broken toolset
        rather than a missing argument. The clients check this and say what to
        pass instead.
        """
        return self._credentials.mode == "client_credentials"

    async def token(self) -> str:
        """Return a valid access token, minting or refreshing if needed.

        A run's bound ``CredentialStore`` is checked first, on every call, and
        never cached here — the store manages its own expiry and refresh, and
        caching its answer on top would serve a token an hour after the store
        would itself have replaced it.
        """
        supplied = await resolve_bearer_token(self._credential_name)
        if supplied:
            return supplied

        if self._token and time.monotonic() < self._expires_at - _SKEW:
            return self._token

        async with self._lock:
            # Another caller may have refreshed while this one waited.
            if self._token and time.monotonic() < self._expires_at - _SKEW:
                return self._token
            return await self._mint()

    async def headers(self) -> dict[str, str]:
        """Authorization headers for a Graph request."""
        return {
            "Authorization": f"Bearer {await self.token()}",
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        """Drop the cached token, so the next call mints a fresh one.

        Called when Graph rejects a token the cache still believed in — a
        revoked grant, or a clock further out than the skew allows.
        """
        self._token = ""
        self._expires_at = 0.0

    # -- minting -------------------------------------------------------------

    async def _mint(self) -> str:
        mode = self._credentials.mode
        if not mode:
            raise GraphAuthError(_missing_message(self._credential_name))

        if mode == "token":
            # Supplied ready-made. Its lifetime is the caller's problem; treat
            # it as valid and let a 401 surface rather than guessing an expiry.
            self._token = self._credentials.access_token
            self._expires_at = float("inf")
            return self._token

        if mode == "refresh_token":
            payload = {
                "grant_type": "refresh_token",
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "refresh_token": self._credentials.refresh_token,
                # offline_access is what buys a *replacement* refresh token in
                # the response. Without it the grant expires and the workflow
                # stops working weeks later for no visible reason.
                "scope": f"{_DEFAULT_SCOPE} offline_access",
            }
        else:
            payload = {
                "grant_type": "client_credentials",
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "scope": _DEFAULT_SCOPE,
            }

        data = await self._post_token(payload)
        token = str(data.get("access_token", ""))
        if not token:
            raise GraphAuthError(f"Token endpoint returned no access_token: {data}")

        self._token = token
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return token

    async def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        import httpx

        url = self._credentials.token_url
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.post(url, data=payload)
        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise classify(
                response.status_code, body, url, dict(response.headers)
            ) from None
        result: dict[str, Any] = response.json()
        return result


def graph_base_url() -> str:
    """The Graph host to call, honouring ``MS_GRAPH_BASE_URL``.

    A national cloud moves **both** hosts: US Gov authenticates against
    ``login.microsoftonline.us`` and serves data from ``graph.microsoft.us``.
    Making only the authority overridable is worse than making neither, because
    the tenant then authenticates correctly and calls the commercial Graph —
    which fails with an authorization error that names the token, not the host.

    Every default client reads this, so one variable moves a whole deployment.
    """
    return os.environ.get("MS_GRAPH_BASE_URL", "") or GRAPH_BASE_URL


def _missing_message(credential_name: str) -> str:
    return (
        "No Microsoft credentials found. Set MS_TENANT_ID + MS_CLIENT_ID + "
        "MS_CLIENT_SECRET (app-only), add MS_REFRESH_TOKEN to act as a person, "
        "or set MS_GRAPH_ACCESS_TOKEN — or connect a "
        f"'{credential_name}' credential via a CredentialStore."
    )


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default: MicrosoftAuth | None = None


def get_default_auth() -> MicrosoftAuth:
    """Return the process-wide :class:`MicrosoftAuth`, building it on first use.

    Shared between OneDrive and SharePoint deliberately: they are the same API
    behind the same token, and one cached token serves both. Unlike Google's
    equivalent there are no per-toolset scopes to reconcile — ``/.default``
    resolves to whatever the tenant granted the app, so a second toolset can
    never widen what the first one minted.
    """
    global _default
    if _default is None:
        _default = MicrosoftAuth()
    return _default


def reset_default_auth() -> None:
    """Drop the process-wide auth. For tests, and for a credential rotation."""
    global _default
    _default = None
