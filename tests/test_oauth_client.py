"""``OAuthClient``: PKCE and device-code flows, and cross-process refresh.

Runs against a fake authorization server driven through ``httpx.MockTransport``
— the same technique ``test_google_toolsets.py`` uses for the token endpoint —
so a flow is executed end-to-end (real request encoding, real response
parsing) rather than mocked at ``OAuthClient``'s own boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from workflow_builder.connectors.credentials import MemoryCredentialStore, StoredCredential
from workflow_builder.connectors.oauth_client import (
    OAuthClient,
    OAuthTokenError,
    generate_pkce_pair,
)
from workflow_builder.core.exceptions import AuthExpired, ConfigurationError
from workflow_builder.core.secret import Secret
from workflow_builder.runtime.clock import ManualClock
from workflow_builder.state.memory import MemoryStore

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class FakeAuthServer:
    """A minimal, stateful OAuth 2.1 authorization server.

    Deliberately implements the two behaviours that make this plan's refresh
    story worth testing: refresh tokens **rotate** (the old one dies the
    instant a new one is issued) and a device code stays ``authorization_pending``
    until explicitly approved.
    """

    def __init__(self) -> None:
        self.token_calls = 0
        self._codes: dict[str, dict[str, Any]] = {}
        self._refresh_tokens: dict[str, bool] = {}
        self._devices: dict[str, dict[str, Any]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def issue_code(self, code: str, *, code_challenge: str | None = None) -> None:
        self._codes[code] = {"used": False, "challenge": code_challenge}

    def approve_device(self, device_code: str) -> None:
        self._devices[device_code]["approved"] = True

    def revoke(self, refresh_token: str) -> None:
        self._refresh_tokens[refresh_token] = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/device_authorization"):
            return self._device_authorization()
        self.token_calls += 1
        return self._token(form)

    def _device_authorization(self) -> httpx.Response:
        device_code = f"device-{secrets.token_hex(4)}"
        self._devices[device_code] = {"approved": False}
        return httpx.Response(
            200,
            json={
                "device_code": device_code,
                "user_code": secrets.token_hex(3).upper(),
                "verification_uri": "https://auth.test/device",
                "expires_in": 60,
                "interval": 0,
            },
        )

    def _token(self, form: dict[str, str]) -> httpx.Response:
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return self._exchange_code(form)
        if grant_type == "refresh_token":
            return self._exchange_refresh(form)
        if grant_type == _DEVICE_GRANT:
            return self._exchange_device(form)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _exchange_code(self, form: dict[str, str]) -> httpx.Response:
        entry = self._codes.get(form.get("code", ""))
        if entry is None or entry["used"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if entry["challenge"]:
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if challenge != entry["challenge"]:
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "bad verifier"},
                )
        entry["used"] = True
        return self._issue_tokens(scope="jira.issues:read")

    def _exchange_refresh(self, form: dict[str, str]) -> httpx.Response:
        token = form.get("refresh_token", "")
        if not self._refresh_tokens.get(token, False):
            return httpx.Response(400, json={"error": "invalid_grant"})
        self._refresh_tokens[token] = False
        return self._issue_tokens()

    def _exchange_device(self, form: dict[str, str]) -> httpx.Response:
        entry = self._devices.get(form.get("device_code", ""))
        if entry is None:
            return httpx.Response(400, json={"error": "expired_token"})
        if not entry["approved"]:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return self._issue_tokens()

    def _issue_tokens(
        self, *, scope: str | None = None, rotate_refresh: bool = True
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "access_token": f"access-{secrets.token_hex(6)}",
            "expires_in": 3600,
            "token_type": "bearer",
        }
        if rotate_refresh:
            refresh_token = f"refresh-{secrets.token_hex(6)}"
            self._refresh_tokens[refresh_token] = True
            payload["refresh_token"] = refresh_token
        if scope:
            payload["scope"] = scope
        return httpx.Response(200, json=payload)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Force every ``httpx.AsyncClient`` in this test to use *transport*."""
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs.setdefault("transport", transport)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _client(**overrides: Any) -> OAuthClient:
    defaults: dict[str, Any] = {
        "client_id": "loom-cli",
        "authorization_endpoint": "https://auth.test/authorize",
        "token_endpoint": "https://auth.test/token",
        "redirect_uri": "http://localhost:8765/callback",
        "device_authorization_endpoint": "https://auth.test/device_authorization",
    }
    defaults.update(overrides)
    return OAuthClient(**defaults)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeAuthServer:
    fake = FakeAuthServer()
    _patch_transport(monkeypatch, fake.transport())
    return fake


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_challenge_is_the_s256_hash_of_the_verifier(self) -> None:
        verifier, challenge = generate_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_each_pair_is_fresh(self) -> None:
        first = generate_pkce_pair()
        second = generate_pkce_pair()
        assert first != second

    def test_verifier_and_challenge_are_url_safe(self) -> None:
        verifier, challenge = generate_pkce_pair()
        for value in (verifier, challenge):
            assert "=" not in value
            assert "+" not in value
            assert "/" not in value


