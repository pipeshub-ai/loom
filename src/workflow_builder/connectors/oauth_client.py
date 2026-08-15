"""An OAuth 2.1 client: PKCE and device-code flows, and how expiry gets renewed.

Two authorization flows, both public-client shaped (no client secret required,
though one may be supplied for a confidential client):

``authorization_url()`` / ``exchange_code()``
    The PKCE authorization-code flow — a browser redirect, a code, an
    exchange. What ``loom login`` uses when a browser is available.
``start_device_authorization()`` / ``poll_device_token()``
    RFC 8628 device authorization — a code the user types into another
    device. What ``loom login --device`` uses, and what headless
    environments fall back to automatically.

``OAuthClient`` also implements :class:`~workflow_builder.connectors.credentials.Refresher`,
which is what makes it the thing a ``CredentialStore`` calls when a stored
credential has expired. That path is the one worth reading carefully:

- **Single-flight across processes.** Refresh tokens are commonly rotated on
  use — the old one stops working the instant the new one is issued — so two
  processes refreshing the same credential at once means one wins and the
  other's request is rejected with ``invalid_grant``, intermittently, only
  under load. A :class:`~workflow_builder.state.base.LockProvider` lease
  serializes refresh attempts across processes; a loser re-reads what the
  winner wrote (via the store's ``peek()``) instead of racing the token
  endpoint itself.
- **Write-before-discard.** The new credential is *returned* to the caller
  (a ``CredentialStore``, which persists it immediately) before this client
  does anything else with it. There is no step here that discards the old
  refresh token before the new one is durable — the two are never both gone
  at once.
- **``AuthExpired``, not a raised network error.** A refresh that cannot
  succeed without a human (denied, expired, no refresh token at all) raises
  ``AuthExpired`` so the run parks — see ``BaseCredentialStore.get()``.

Wiring a refresher into a store (they need each other, so one is built first
and connected to the second)::

    store = KeyringCredentialStore()
    client = OAuthClient(
        client_id="...", authorization_endpoint="...", token_endpoint="...",
        lock=store_that_provides_locks, owner=node_id, store=store,
    )
    store._refresher = client  # doc'd as the intended wiring, not a hack
"""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

from workflow_builder.connectors.credentials import StoredCredential
from workflow_builder.core.exceptions import AuthExpired, ConfigurationError
from workflow_builder.core.secret import Secret
from workflow_builder.runtime.clock import Clock, SystemClock
from workflow_builder.state.base import LockProvider

__all__ = [
    "DeviceAuthorization",
    "MetadataRefresher",
    "OAuthClient",
    "OAuthTokenError",
    "generate_pkce_pair",
]

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class _StoreLike(Protocol):
    """The two store operations a cross-process refresh needs.

    Narrower than :class:`~workflow_builder.connectors.credentials.CredentialStore`
    on purpose — an ``OAuthClient`` never calls ``get()`` (that would recurse
    back into itself as the configured refresher) or ``forget()``. Any
    ``BaseCredentialStore`` subclass satisfies this structurally, with no
    inheritance relationship needed.
    """

    async def peek(self, name: str) -> StoredCredential | None: ...
    async def put(self, name: str, credential: StoredCredential) -> None: ...


class OAuthTokenError(Exception):
    """The token endpoint returned an OAuth error response.

    Carries the ``error`` code (``invalid_grant``, ``authorization_pending``,
    ``slow_down``, ...) so callers can branch on it per RFC 6749 §5.2 / RFC
    8628 §3.5 rather than parsing a message string.
    """

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description


