"""Zoom credentials — Server-to-Server OAuth, and the two alternatives.

Three sources, tried in order, because the right one depends on what the
workflow is acting as:

``ZOOM_ACCOUNT_ID`` + ``ZOOM_CLIENT_ID`` + ``ZOOM_CLIENT_SECRET``
    **Server-to-Server OAuth**, and the usual choice for an unattended
    workflow. The app acts as the *account*, not as a person.
``loom connect zoom``
    User-delegated OAuth, for a workflow acting as one person. The stored
    credential refreshes through the ordinary credential-store machinery.
``ZOOM_ACCESS_TOKEN``
    A token minted elsewhere. What a test uses, and the only form that needs
    no secret on the box.

Server-to-Server OAuth is unlike every other integration here in one way worth
naming: **there is no refresh token.** The client id and secret *are* the
durable credential, and a fresh access token is minted from them on demand.
So none of the credential-store refresh machinery applies to this path — this
module mints and caches its own hourly token, under a lock, the same shape
``GoogleAuth`` uses for a service account.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.toolsets.zoom.errors import ZoomAuthError, classify

__all__ = ["ZoomAuth", "ZoomCredentials", "get_default_auth", "reset_default_auth"]

TOKEN_URL = "https://zoom.us/oauth/token"

#: Remint this many seconds before expiry. Zoom's tokens last an hour; a token
#: that expires mid-flight fails a step that had no other reason to fail.
_SKEW = 60.0

#: The credential name a ``loom connect zoom`` login is stored under.
CREDENTIAL_NAME = "zoom"


@dataclass
class ZoomCredentials:
    """Whichever of the credential shapes the environment supplied."""

    account_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ZoomCredentials:
        source = os.environ if env is None else env
        return cls(
            account_id=source.get("ZOOM_ACCOUNT_ID", ""),
            client_id=source.get("ZOOM_CLIENT_ID", ""),
            client_secret=source.get("ZOOM_CLIENT_SECRET", ""),
            access_token=source.get("ZOOM_ACCESS_TOKEN", ""),
        )

    @property
    def mode(self) -> str:
        """Which flow these select, or ``""`` if none is complete.

        Server-to-Server wins over a ready-made access token when both are
        present, and for the same reason Google prefers a refresh token: an
        access token lives an hour, the client secret mints them indefinitely,
        and a workflow that sleeps outlives the access token by design.
        """
        if self.account_id and self.client_id and self.client_secret:
            return "server_to_server"
        if self.access_token:
            return "token"
        return ""


class ZoomAuth:
    """Mints and caches a Zoom access token."""

    def __init__(
        self,
        credentials: ZoomCredentials | None = None,
        *,
        credential_name: str = CREDENTIAL_NAME,
        transport: Any | None = None,
    ) -> None:
        self._credentials = credentials or ZoomCredentials.from_env()
        self._credential_name = credential_name
        self._transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

        # A run's CredentialStore may supply what the environment did not, but
        # that can only be checked with an await — so this raises only when
        # nothing could possibly save it.
        if not self._credentials.mode and current_credential_store() is None:
            raise ZoomAuthError(
                "No Zoom credentials found. Set ZOOM_ACCOUNT_ID + "
                "ZOOM_CLIENT_ID + ZOOM_CLIENT_SECRET for a Server-to-Server "
                "OAuth app, or ZOOM_ACCESS_TOKEN, or run 'loom connect zoom'."
            )


    @classmethod
    def from_values(
        cls, values: Mapping[str, str], *, scopes: Sequence[str] = ()
    ) -> ZoomAuth:
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
        return cls(credentials=ZoomCredentials.from_env(dict(values)))

    @property
    def mode(self) -> str:
        return self._credentials.mode

    async def token(self) -> str:
        """A valid access token, minting or reminting as needed.

        A run's connected credential is checked first, on every call and never
        cached here — the credential store owns its own expiry and renewal, and
        caching its answer on top would serve a token an hour after the store
        would have refreshed it.
        """
        connected = await resolve_bearer_token(self._credential_name)
        if connected:
            return connected

        if self._token and time.monotonic() < self._expires_at - _SKEW:
            return self._token

        async with self._lock:
            # Another caller may have minted while this one waited.
            if self._token and time.monotonic() < self._expires_at - _SKEW:
                return self._token
            return await self._mint()

    async def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.token()}",
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        """Drop the cached token, so the next call mints a fresh one.

        Called when Zoom rejects a token this cache still believed in — a
        revoked app, or a clock further out than the skew allows.
        """
        self._token = ""
        self._expires_at = 0.0

    async def _mint(self) -> str:
        mode = self._credentials.mode
        if not mode:
            raise ZoomAuthError(
                "No Zoom credentials found. Set ZOOM_ACCOUNT_ID + "
                "ZOOM_CLIENT_ID + ZOOM_CLIENT_SECRET, or ZOOM_ACCESS_TOKEN, or "
                f"connect a '{self._credential_name}' credential with "
                "'loom connect zoom'."
            )
        if mode == "token":
            # Supplied ready-made. Its lifetime is the caller's problem; treat
            # it as valid and let a 401 surface rather than guessing an expiry.
            self._token = self._credentials.access_token
            self._expires_at = float("inf")
            return self._token

        import httpx

        # The account-credentials grant: client id and secret go in a Basic
        # header, the account id in the query string. Putting the secret in the
        # body instead is a 400 that reads as a bad account id.
        basic = base64.b64encode(
            f"{self._credentials.client_id}:{self._credentials.client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.post(
                TOKEN_URL,
                params={
                    "grant_type": "account_credentials",
                    "account_id": self._credentials.account_id,
                },
                headers={"Authorization": f"Basic {basic}"},
            )
        if response.status_code >= 400:
            raise classify(response.status_code, _safe_json(response), TOKEN_URL)

        data = response.json()
        token = str(data.get("access_token", ""))
        if not token:
            raise ZoomAuthError(f"Zoom token endpoint returned no access_token: {data}")

        self._token = token
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return token


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default: ZoomAuth | None = None


def get_default_auth() -> ZoomAuth:
    """Return the process-wide :class:`ZoomAuth`, building it on first use."""
    global _default
    if _default is None:
        _default = ZoomAuth()
    return _default


def reset_default_auth() -> None:
    """Drop the process-wide auth. For tests, and for a credential rotation."""
    global _default
    _default = None