class TestAuthorizationUrl:
    def test_includes_pkce_state_and_client_id(self) -> None:
        client = _client(scopes=("jira.issues:read",))
        url = client.authorization_url(state="xyz", code_challenge="chal123")
        assert url.startswith("https://auth.test/authorize?")
        assert "client_id=loom-cli" in url
        assert "state=xyz" in url
        assert "code_challenge=chal123" in url
        assert "code_challenge_method=S256" in url
        assert "response_type=code" in url
        assert "scope=jira.issues%3Aread" in url

    def test_without_an_authorization_endpoint_raises_configuration_error(self) -> None:
        client = _client(authorization_endpoint="")
        with pytest.raises(ConfigurationError):
            client.authorization_url(state="s", code_challenge="c")


# ---------------------------------------------------------------------------
# Authorization-code (PKCE) flow
# ---------------------------------------------------------------------------


class TestAuthorizationCodeFlow:
    async def test_exchange_code_returns_a_stored_credential(
        self, server: FakeAuthServer
    ) -> None:
        verifier, challenge = generate_pkce_pair()
        server.issue_code("good-code", code_challenge=challenge)
        client = _client()

        cred = await client.exchange_code("good-code", code_verifier=verifier)

        assert isinstance(cred, StoredCredential)
        assert isinstance(cred.token, Secret)
        assert cred.token.reveal().startswith("access-")
        assert cred.refresh_token is not None
        assert cred.scopes == {"jira.issues:read"}
        assert cred.expires_at is not None

    async def test_wrong_verifier_is_rejected(self, server: FakeAuthServer) -> None:
        _, challenge = generate_pkce_pair()
        server.issue_code("good-code", code_challenge=challenge)
        client = _client()

        with pytest.raises(OAuthTokenError) as caught:
            await client.exchange_code("good-code", code_verifier="wrong-verifier")
        assert caught.value.error == "invalid_grant"

    async def test_a_code_cannot_be_redeemed_twice(self, server: FakeAuthServer) -> None:
        verifier, challenge = generate_pkce_pair()
        server.issue_code("one-shot", code_challenge=challenge)
        client = _client()

        await client.exchange_code("one-shot", code_verifier=verifier)
        with pytest.raises(OAuthTokenError):
            await client.exchange_code("one-shot", code_verifier=verifier)


# ---------------------------------------------------------------------------
# Device-code flow (RFC 8628)
# ---------------------------------------------------------------------------


