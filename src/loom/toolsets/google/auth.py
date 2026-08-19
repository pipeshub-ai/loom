"""Google OAuth2 credentials, resolved from the environment.

Three sources, tried in order, because the right one depends on where the
workflow runs and none of them covers every case:

``GOOGLE_ACCESS_TOKEN``
    A token someone else minted. What a test uses, what a gateway hands a
    worker, and the only form that needs no client secret on the box.
``GOOGLE_CLIENT_ID`` + ``GOOGLE_CLIENT_SECRET`` + ``GOOGLE_REFRESH_TOKEN``
    The installed-app flow. Long-lived, self-renewing, and the usual choice for
    a workflow acting as one person. Needs nothing but httpx.
``GOOGLE_SERVICE_ACCOUNT_FILE`` (+ ``GOOGLE_IMPERSONATE_SUBJECT``)
    Workspace domain-wide delegation, for acting as any user in a domain.
    Signing a JWT needs a crypto library, so this one — and only this one —
    requires ``pip install 'loomsdk[google]'``.

Tokens are cached until shortly before they expire and refreshed under a lock,
so a workflow that fans out ten Gmail steps mints one token, not ten.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.toolsets.google.errors import GoogleAuthError, classify

__all__ = ["GoogleAuth", "GoogleCredentials", "get_default_auth", "reset_default_auth"]

TOKEN_URL = "https://oauth2.googleapis.com/token"
_JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Refresh this many seconds before expiry. A token that expires mid-flight
#: fails a step that had no other reason to fail.
_SKEW = 60.0


@dataclass
class GoogleCredentials:
    """Whichever of the three credential shapes the environment supplied."""

    access_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    service_account_file: str = ""
    subject: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GoogleCredentials:
        source = os.environ if env is None else env
        return cls(
            access_token=source.get("GOOGLE_ACCESS_TOKEN", ""),
            client_id=source.get("GOOGLE_CLIENT_ID", ""),
            client_secret=source.get("GOOGLE_CLIENT_SECRET", ""),
            refresh_token=source.get("GOOGLE_REFRESH_TOKEN", ""),
            service_account_file=source.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
            subject=source.get("GOOGLE_IMPERSONATE_SUBJECT", ""),
        )

    @property
    def mode(self) -> str:
        """Which flow these credentials select, or ``""`` if none is complete.

        The refresh token wins over a ready-made access token when both are
        present, which is the opposite of the obvious order. An access token
        lives about an hour; a refresh token mints fresh ones indefinitely. So
        the moment both are configured — which is exactly what the setup helper
        prints — preferring the access token means everything works until it
        silently does not, and the durable credential sitting right beside it
        never gets used. A workflow that sleeps outlives the access token by
        design.
        """
        if self.client_id and self.client_secret and self.refresh_token:
            return "refresh_token"
        if self.access_token:
            return "token"
        if self.service_account_file:
            return "service_account"
        return ""


class GoogleAuth:
    """Mints and caches an access token for the Google REST APIs."""

    def __init__(
        self,
        credentials: GoogleCredentials | None = None,
        *,
        scopes: list[str] | None = None,
        credential_name: str = "google",
    ) -> None:
        self._credentials = credentials or GoogleCredentials.from_env()
        # Copied, not aliased: callers pass their module-level ``SCOPES``
        # constant, and add_scopes() extends this list — so sharing it would
        # let one toolset's scopes accumulate onto another toolset's constant
        # for the life of the process.
        self._scopes = list(scopes or [])
        self._credential_name = credential_name
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

        # A CredentialStore bound to the current run might supply what the
        # environment did not — but that can only be checked with an await,
        # so this raises only when nothing could possibly save it: no local
        # credentials *and* no store bound at all. A store that turns out to
        # have nothing under `credential_name` still surfaces this same
        # error, just later, from _mint() at first actual use.
        if not self._credentials.mode and current_credential_store() is None:
            raise GoogleAuthError(
                "No Google credentials found. Set GOOGLE_ACCESS_TOKEN, or "
                "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN, "
                "or GOOGLE_SERVICE_ACCOUNT_FILE."
            )

    @property
    def mode(self) -> str:
        return self._credentials.mode

    @property
    def scopes(self) -> list[str]:
        return list(self._scopes)

    def add_scopes(self, scopes: list[str] | None) -> None:
        """Widen this auth's scope set, dropping a token minted without them.

        One :class:`GoogleAuth` serves every Google toolset in the process, and
        it is built by whichever one is called first. For a service account the
        scopes are *in the signed assertion*, so without this the second toolset
        to be used gets a token carrying only the first one's scopes — a Drive
        call authenticated for Gmail, which fails 403 ``insufficient scopes``
        and reads as a misconfigured credential rather than a shared cache.

        Invalidates the cached token only when something was actually added, so
        the common case — every toolset asking for what it already has — does
        not remint on each call.
        """
        fresh = [scope for scope in scopes or [] if scope not in self._scopes]
        if not fresh:
            return
        self._scopes.extend(fresh)
        # Only the service-account flow bakes scopes into the token it mints;
        # the other two mint against whatever the grant carries, so throwing
        # away a live token there would buy nothing.
        if self._credentials.mode == "service_account":
            self.invalidate()

    async def token(self) -> str:
        """Return a valid access token, minting or refreshing if needed.

        A run's bound ``CredentialStore`` is checked first, on every call —
        never cached here, since the store already manages its own expiry
        and refresh (see ``BaseCredentialStore.get``) and caching its answer
        on top would mean this class serves a token an hour after the store
        itself would have refreshed it. Falls through to the environment
        credentials below when no store is bound, or one is bound but has
        nothing under ``credential_name`` — both exactly today's behaviour.
        """
        token = await resolve_bearer_token(self._credential_name)
        if token:
            return token

        if self._token and time.monotonic() < self._expires_at - _SKEW:
            return self._token

        async with self._lock:
            # Another caller may have refreshed while this one waited.
            if self._token and time.monotonic() < self._expires_at - _SKEW:
                return self._token
            return await self._mint()

    async def headers(self) -> dict[str, str]:
        """Authorization headers for an API request."""
        return {
            "Authorization": f"Bearer {await self.token()}",
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        """Drop the cached token, so the next call mints a fresh one.

        Called when the API rejects a token the cache still believed in — a
        revoked grant, or a clock further out than the skew allows.
        """
        self._token = ""
        self._expires_at = 0.0

    # -- minting -------------------------------------------------------------

    async def _mint(self) -> str:
        mode = self._credentials.mode
        if not mode:
            raise GoogleAuthError(
                "No Google credentials found. Set GOOGLE_ACCESS_TOKEN, or "
                "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN, "
                "or GOOGLE_SERVICE_ACCOUNT_FILE, or connect a "
                f"'{self._credential_name}' credential via a CredentialStore."
            )
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
            }
        else:
            payload = {"grant_type": _JWT_GRANT, "assertion": self._signed_assertion()}

        data = await self._post_token(payload)
        token = str(data.get("access_token", ""))
        if not token:
            raise GoogleAuthError(f"Token endpoint returned no access_token: {data}")

        self._token = token
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return token

    async def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(TOKEN_URL, data=payload)
        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise classify(response.status_code, body, TOKEN_URL) from None
        result: dict[str, Any] = response.json()
        return result

    def _signed_assertion(self) -> str:
        """Build the signed JWT that buys a token for a service account."""
        try:
            from google.auth import jwt as google_jwt
        except ImportError:
            raise GoogleAuthError(
                "Service-account credentials need the google extra: "
                "pip install 'loomsdk[google]'. Alternatively use "
                "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN, "
                "which needs no extra."
            ) from None

        try:
            with open(self._credentials.service_account_file) as handle:
                info = json.load(handle)
        except OSError as exc:
            raise GoogleAuthError(f"Cannot read service account file: {exc}") from exc
        except ValueError as exc:
            raise GoogleAuthError(f"Service account file is not JSON: {exc}") from exc

        now = int(time.time())
        claims = {
            "iss": info.get("client_email", ""),
            "scope": " ".join(self._scopes),
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        if self._credentials.subject:
            claims["sub"] = self._credentials.subject

        signer = google_jwt.Credentials.from_service_account_info(info)._signer
        return str(google_jwt.encode(signer, claims).decode())


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default: GoogleAuth | None = None


def get_default_auth(scopes: list[str] | None = None) -> GoogleAuth:
    """Return the process-wide :class:`GoogleAuth`, building it on first use.

    Shared between Gmail, Calendar, Drive and Meet deliberately: they
    authenticate against the same account, and one cached token serves all four.

    A later caller's scopes are *added* rather than ignored — see
    :meth:`GoogleAuth.add_scopes`. Ignoring them is the shape of bug that only
    appears in the service-account deployment and only for whichever toolset
    happened to be used second.
    """
    global _default
    if _default is None:
        _default = GoogleAuth(scopes=scopes)
    else:
        _default.add_scopes(scopes)
    return _default


def reset_default_auth() -> None:
    """Drop the process-wide auth. For tests, and for a credential rotation."""
    global _default
    _default = None
