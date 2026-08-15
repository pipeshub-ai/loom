"""``loom login`` / ``logout`` / ``whoami`` / ``connect``.

Mirrors ``test_oauth_client.py``'s technique for the flows (a fake
authorization server driven through ``httpx.MockTransport``, actually
executed rather than mocked at ``OAuthClient``'s own boundary) and adds one
piece that module does not need: a real local socket for the PKCE redirect,
driven with a raw HTTP request the way a browser actually would.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from loom.cli import auth_commands
from loom.cli.output import Exit, Printer
from loom.connectors.credentials import MemoryCredentialStore, StoredCredential
from loom.connectors.oauth_client import MetadataRefresher
from loom.core.exceptions import AuthExpired, ConfigurationError
from loom.core.secret import Secret
from test_oauth_client import FakeAuthServer, _patch_transport


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "server": None,
        "authorization_endpoint": None,
        "token_endpoint": None,
        "device_authorization_endpoint": None,
        "client_id": None,
        "client_secret": None,
        "scope": None,
        "pkce": False,
        "device": False,
        "timeout": 5.0,
        "redirect_port": None,
        "json": False,
        "provider": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _target(**overrides: Any) -> auth_commands._OAuthTarget:
    defaults: dict[str, Any] = {
        "name": "jira",
        "authorization_endpoint": "https://auth.test/authorize",
        "token_endpoint": "https://auth.test/token",
        "device_authorization_endpoint": "https://auth.test/device_authorization",
        "client_id": "loom-cli",
        "client_secret": None,
        "scopes": (),
    }
    defaults.update(overrides)
    return auth_commands._OAuthTarget(**defaults)


@pytest.fixture
def out() -> Printer:
    return Printer(as_json=True, quiet=True)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeAuthServer:
    fake = FakeAuthServer()
    _patch_transport(monkeypatch, fake.transport())
    return fake


@pytest.fixture
def store() -> MemoryCredentialStore:
    """One shared in-memory store, wired the way ``auth_commands._store()``
    wires the real one, monkeypatched in for the duration of the test."""
    return MemoryCredentialStore()


@pytest.fixture(autouse=True)
def _use_memory_store(
    monkeypatch: pytest.MonkeyPatch, store: MemoryCredentialStore
) -> None:
    store._refresher = MetadataRefresher(store=store)
    monkeypatch.setattr(auth_commands, "_store", lambda: store)


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every test gets a browser that never actually opens — ``webbrowser.open``
    calls are recorded here instead."""
    opened: list[str] = []
    monkeypatch.setattr(auth_commands.webbrowser, "open", lambda url: opened.append(url))
    return opened