class TestDeviceFlow:
    async def test_start_returns_a_verification_uri_and_user_code(
        self, server: FakeAuthServer
    ) -> None:
        client = _client()
        device = await client.start_device_authorization()
        assert device.verification_uri == "https://auth.test/device"
        assert device.device_code
        assert device.user_code

    async def test_without_a_device_endpoint_raises_configuration_error(self) -> None:
        client = _client(device_authorization_endpoint=None)
        with pytest.raises(ConfigurationError):
            await client.start_device_authorization()

    async def test_polling_waits_through_authorization_pending_then_succeeds(
        self, server: FakeAuthServer
    ) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        client = _client(clock=clock)
        device = await client.start_device_authorization()

        async def approve_shortly() -> None:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            server.approve_device(device.device_code)

        approver = asyncio.ensure_future(approve_shortly())
        cred = await client.poll_device_token(device)
        await approver

        assert cred.token.reveal().startswith("access-")
        assert len(clock.slept) >= 1

    async def test_denied_authorization_raises_auth_expired(
        self, server: FakeAuthServer
    ) -> None:
        client = _client(clock=ManualClock(datetime(2030, 1, 1, tzinfo=UTC)))
        device = await client.start_device_authorization()

        # Denial is modeled as the device entry vanishing server-side is not
        # how real servers behave; a real server returns access_denied. Patch
        # the server to do that for this one poll.
        original = server._exchange_device

        def denied(form: dict[str, str]) -> httpx.Response:
            return httpx.Response(400, json={"error": "access_denied"})

        server._exchange_device = denied  # type: ignore[method-assign]
        with pytest.raises(AuthExpired):
            await client.poll_device_token(device)
        server._exchange_device = original  # type: ignore[method-assign]

    async def test_expiry_before_approval_raises_auth_expired(
        self, server: FakeAuthServer
    ) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        client = _client(clock=clock)
        device = await client.start_device_authorization()
        clock.advance(seconds=device.expires_in + 1)

        with pytest.raises(AuthExpired):
            await client.poll_device_token(device)


# ---------------------------------------------------------------------------
# Refresher protocol
# ---------------------------------------------------------------------------