@dataclass(frozen=True)
class DeviceAuthorization:
    """The response from a device authorization request (RFC 8628 §3.2).

    ``expires_in`` is relative to ``issued_at``, per the RFC — not to
    whenever :meth:`OAuthClient.poll_device_token` happens to be called.
    A caller that shows the user a code and waits for them to switch
    devices, type it in, and approve routinely takes longer to *start*
    polling than the whole window if the deadline were measured from
    poll time instead.
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    issued_at: datetime
    interval: int = 5
    verification_uri_complete: str | None = None


def generate_pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair, S256 (RFC 7636).

    ``secrets.token_urlsafe`` already produces characters from PKCE's
    allowed set (``[A-Za-z0-9\\-._~]`` minus ``.``/``~``, both optional per
    the RFC), so no extra encoding step is needed for the verifier itself.
    """
    verifier = secrets.token_urlsafe(40)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OAuthClient:
    """PKCE + device-code flows against one authorization server.

    *store* and *lock* are both optional and both needed for cross-process
    single-flight refresh:

    - Without *lock*, :meth:`refresh` still works (a single embedded process
      refreshing its own credential), but two processes sharing a store can
      both hit the token endpoint at once.
    - Without *store*, a refresh that loses the lock race falls back to
      waiting once and retrying rather than re-reading — correct, but a
      wasted round trip when another process already refreshed. Without it,
      the freshly minted credential is also never persisted until the
      caller (typically ``BaseCredentialStore.get()``) writes it back
      itself — with it, the write happens *before* this client releases the
      lock, so a second process racing to acquire it right after release is
      guaranteed to see the fresh value rather than a stale one.
    """

    def __init__(
        self,
        *,
        client_id: str,
        authorization_endpoint: str = "",
        token_endpoint: str,
        redirect_uri: str = "",
        client_secret: str | None = None,
        scopes: tuple[str, ...] = (),
        device_authorization_endpoint: str | None = None,
        lock: LockProvider | None = None,
        store: _StoreLike | None = None,
        owner: str = "",
        lease_ttl: float = 30.0,
        poll_interval: float = 1.0,
        clock: Clock | None = None,
        pkce: bool = True,
    ) -> None:
        self._client_id = client_id
        self._authorization_endpoint = authorization_endpoint
        self._token_endpoint = token_endpoint
        self._redirect_uri = redirect_uri
        self._client_secret = client_secret
        self._scopes = scopes
        self._device_authorization_endpoint = device_authorization_endpoint
        self._lock = lock
        self._store = store
        self._owner = owner or f"oauth-client-{secrets.token_hex(4)}"
        self._lease_ttl = lease_ttl
        self._poll_interval = poll_interval
        self.clock = clock or SystemClock()
        self._pkce = pkce

    # -- PKCE authorization-code flow ----------------------------------------

    def authorization_url(
        self,
        *,
        state: str,
        code_challenge: str = "",
        extra_params: dict[str, str] | None = None,
    ) -> str:
        """The URL to send a browser to. Pair with :func:`generate_pkce_pair`."""
        if not self._authorization_endpoint:
            raise ConfigurationError(
                "this OAuthClient has no authorization_endpoint configured"
            )
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        if self._pkce:
            if not code_challenge:
                raise ConfigurationError(
                    "PKCE is enabled on this OAuthClient; pass code_challenge"
                )
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if self._scopes:
            params["scope"] = " ".join(self._scopes)
        if extra_params:
            params.update(extra_params)
        return f"{self._authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, code: str, *, code_verifier: str = "") -> StoredCredential:
        """Trade an authorization code (plus its PKCE verifier) for tokens."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
        }
        if self._pkce:
            if not code_verifier:
                raise ConfigurationError(
                    "PKCE is enabled on this OAuthClient; pass code_verifier"
                )
            payload["code_verifier"] = code_verifier
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        data = await self._post_token(payload)
        return self._to_stored_credential(data)

    # -- Device-code flow (RFC 8628) -----------------------------------------

    async def start_device_authorization(self) -> DeviceAuthorization:
        """Begin the device flow. Show the user ``verification_uri``/``user_code``."""
        if not self._device_authorization_endpoint:
            raise ConfigurationError(
                "this OAuthClient has no device_authorization_endpoint configured"
            )
        payload = {"client_id": self._client_id}
        if self._scopes:
            payload["scope"] = " ".join(self._scopes)
        data = await self._post(self._device_authorization_endpoint, payload)
        return DeviceAuthorization(
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(data.get("verification_uri", data.get("verification_url", ""))),
            verification_uri_complete=(
                str(data["verification_uri_complete"])
                if data.get("verification_uri_complete")
                else None
            ),
            expires_in=int(data.get("expires_in", 1800)),
            issued_at=self.clock.now(),
            interval=int(data.get("interval", 5)),
        )

    async def poll_device_token(self, device: DeviceAuthorization) -> StoredCredential:
        """Poll the token endpoint until the user approves, denies, or it expires.

        Honors ``slow_down`` by backing off, and ``authorization_pending`` by
        continuing to wait — both routine, not failures. Anything else
        (``access_denied``, ``expired_token``) raises :class:`AuthExpired`:
        a device flow that did not complete needs a human, same as any other
        credential a run cannot renew on its own.
        """
        deadline = device.issued_at + timedelta(seconds=device.expires_in)
        interval = float(device.interval)
        while True:
            if self.clock.now() >= deadline:
                raise AuthExpired(
                    "device authorization expired before the user approved it"
                )
            await self.clock.sleep(interval)
            payload = {
                "grant_type": _DEVICE_GRANT,
                "device_code": device.device_code,
                "client_id": self._client_id,
            }
            try:
                data = await self._post_token(payload)
            except OAuthTokenError as exc:
                if exc.error == "authorization_pending":
                    continue
                if exc.error == "slow_down":
                    interval += 5.0
                    continue
                if exc.error == "access_denied":
                    raise AuthExpired(
                        "the user denied the authorization request"
                    ) from exc
                if exc.error == "expired_token":
                    raise AuthExpired(
                        "device authorization expired before the user approved it"
                    ) from exc
                raise
            return self._to_stored_credential(data)

    # -- Refresher protocol ---------------------------------------------------

    async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
        """Renew *stored*. Implements
        :class:`~workflow_builder.connectors.credentials.Refresher`."""
        if stored.refresh_token is None:
            raise AuthExpired(
                f"credential '{name}' has no refresh token and cannot renew "
                f"itself. Run 'loom connect {name}' to reauthorize.",
                name=name,
            )

        if self._lock is None:
            return await self._refresh_via_token_endpoint(name, stored)
        return await self._refresh_with_lock(name, stored)

    async def _refresh_with_lock(
        self, name: str, stored: StoredCredential
    ) -> StoredCredential:
        """Refresh under a cross-process lease, or wait out whoever holds it.

        A loser does not retry the token endpoint — it polls the *store* for
        the winner's write, re-attempting the lock between polls so a holder
        whose lease has quietly expired (crashed mid-refresh) gets taken over
        rather than wedging every other caller until ``_lease_ttl`` seconds
        pass. Bounded by ``_lease_ttl`` overall: a lease cannot outlive its
        own TTL, so waiting longer than that for it to clear would be waiting
        for something that is never coming.
        """
        assert self._lock is not None
        lock_key = f"credential-refresh:{name}"
        deadline = self.clock.now() + timedelta(seconds=self._lease_ttl)

        while True:
            acquired = await self._lock.acquire(lock_key, self._owner, self._lease_ttl)
            if acquired:
                try:
                    # The lock may have been free because whoever held it
                    # already finished, not because no one ever raced us —
                    # check the store before spending a token request on a
                    # refresh token that a finished holder already rotated
                    # away. `stored` was only ever a snapshot from before
                    # this call started.
                    current = await self._peek_if_fresh(name)
                    if current is not None:
                        return current
                    fresh = await self._refresh_via_token_endpoint(name, stored)
                    if self._store is not None:
                        # Written before release, not after — so a process
                        # that acquires the lock the instant it frees never
                        # observes a "free lock, stale store" gap.
                        await self._store.put(name, fresh)
                    return fresh
                finally:
                    await self._lock.release(lock_key, self._owner)

            winners_write = await self._peek_if_fresh(name)
            if winners_write is not None:
                return winners_write
            if self.clock.now() >= deadline:
                raise AuthExpired(
                    f"credential '{name}' is being refreshed by another "
                    "process and did not become ready in time. It should "
                    "resolve on the next attempt if that refresh succeeds.",
                    name=name,
                )
            await self.clock.sleep(self._poll_interval)

    async def _peek_if_fresh(self, name: str) -> StoredCredential | None:
        """The stored record via ``store.peek()`` if it is present and not
        expired — never :meth:`CredentialStore.get`, which would recurse
        back into this same refresher if still expired."""
        if self._store is None:
            return None
        current = await self._store.peek(name)
        if current is not None and not current.is_expired(self.clock):
            return current
        return None

    async def _await_and_peek(self, name: str) -> StoredCredential | None:
        """Wait one beat for a concurrent refresher, then check its result."""
        await self.clock.sleep(self._poll_interval)
        return await self._peek_if_fresh(name)

    async def _refresh_via_token_endpoint(
        self, name: str, stored: StoredCredential
    ) -> StoredCredential:
        assert stored.refresh_token is not None
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": stored.refresh_token.reveal(),
            "client_id": self._client_id,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        try:
            data = await self._post_token(payload)
        except OAuthTokenError as exc:
            if exc.error != "invalid_grant":
                raise
            # The refresh token we had may already have been rotated away by
            # a refresh that completed elsewhere between us reading `stored`
            # and reaching the token endpoint. One re-read, then give up
            # cleanly rather than retrying a grant that is not coming back.
            fresh = await self._await_and_peek(name)
            if fresh is not None:
                return fresh
            raise AuthExpired(
                f"credential '{name}' could not be refreshed (its refresh "
                f"token was rejected). Run 'loom connect {name}' to "
                "reauthorize.",
                name=name,
            ) from exc

        return self._to_stored_credential(data, previous=stored)

    # -- wire protocol ----------------------------------------------------

    async def _post(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url, data=payload, headers={"Accept": "application/json"}
            )
        if response.status_code >= 400:
            body = _safe_json(response)
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
            description = (
                str(body.get("error_description", ""))
                if isinstance(body, dict)
                else response.text
            )
            raise OAuthTokenError(error or f"http_{response.status_code}", description)
        result: dict[str, Any] = response.json()
        return result

    async def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        return await self._post(self._token_endpoint, payload)

    def _to_stored_credential(
        self, data: dict[str, Any], *, previous: StoredCredential | None = None
    ) -> StoredCredential:
        access_token = data.get("access_token")
        if not access_token:
            raise ConfigurationError(f"token endpoint returned no access_token: {data}")

        refresh_token = data.get("refresh_token")
        if refresh_token:
            refresh_token_secret: Secret[str] | None = Secret(str(refresh_token))
        elif previous is not None:
            # Not every server rotates the refresh token on every use; keep
            # the one we already have rather than losing it.
            refresh_token_secret = previous.refresh_token
        else:
            refresh_token_secret = None

        expires_in = data.get("expires_in")
        expires_at = (
            self.clock.now() + timedelta(seconds=int(expires_in))
            if expires_in is not None
            else None
        )

        scope = data.get("scope")
        scopes = (
            frozenset(str(scope).split())
            if scope
            else (previous.scopes if previous is not None else frozenset())
        )

        return StoredCredential(
            token=Secret(str(access_token)),
            refresh_token=refresh_token_secret,
            expires_at=expires_at,
            scopes=scopes,
            token_type=str(data.get("token_type", "bearer")),
            metadata=dict(previous.metadata) if previous is not None else {},
        )

    def __repr__(self) -> str:
        return f"<OAuthClient client_id={self._client_id!r}>"


class MetadataRefresher:
    """One :class:`~workflow_builder.connectors.credentials.Refresher` for
    every credential in a store, not one per authorization server.

    A :class:`BaseCredentialStore` is configured with a single ``refresher``,
    but a real store holds credentials from many authorization servers —
    ``loom login``'s own token plus one per ``loom connect``ed toolset — each
    with its own token endpoint and client id. Rather than a refresher per
    name, this reads that configuration back out of ``stored.metadata``,
    which whoever first minted the credential (``cli/auth_commands.py``) is
    expected to have populated with ``token_endpoint``, ``client_id``, and
    optionally ``client_secret``/``device_authorization_endpoint`` before the
    initial :meth:`~workflow_builder.connectors.credentials.CredentialStore.put`.

    A credential with no such metadata (hand-inserted, or from a version
    that predates this) raises :class:`AuthExpired` naming the fix, same as
    a store with no refresher at all — there is nothing to guess here that
    would not be a security-relevant assumption.
    """

    def __init__(
        self,
        *,
        store: _StoreLike | None = None,
        lock: LockProvider | None = None,
        owner: str = "",
        lease_ttl: float = 30.0,
        poll_interval: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._lock = lock
        self._owner = owner
        self._lease_ttl = lease_ttl
        self._poll_interval = poll_interval
        self._clock = clock

    async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
        meta = stored.metadata
        token_endpoint = meta.get("token_endpoint")
        client_id = meta.get("client_id")
        if not token_endpoint or not client_id:
            raise AuthExpired(
                f"credential '{name}' has expired and carries no endpoint "
                f"metadata to refresh itself with. Run 'loom connect {name}' "
                "to reauthorize.",
                name=name,
            )
        client = OAuthClient(
            client_id=str(client_id),
            client_secret=meta.get("client_secret"),
            token_endpoint=str(token_endpoint),
            device_authorization_endpoint=meta.get("device_authorization_endpoint"),
            lock=self._lock,
            store=self._store,
            owner=self._owner,
            lease_ttl=self._lease_ttl,
            poll_interval=self._poll_interval,
            clock=self._clock,
        )
        return await client.refresh(name, stored)

    def __repr__(self) -> str:
        return "<MetadataRefresher>"


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