@pytest.fixture(autouse=True)
def _clean_redirect_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer .env must not change what these tests assert about the
    default port — each test that wants an override sets the var itself."""
    monkeypatch.delenv(auth_commands.REDIRECT_PORT_ENV, raising=False)


# ---------------------------------------------------------------------------
# Headless detection
# ---------------------------------------------------------------------------


class TestIsHeadless:
    def test_the_override_env_var_always_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_LOGIN_HEADLESS", "1")
        monkeypatch.setattr(auth_commands.sys, "platform", "darwin")
        assert auth_commands._is_headless() is True

    def test_macos_is_never_headless_by_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_LOGIN_HEADLESS", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(auth_commands.sys, "platform", "darwin")
        assert auth_commands._is_headless() is False

    def test_linux_with_no_display_is_headless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_LOGIN_HEADLESS", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(auth_commands.sys, "platform", "linux")
        assert auth_commands._is_headless() is True

    def test_linux_with_a_display_is_not_headless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_LOGIN_HEADLESS", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(auth_commands.sys, "platform", "linux")
        assert auth_commands._is_headless() is False


# ---------------------------------------------------------------------------
# env_prefix_for
# ---------------------------------------------------------------------------


class TestEnvPrefixFor:
    def test_a_simple_name(self) -> None:
        assert auth_commands.env_prefix_for("jira") == "LOOM_CONNECT_JIRA"

    def test_punctuation_becomes_underscores(self) -> None:
        assert auth_commands.env_prefix_for("my-service.v2") == "LOOM_CONNECT_MY_SERVICE_V2"


# ---------------------------------------------------------------------------
# _resolve_target
# ---------------------------------------------------------------------------


class TestResolveTarget:
    def test_flags_win_over_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_CONNECT_JIRA_CLIENT_ID", "env-client")
        args = _args(
            client_id="flag-client",
            token_endpoint="https://x/token",
            authorization_endpoint="https://x/authorize",
        )
        target = auth_commands._resolve_target(args, name="jira", env_prefix="LOOM_CONNECT_JIRA")
        assert target.client_id == "flag-client"

    def test_environment_fills_in_what_flags_omit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_CONNECT_JIRA_CLIENT_ID", "env-client")
        monkeypatch.setenv("LOOM_CONNECT_JIRA_TOKEN_ENDPOINT", "https://x/token")
        monkeypatch.setenv("LOOM_CONNECT_JIRA_AUTHORIZATION_ENDPOINT", "https://x/authorize")
        target = auth_commands._resolve_target(
            _args(), name="jira", env_prefix="LOOM_CONNECT_JIRA"
        )
        assert target.client_id == "env-client"
        assert target.token_endpoint == "https://x/token"

    def test_missing_token_endpoint_or_client_id_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="token endpoint and a client id"):
            auth_commands._resolve_target(_args(), name="jira", env_prefix="LOOM_CONNECT_JIRA")

    def test_missing_both_authorization_paths_is_a_configuration_error(self) -> None:
        args = _args(client_id="c", token_endpoint="https://x/token")
        with pytest.raises(ConfigurationError, match="nothing to connect to"):
            auth_commands._resolve_target(args, name="jira", env_prefix="LOOM_CONNECT_JIRA")

    def test_scope_flags_are_used_verbatim(self) -> None:
        args = _args(
            client_id="c",
            token_endpoint="https://x/token",
            authorization_endpoint="https://x/authorize",
            scope=["read", "write"],
        )
        target = auth_commands._resolve_target(args, name="jira", env_prefix="LOOM_CONNECT_JIRA")
        assert target.scopes == ("read", "write")

    def test_scopes_fall_back_to_a_space_separated_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CONNECT_JIRA_SCOPES", "read write")
        args = _args(client_id="c", token_endpoint="https://x/token", authorization_endpoint="https://x/authorize")
        target = auth_commands._resolve_target(args, name="jira", env_prefix="LOOM_CONNECT_JIRA")
        assert target.scopes == ("read", "write")

    def test_a_known_provider_fills_endpoints_from_the_registry(self) -> None:
        target = auth_commands._resolve_target(
            _args(client_id="X", client_secret="Y"),
            name="google",
            env_prefix="LOOM_CONNECT_GOOGLE",
            provider_hint="google",
        )
        assert target.token_endpoint == "https://oauth2.googleapis.com/token"
        assert target.authorization_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
        assert target.extra_auth_params["access_type"] == "offline"
        assert target.provider_id == "google"
        assert target.pkce is True

    def test_scope_flags_replace_provider_defaults_rather_than_merging(self) -> None:
        target = auth_commands._resolve_target(
            _args(client_id="X", client_secret="Y", scope=["openid"]),
            name="google",
            env_prefix="LOOM_CONNECT_GOOGLE",
            provider_hint="google",
        )
        assert target.scopes == ("openid",)

    def test_login_does_not_infer_a_provider_from_the_name(self) -> None:
        with pytest.raises(ConfigurationError, match="token endpoint and a client id"):
            auth_commands._resolve_target(
                _args(), name="google", env_prefix="LOOM_LOGIN"
            )

    def test_an_unknown_provider_hint_is_named_in_the_error(self) -> None:
        with pytest.raises(ConfigurationError, match="not a known provider"):
            auth_commands._resolve_target(
                _args(),
                name="custom",
                env_prefix="LOOM_CONNECT_CUSTOM",
                provider_hint="not-a-real-provider",
            )

    def test_github_disables_pkce(self) -> None:
        target = auth_commands._resolve_target(
            _args(client_id="X", client_secret="Y"),
            name="github",
            env_prefix="LOOM_CONNECT_GITHUB",
            provider_hint="github",
        )
        assert target.pkce is False


# ---------------------------------------------------------------------------
# _choose_flow
# ---------------------------------------------------------------------------


class TestChooseFlow:
    def test_explicit_device_flag_wins(self) -> None:
        assert auth_commands._choose_flow(_args(device=True), _target()) == "device"

    def test_explicit_pkce_flag_wins(self) -> None:
        assert auth_commands._choose_flow(_args(pkce=True), _target()) == "pkce"

    def test_auto_detects_device_when_headless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth_commands, "_is_headless", lambda: True)
        assert auth_commands._choose_flow(_args(), _target()) == "device"

    def test_auto_detects_pkce_when_graphical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth_commands, "_is_headless", lambda: False)
        assert auth_commands._choose_flow(_args(), _target()) == "pkce"

    def test_pkce_falls_back_to_device_without_an_authorization_endpoint(self) -> None:
        target = _target(authorization_endpoint=None)
        assert auth_commands._choose_flow(_args(pkce=True), target) == "device"

    def test_device_falls_back_to_pkce_without_a_device_endpoint(self) -> None:
        target = _target(device_authorization_endpoint=None)
        assert auth_commands._choose_flow(_args(device=True), target) == "pkce"

    def test_neither_flow_available_is_a_configuration_error(self) -> None:
        target = _target(authorization_endpoint=None, device_authorization_endpoint=None)
        with pytest.raises(ConfigurationError, match="nothing to connect to"):
            auth_commands._choose_flow(_args(pkce=True), target)


# ---------------------------------------------------------------------------
# The PKCE redirect listener
# ---------------------------------------------------------------------------


class TestPkceListener:
    async def test_receives_query_params_from_a_raw_redirect_request(self) -> None:
        listener = await auth_commands._PkceListener.start(port=0)
        try:
            assert listener.port > 0
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
            writer.write(
                b"GET /callback?code=abc123&state=xyz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()

            params = await listener.wait_for_redirect(timeout=5.0)
        finally:
            await listener.close()

        assert params == {"code": "abc123", "state": "xyz"}
        assert response.startswith(b"HTTP/1.1 200 OK")

    async def test_times_out_when_nothing_ever_connects(self) -> None:
        listener = await auth_commands._PkceListener.start(port=0)
        try:
            with pytest.raises(ConfigurationError, match="timed out"):
                await listener.wait_for_redirect(timeout=0.05)
        finally:
            await listener.close()

    async def test_binds_the_stable_default_port(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        listener = await auth_commands._PkceListener.start()
        try:
            assert listener.port == auth_commands.DEFAULT_REDIRECT_PORT
        finally:
            await listener.close()

    async def test_a_busy_port_is_an_error_not_a_silent_hop(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        held = await auth_commands._PkceListener.start()
        try:
            with pytest.raises(ConfigurationError, match="is in use"):
                await auth_commands._PkceListener.start()
        finally:
            await held.close()


# ---------------------------------------------------------------------------
# Redirect port resolution
# ---------------------------------------------------------------------------


class TestResolveRedirectPort:
    def test_default_is_stable(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert auth_commands.resolve_redirect_port() == 8931

    def test_an_explicit_port_wins(self) -> None:
        assert auth_commands.resolve_redirect_port(7777) == 7777

    def test_env_overrides_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(auth_commands.REDIRECT_PORT_ENV, "4321")
        assert auth_commands.resolve_redirect_port() == 4321

    def test_dotenv_overrides_the_default(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("LOOM_OAUTH_REDIRECT_PORT=2468\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert auth_commands.resolve_redirect_port() == 2468

    def test_real_env_wins_over_dotenv(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("LOOM_OAUTH_REDIRECT_PORT=2468\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(auth_commands.REDIRECT_PORT_ENV, "4321")
        assert auth_commands.resolve_redirect_port() == 4321

    def test_a_garbage_value_is_a_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(auth_commands.REDIRECT_PORT_ENV, "not-a-port")
        with pytest.raises(ConfigurationError, match="not a port number"):
            auth_commands.resolve_redirect_port()


# ---------------------------------------------------------------------------
# Device flow, executed against a fake authorization server
# ---------------------------------------------------------------------------


class TestRunDeviceFlow:
    async def test_completes_once_approved(
        self, server: FakeAuthServer, out: Printer
    ) -> None:
        from loom.connectors.oauth_client import OAuthClient

        client = OAuthClient(
            client_id="loom-cli",
            token_endpoint="https://auth.test/token",
            device_authorization_endpoint="https://auth.test/device_authorization",
            poll_interval=0.0,
        )

        async def approve_shortly() -> None:
            while not server._devices:
                await asyncio.sleep(0)
            device_code = next(iter(server._devices))
            server.approve_device(device_code)

        approver = asyncio.ensure_future(approve_shortly())
        credential = await auth_commands._run_device_flow(client, out)
        await approver

        assert credential.token.reveal().startswith("access-")


# ---------------------------------------------------------------------------
# PKCE flow, end to end with a simulated browser redirect
# ---------------------------------------------------------------------------


class TestRunPkceFlow:
    async def test_completes_via_a_simulated_browser_redirect(
        self, server: FakeAuthServer, out: Printer, _no_real_browser: list[str]
    ) -> None:
        target = _target()

        async def simulate_browser() -> None:
            while not _no_real_browser:
                await asyncio.sleep(0)
            parsed = urlparse(_no_real_browser[0])
            qs = parse_qs(parsed.query)
            challenge = qs["code_challenge"][0]
            state = qs["state"][0]
            redirect_uri = urlparse(qs["redirect_uri"][0])
            server.issue_code("sim-code", code_challenge=challenge)

            reader, writer = await asyncio.open_connection(
                redirect_uri.hostname, redirect_uri.port
            )
            writer.write(
                f"GET {redirect_uri.path}?code=sim-code&state={state} HTTP/1.1\r\n"
                f"Host: {redirect_uri.hostname}\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        simulator = asyncio.ensure_future(simulate_browser())
        credential = await auth_commands._run_pkce_flow(
            target, out, timeout=5.0, redirect_port=0
        )
        await simulator

        assert credential.token.reveal().startswith("access-")

    async def test_extra_auth_params_reach_the_authorization_url(
        self, server: FakeAuthServer, out: Printer, _no_real_browser: list[str]
    ) -> None:
        target = _target(extra_auth_params={"access_type": "offline", "prompt": "consent"})

        async def simulate_browser() -> None:
            while not _no_real_browser:
                await asyncio.sleep(0)
            parsed = urlparse(_no_real_browser[0])
            qs = parse_qs(parsed.query)
            assert qs["access_type"] == ["offline"]
            assert qs["prompt"] == ["consent"]
            challenge = qs["code_challenge"][0]
            state = qs["state"][0]
            redirect_uri = urlparse(qs["redirect_uri"][0])
            server.issue_code("sim-code", code_challenge=challenge)
            reader, writer = await asyncio.open_connection(
                redirect_uri.hostname, redirect_uri.port
            )
            writer.write(
                f"GET {redirect_uri.path}?code=sim-code&state={state} HTTP/1.1\r\n"
                f"Host: {redirect_uri.hostname}\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        simulator = asyncio.ensure_future(simulate_browser())
        credential = await auth_commands._run_pkce_flow(
            target, out, timeout=5.0, redirect_port=0
        )
        await simulator
        assert credential.token.reveal().startswith("access-")

    async def test_pkce_false_omits_challenge_and_verifier(
        self, server: FakeAuthServer, out: Printer, _no_real_browser: list[str]
    ) -> None:
        target = _target(pkce=False)

        async def simulate_browser() -> None:
            while not _no_real_browser:
                await asyncio.sleep(0)
            parsed = urlparse(_no_real_browser[0])
            qs = parse_qs(parsed.query)
            assert "code_challenge" not in qs
            assert "code_challenge_method" not in qs
            state = qs["state"][0]
            redirect_uri = urlparse(qs["redirect_uri"][0])
            server.issue_code("sim-code")
            reader, writer = await asyncio.open_connection(
                redirect_uri.hostname, redirect_uri.port
            )
            writer.write(
                f"GET {redirect_uri.path}?code=sim-code&state={state} HTTP/1.1\r\n"
                f"Host: {redirect_uri.hostname}\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        simulator = asyncio.ensure_future(simulate_browser())
        credential = await auth_commands._run_pkce_flow(
            target, out, timeout=5.0, redirect_port=0
        )
        await simulator
        assert credential.token.reveal().startswith("access-")

    async def test_a_mismatched_state_is_rejected(
        self, server: FakeAuthServer, out: Printer, _no_real_browser: list[str]
    ) -> None:
        target = _target()

        async def simulate_wrong_state() -> None:
            while not _no_real_browser:
                await asyncio.sleep(0)
            parsed = urlparse(_no_real_browser[0])
            qs = parse_qs(parsed.query)
            challenge = qs["code_challenge"][0]
            redirect_uri = urlparse(qs["redirect_uri"][0])
            server.issue_code("sim-code", code_challenge=challenge)

            reader, writer = await asyncio.open_connection(
                redirect_uri.hostname, redirect_uri.port
            )
            writer.write(
                f"GET {redirect_uri.path}?code=sim-code&state=WRONG HTTP/1.1\r\n"
                f"Host: {redirect_uri.hostname}\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        simulator = asyncio.ensure_future(simulate_wrong_state())
        with pytest.raises(ConfigurationError, match="unexpected 'state'"):
            await auth_commands._run_pkce_flow(
                target, out, timeout=5.0, redirect_port=0
            )
        await simulator


# ---------------------------------------------------------------------------
# _connect stamps refresher metadata
# ---------------------------------------------------------------------------


class TestConnectStampsMetadata:
    async def test_device_flow_result_carries_endpoint_metadata(
        self, server: FakeAuthServer, out: Printer
    ) -> None:
        args = _args(
            device=True,
            client_id="loom-cli",
            token_endpoint="https://auth.test/token",
            device_authorization_endpoint="https://auth.test/device_authorization",
        )

        async def approve_shortly() -> None:
            while not server._devices:
                await asyncio.sleep(0)
            device_code = next(iter(server._devices))
            server.approve_device(device_code)

        approver = asyncio.ensure_future(approve_shortly())
        credential = await auth_commands._connect(
            args, out, name="jira", env_prefix="LOOM_CONNECT_JIRA"
        )
        await approver

        assert credential.metadata["token_endpoint"] == "https://auth.test/token"
        assert credential.metadata["client_id"] == "loom-cli"

    async def test_provider_id_is_persisted(
        self, server: FakeAuthServer, out: Printer
    ) -> None:
        args = _args(
            device=True,
            client_id="loom-cli",
            token_endpoint="https://auth.test/token",
            device_authorization_endpoint="https://auth.test/device_authorization",
            provider="google",
        )

        async def approve_shortly() -> None:
            while not server._devices:
                await asyncio.sleep(0)
            server.approve_device(next(iter(server._devices)))

        approver = asyncio.ensure_future(approve_shortly())
        credential = await auth_commands._connect(
            args, out, name="jira", env_prefix="LOOM_CONNECT_JIRA"
        )
        await approver
        assert credential.metadata["provider_id"] == "google"


class TestMetadataRefresherRoundTrip:
    async def test_a_credential_connected_now_can_refresh_itself_later(
        self, server: FakeAuthServer, out: Printer, store: MemoryCredentialStore
    ) -> None:
        """The whole point of stamping metadata at connect time: a store
        wired with one generic ``MetadataRefresher`` can renew this
        specific credential without the caller ever repeating its
        endpoint configuration."""
        args = _args(
            device=True,
            client_id="loom-cli",
            token_endpoint="https://auth.test/token",
            device_authorization_endpoint="https://auth.test/device_authorization",
        )

        async def approve_shortly() -> None:
            while not server._devices:
                await asyncio.sleep(0)
            device_code = next(iter(server._devices))
            server.approve_device(device_code)

        approver = asyncio.ensure_future(approve_shortly())
        credential = await auth_commands._connect(
            args, out, name="jira", env_prefix="LOOM_CONNECT_JIRA"
        )
        await approver
        await store.put("jira", credential)

        # Force expiry, as if time had passed.
        from dataclasses import replace

        expired = replace(credential, expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        await store.put("jira", expired)

        fresh_token = await store.get("jira")
        assert fresh_token.reveal().startswith("access-")
        assert server.token_calls == 2  # one for the initial device grant, one refresh


# ---------------------------------------------------------------------------
# The commands themselves
# ---------------------------------------------------------------------------


def _stored(
    *, scopes: frozenset[str] = frozenset(), expires_at: datetime | None = None
) -> StoredCredential:
    return StoredCredential(
        token=Secret("super-secret-access-token"),
        refresh_token=Secret("super-secret-refresh-token"),
        scopes=scopes,
        expires_at=expires_at,
        metadata={"token_endpoint": "https://x/token", "client_id": "c"},
    )


class TestCmdLogin:
    """``cmd_login`` (like every command) drives its own event loop via
    ``run_async`` -> ``asyncio.run()``, so these tests call it synchronously,
    the way the real CLI entry point does, rather than from inside a loop
    pytest-asyncio already has running."""

    def test_stores_under_loom_prefixed_server_name(
        self, monkeypatch: pytest.MonkeyPatch, store: MemoryCredentialStore, capsys: Any
    ) -> None:
        async def fake_connect(args, out, *, name, env_prefix):
            return _stored(scopes=frozenset({"runs:write"}))

        monkeypatch.setattr(auth_commands, "_connect", fake_connect)
        args = _args(server="http://example.test:9000", json=True)

        code = auth_commands.cmd_login(args)

        assert code == int(Exit.OK)
        assert asyncio.run(store.names()) == ["loom:http://example.test:9000"]
        printed = capsys.readouterr().out
        assert "super-secret-access-token" not in printed
        assert "runs:write" in printed

    def test_default_server_is_used_when_none_given(
        self, monkeypatch: pytest.MonkeyPatch, store: MemoryCredentialStore
    ) -> None:
        async def fake_connect(args, out, *, name, env_prefix):
            return _stored()

        monkeypatch.setattr(auth_commands, "_connect", fake_connect)
        code = auth_commands.cmd_login(_args())

        assert code == int(Exit.OK)
        assert asyncio.run(store.names()) == [
            auth_commands._server_credential_name(auth_commands.DEFAULT_SERVER)
        ]

    def test_a_configuration_error_from_connect_is_reported_as_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        async def fake_connect(args, out, *, name, env_prefix):
            raise ConfigurationError("no token endpoint")

        monkeypatch.setattr(auth_commands, "_connect", fake_connect)
        code = auth_commands.cmd_login(_args())

        assert code == int(Exit.USAGE)
        assert "no token endpoint" in capsys.readouterr().err

    def test_auth_expired_from_connect_is_also_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_connect(args, out, *, name, env_prefix):
            raise AuthExpired("the user denied the authorization request")

        monkeypatch.setattr(auth_commands, "_connect", fake_connect)
        assert auth_commands.cmd_login(_args()) == int(Exit.USAGE)


class TestCmdConnect:
    def test_stores_under_the_given_name(
        self, monkeypatch: pytest.MonkeyPatch, store: MemoryCredentialStore
    ) -> None:
        async def fake_connect(args, out, *, name, env_prefix):
            assert name == "jira"
            assert env_prefix == "LOOM_CONNECT_JIRA"
            return _stored()

        monkeypatch.setattr(auth_commands, "_connect", fake_connect)
        args = _args()
        args.name = "jira"

        assert auth_commands.cmd_connect(args) == int(Exit.OK)
        assert asyncio.run(store.names()) == ["jira"]


class TestCmdDisconnect:
    def test_forgets_the_named_credential(
        self, store: MemoryCredentialStore, capsys: Any
    ) -> None:
        asyncio.run(store.put("jira", _stored()))
        args = _args(json=True)
        args.name = "jira"

        assert auth_commands.cmd_disconnect(args) == int(Exit.OK)
        assert asyncio.run(store.names()) == []
        printed = capsys.readouterr().out
        assert '"disconnected": true' in printed

    def test_disconnecting_something_never_connected_is_not_an_error(
        self, capsys: Any
    ) -> None:
        args = _args(json=True)
        args.name = "never-connected"

        assert auth_commands.cmd_disconnect(args) == int(Exit.OK)
        printed = capsys.readouterr().out
        assert '"disconnected": false' in printed

    def test_disconnecting_one_name_leaves_others_stored(
        self, store: MemoryCredentialStore
    ) -> None:
        asyncio.run(store.put("jira", _stored()))
        asyncio.run(store.put("google", _stored()))
        args = _args(json=True)
        args.name = "jira"

        auth_commands.cmd_disconnect(args)
        assert asyncio.run(store.names()) == ["google"]


class TestCmdLogout:
    def test_forgets_the_stored_login(self, store: MemoryCredentialStore) -> None:
        asyncio.run(store.put("loom:http://x", _stored()))
        args = _args(server="http://x")

        assert auth_commands.cmd_logout(args) == int(Exit.OK)
        assert asyncio.run(store.names()) == []

    def test_logging_out_of_nothing_is_not_an_error(self) -> None:
        assert auth_commands.cmd_logout(_args(server="http://never-logged-in")) == int(Exit.OK)


class TestCmdWhoami:
    def test_nothing_connected(self, capsys: Any) -> None:
        args = _args(json=True)
        assert auth_commands.cmd_whoami(args) == int(Exit.OK)
        assert '"connected": []' in capsys.readouterr().out

    def test_lists_stored_credentials_without_ever_printing_the_token(
        self, store: MemoryCredentialStore, capsys: Any
    ) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)

        async def seed() -> None:
            await store.put(
                "jira", _stored(scopes=frozenset({"jira.issues:read"}), expires_at=future)
            )
            await store.put(
                "loom:http://x", _stored(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )

        asyncio.run(seed())

        code = auth_commands.cmd_whoami(_args(json=True))

        assert code == int(Exit.OK)
        printed = capsys.readouterr().out
        assert "super-secret-access-token" not in printed
        assert "super-secret-refresh-token" not in printed
        assert '"name": "jira"' in printed
        assert '"expired": false' in printed
        assert '"expired": true' in printed


# ---------------------------------------------------------------------------
# server_token_provider
# ---------------------------------------------------------------------------


class TestServerTokenProvider:
    async def test_nothing_stored_returns_none(self) -> None:
        provider = auth_commands.server_token_provider("http://x")
        assert await provider(False) is None

    async def test_a_stored_unexpired_token_is_returned(
        self, store: MemoryCredentialStore
    ) -> None:
        await store.put("loom:http://x", _stored())
        provider = auth_commands.server_token_provider("http://x")
        assert await provider(False) == "super-secret-access-token"

    async def test_a_credential_that_cannot_refresh_yields_none_not_a_raise(
        self, store: MemoryCredentialStore
    ) -> None:
        expired_no_metadata = StoredCredential(
            token=Secret("stale"), expires_at=datetime(2000, 1, 1, tzinfo=UTC)
        )
        await store.put("loom:http://x", expired_no_metadata)
        provider = auth_commands.server_token_provider("http://x")
        assert await provider(False) is None

    async def test_wires_into_loom_client_as_a_bearer_header(
        self, store: MemoryCredentialStore
    ) -> None:
        from loom.server.client import LoomClient

        await store.put("loom:http://x", _stored())
        client = LoomClient(
            base_url="http://x", token_provider=auth_commands.server_token_provider("http://x")
        )
        header = await client._authorization_header(force_refresh=False)
        assert header == {"Authorization": "Bearer super-secret-access-token"}

    async def test_an_unauthenticated_server_gets_no_header(self) -> None:
        from loom.server.client import LoomClient

        client = LoomClient(
            base_url="http://x", token_provider=auth_commands.server_token_provider("http://x")
        )
        header = await client._authorization_header(force_refresh=False)
        assert header == {}


# ---------------------------------------------------------------------------
# cli/targets.py wiring
# ---------------------------------------------------------------------------


class TestTargetsResolveWiresTokenProvider:
    def test_a_server_target_gets_a_loom_client_with_a_token_provider(self) -> None:
        from loom.cli.targets import resolve
        from loom.facade import RemoteFacade

        target = resolve(None, server="http://example.test:8000")

        assert isinstance(target.backend, RemoteFacade)
        assert target.backend.client._token_provider is not None


class TestCliWiring:
    def test_connect_accepts_redirect_port(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(
            ["connect", "jira", "--redirect-port", "4321"]
        )
        assert args.redirect_port == 4321
        assert args.name == "jira"

    def test_connect_accepts_provider(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(
            ["connect", "my-google", "--provider", "google"]
        )
        assert args.name == "my-google"
        assert args.provider == "google"

    def test_login_has_no_provider_flag(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(["login"])
        assert not hasattr(args, "provider")

    def test_providers_lists_google(self, capsys: Any) -> None:
        assert auth_commands.cmd_providers(_args(json=True)) == int(Exit.OK)
        printed = capsys.readouterr().out
        assert '"id": "google"' in printed
        assert "authorization_endpoint" in printed

    def test_disconnect_is_wired_into_the_parser(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(["disconnect", "jira"])
        assert args.name == "jira"
        assert args.command == "disconnect"

    def test_disconnect_dispatches_to_cmd_disconnect(self) -> None:
        from loom.cli import _HANDLERS

        assert _HANDLERS["disconnect"] is auth_commands.cmd_disconnect