def _stored(*, refresh_token: str | None = "refresh-1") -> StoredCredential:
    return StoredCredential(
        token=Secret("stale-token"),
        refresh_token=Secret(refresh_token) if refresh_token else None,
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


class TestRefresh:
    async def test_no_refresh_token_raises_auth_expired(self) -> None:
        client = _client()
        with pytest.raises(AuthExpired, match="loom connect"):
            await client.refresh("jira", _stored(refresh_token=None))

    async def test_refresh_returns_a_new_token_and_rotates_the_refresh_token(
        self, server: FakeAuthServer
    ) -> None:
        server._refresh_tokens["refresh-1"] = True
        client = _client()

        fresh = await client.refresh("jira", _stored(refresh_token="refresh-1"))

        assert fresh.token.reveal().startswith("access-")
        assert fresh.refresh_token is not None
        assert fresh.refresh_token.reveal() != "refresh-1"

    async def test_a_server_that_does_not_rotate_keeps_the_old_refresh_token(
        self, server: FakeAuthServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server._refresh_tokens["refresh-1"] = True
        monkeypatch.setattr(
            server,
            "_exchange_refresh",
            lambda form: server._issue_tokens(rotate_refresh=False)
            if server._refresh_tokens.get(form.get("refresh_token", ""))
            else httpx.Response(400, json={"error": "invalid_grant"}),
        )
        client = _client()

        fresh = await client.refresh("jira", _stored(refresh_token="refresh-1"))

        assert fresh.refresh_token is not None
        assert fresh.refresh_token.reveal() == "refresh-1"

    async def test_an_already_rotated_refresh_token_with_no_lock_raises_auth_expired(
        self, server: FakeAuthServer
    ) -> None:
        # Never registered on the server -> always invalid_grant.
        client = _client()
        with pytest.raises(AuthExpired, match="loom connect"):
            await client.refresh("jira", _stored(refresh_token="never-issued"))

    async def test_scopes_are_kept_when_the_server_omits_scope_on_refresh(
        self, server: FakeAuthServer
    ) -> None:
        server._refresh_tokens["refresh-1"] = True
        client = _client()
        stored = StoredCredential(
            token=Secret("stale"),
            refresh_token=Secret("refresh-1"),
            scopes=frozenset({"jira.issues:read"}),
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        fresh = await client.refresh("jira", stored)

        assert fresh.scopes == {"jira.issues:read"}


# ---------------------------------------------------------------------------
# Cross-process single-flight refresh
# ---------------------------------------------------------------------------


class TestCrossProcessSingleFlight:
    async def test_two_workers_refreshing_at_once_hit_the_token_endpoint_once(
        self, server: FakeAuthServer
    ) -> None:
        """Neither worker races the token endpoint; the loser polls the
        store — via the lock/peek loop in ``_refresh_with_lock`` — until it
        sees the winner's write, however many polls that takes. The winner
        writes to the store *before* releasing the lock, so there is no gap
        where the lock is free but the store is still stale."""
        server._refresh_tokens["refresh-1"] = True
        lock = MemoryStore()
        store = MemoryCredentialStore()
        stored = _stored(refresh_token="refresh-1")
        await store.put("jira", stored)

        worker_1 = OAuthClient(
            client_id="loom-cli", token_endpoint="https://auth.test/token",
            lock=lock, store=store, owner="worker-1", poll_interval=0.01,
        )
        worker_2 = OAuthClient(
            client_id="loom-cli", token_endpoint="https://auth.test/token",
            lock=lock, store=store, owner="worker-2", poll_interval=0.01,
        )

        results = await asyncio.gather(
            worker_1.refresh("jira", stored), worker_2.refresh("jira", stored)
        )

        assert server.token_calls == 1
        # Both workers end up with a usable, matching token — the loser
        # re-read the winner's write rather than requesting its own.
        assert results[0].token.reveal() == results[1].token.reveal()
        assert (await store.get("jira")).reveal() == results[0].token.reveal()

    async def test_the_loser_never_calls_the_token_endpoint(
        self, server: FakeAuthServer
    ) -> None:
        server._refresh_tokens["refresh-1"] = True
        lock = MemoryStore()
        store = MemoryCredentialStore()
        stored = _stored(refresh_token="refresh-1")
        await store.put("jira", stored)

        winner = OAuthClient(
            client_id="loom-cli", token_endpoint="https://auth.test/token",
            lock=lock, store=store, owner="winner",
        )
        # The winner acquires the lock, refreshes, writes to the store, and
        # releases — all before this returns.
        fresh = await winner.refresh("jira", stored)

        # A second worker starts from the *same* stale record (as if it had
        # read it before the winner's write), with someone else still
        # holding the lock — it must return the winner's write rather than
        # calling the token endpoint again.
        await lock.acquire("credential-refresh:jira", "someone-else", 30.0)
        loser = OAuthClient(
            client_id="loom-cli", token_endpoint="https://auth.test/token",
            lock=lock, store=store, owner="loser", poll_interval=0.01,
        )
        result = await loser.refresh("jira", stored)
        assert result.token.reveal() == fresh.token.reveal()
        assert server.token_calls == 1

    async def test_takeover_when_the_lock_holder_never_writes(
        self, server: FakeAuthServer
    ) -> None:
        """A crashed refresher's lease should not wedge every other caller
        forever — its lease expiring lets the next poll's re-acquire take
        over rather than waiting the full deadline out."""
        lock = MemoryStore()
        store = MemoryCredentialStore()
        stored = _stored(refresh_token="never-registered")
        await store.put("jira", stored)

        # Simulate a crashed holder: a short lease, never released, never
        # followed by a write.
        await lock.acquire("credential-refresh:jira", "dead-worker", 0.02)

        client = OAuthClient(
            client_id="loom-cli", token_endpoint="https://auth.test/token",
            lock=lock, store=store, owner="rescuer", poll_interval=0.05,
        )
        # Takes over once the dead lease expires, then fails for a genuine
        # reason (the stored refresh token was never valid) — proving the
        # takeover happened rather than a wait-forever hang.
        with pytest.raises(AuthExpired, match="could not be refreshed"):
            await client.refresh("jira", stored)
        assert server.token_calls == 1


# ---------------------------------------------------------------------------
# No-secret-escapes
# ---------------------------------------------------------------------------


class TestNoSecretEscapes:
    def test_repr_never_contains_a_client_secret(self) -> None:
        client = _client(client_secret="super-secret-value")
        assert "super-secret-value" not in repr(client)

    async def test_a_refreshed_credential_prints_as_redacted(
        self, server: FakeAuthServer
    ) -> None:
        server._refresh_tokens["refresh-1"] = True
        client = _client()
        fresh = await client.refresh("jira", _stored(refresh_token="refresh-1"))
        assert "access-" not in repr(fresh.token)
        assert str(fresh.token) == "Secret(***)"
        if fresh.refresh_token is not None:
            assert str(fresh.refresh_token) == "Secret(***)"
