"""HTTP surface auth: the FastAPI dependency, RFC 9728 metadata, and ``LoomClient``.

Mirrors ``test_mcp_auth.py``'s split, for the plain HTTP surface instead of
MCP:

- **``server/auth.py``** — ``build_http_auth``, the RFC 9728 metadata route,
  and the FastAPI dependency, unit-tested directly.
- **Wiring** — ``create_app`` end to end, over a real FastAPI app and
  ``httpx.ASGITransport``, exactly like ``test_p2_production.py``'s own
  ``api`` fixture but with identity configured.
- **``LoomClient``** — bearer token attachment and the one-shot refresh on
  401, against a fake ASGI app rather than a real LOOM server.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loom import Context, Runtime, step, workflow
from loom.core.exceptions import ConfigurationError
from loom.identity.config import IdentitySettings
from loom.stores.memory import MemoryStore

pytest.importorskip("fastapi", reason="needs the api extra")


# ---------------------------------------------------------------------------
# server/auth.py, unit
# ---------------------------------------------------------------------------


class TestResourceMetadataUrl:
    def test_inserts_the_well_known_segment_before_the_path(self) -> None:
        from loom.server.auth import _resource_metadata_url

        assert (
            _resource_metadata_url("https://loom.example.com/api")
            == "https://loom.example.com/.well-known/oauth-protected-resource/api"
        )

    def test_a_bare_host_gets_no_trailing_path(self) -> None:
        from loom.server.auth import _resource_metadata_url

        assert (
            _resource_metadata_url("https://loom.example.com")
            == "https://loom.example.com/.well-known/oauth-protected-resource"
        )


class TestBuildHttpAuth:
    def test_unconfigured_settings_yield_no_auth(self) -> None:
        from loom.server.auth import build_http_auth

        assert build_http_auth(IdentitySettings()) is None

    def test_a_verifier_with_no_issuer_or_resource_is_refused(self, tmp_path: Any) -> None:
        from loom.server.auth import build_http_auth

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        settings = IdentitySettings(static_tokens_file=str(path))

        with pytest.raises(ConfigurationError, match="LOOM_AUTH_ISSUER"):
            build_http_auth(settings)

    def test_a_fully_configured_settings_builds_everything(self, tmp_path: Any) -> None:
        from loom.server.auth import build_http_auth

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        settings = IdentitySettings(
            issuer="https://auth.test",
            resource="https://loom.test",
            static_tokens_file=str(path),
        )

        auth = build_http_auth(settings)
        assert auth is not None
        assert auth.issuer == "https://auth.test"
        assert auth.resource == "https://loom.test"
        assert auth.metadata_url == (
            "https://loom.test/.well-known/oauth-protected-resource"
        )


class TestPrincipalDependency:
    async def test_no_auth_configured_always_yields_anonymous(self) -> None:
        from loom.identity.principal import ANONYMOUS
        from loom.server.auth import build_principal_dependency

        dependency = build_principal_dependency(None)
        assert await dependency(credentials=None) is ANONYMOUS

    async def test_missing_credentials_is_a_401(self, tmp_path: Any) -> None:
        from fastapi import HTTPException

        from loom.server.auth import build_http_auth, build_principal_dependency

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        auth = build_http_auth(
            IdentitySettings(
                issuer="https://auth.test",
                resource="https://loom.test",
                static_tokens_file=str(path),
            )
        )
        dependency = build_principal_dependency(auth)

        with pytest.raises(HTTPException) as caught:
            await dependency(credentials=None)
        assert caught.value.status_code == 401
        assert "resource_metadata" in caught.value.headers["WWW-Authenticate"]

    async def test_an_unknown_token_is_a_401(self, tmp_path: Any) -> None:
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials

        from loom.server.auth import build_http_auth, build_principal_dependency

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        auth = build_http_auth(
            IdentitySettings(
                issuer="https://auth.test",
                resource="https://loom.test",
                static_tokens_file=str(path),
            )
        )
        dependency = build_principal_dependency(auth)
        bad = HTTPAuthorizationCredentials(scheme="Bearer", credentials="nope")

        with pytest.raises(HTTPException) as caught:
            await dependency(credentials=bad)
        assert caught.value.status_code == 401

    async def test_a_known_token_resolves_a_principal(self, tmp_path: Any) -> None:
        from fastapi.security import HTTPAuthorizationCredentials

        from loom.server.auth import build_http_auth, build_principal_dependency

        path = tmp_path / "tokens.json"
        path.write_text(
            json.dumps({"tok-alice": {"subject": "alice", "scopes": ["runs:read"]}})
        )
        auth = build_http_auth(
            IdentitySettings(
                issuer="https://auth.test",
                resource="https://loom.test",
                static_tokens_file=str(path),
            )
        )
        dependency = build_principal_dependency(auth)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok-alice")

        principal = await dependency(credentials=credentials)
        assert principal.subject == "alice"
        assert principal.has("runs:read")


# ---------------------------------------------------------------------------
# create_app, end to end
# ---------------------------------------------------------------------------

pytest.importorskip("jwt", reason="test tokens use the identity extra's verifier path")


@pytest.fixture
def static_identity(tmp_path: Any) -> IdentitySettings:
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "tok-alice": {
                    "subject": "alice",
                    "scopes": ["runs:write", "runs:read", "workflows:read"],
                },
                "tok-bob": {
                    "subject": "bob",
                    "scopes": ["runs:write", "runs:read", "workflows:read"],
                },
                "tok-viewer": {"subject": "viewer", "scopes": ["workflows:read"]},
                "tok-root": {"subject": "root", "scopes": ["admin"]},
            }
        )
    )
    return IdentitySettings(
        issuer="https://auth.test", resource="https://loom.test", static_tokens_file=str(path)
    )


@workflow(name="http_auth_doubler")
async def _doubler(ctx: Context, n: int) -> int:
    return await ctx.step(_double, n)


@step
async def _double(n: int) -> int:
    return n * 2


def _client(app: Any, *, token: str | None = None) -> Any:
    from loom.server import LoomClient

    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
    return LoomClient(http=http, token=token)


@pytest.fixture
def authed_app(static_identity: IdentitySettings) -> Any:
    from loom.server.app import create_app

    rt = Runtime(store=MemoryStore())
    rt.register(_doubler)
    return create_app(rt, identity=static_identity)


class TestCreateAppCompatibility:
    async def test_unconfigured_identity_behaves_exactly_as_before(self) -> None:
        """The compatibility contract: no LOOM_AUTH_* set, no auth at all."""
        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore())
        rt.register(_doubler)
        app = create_app(rt, identity=IdentitySettings())
        client = _client(app)

        run = await client.start("http_auth_doubler", 5, wait=True)
        assert run["output"] == 10


class TestCreateAppAuthentication:
    async def test_no_token_is_401_with_resource_metadata(self, authed_app: Any) -> None:
        from loom.server import LoomClientError

        client = _client(authed_app)
        with pytest.raises(LoomClientError) as caught:
            await client.workflows()

        assert caught.value.status_code == 401
        assert caught.value.requires_reauth
        assert "resource_metadata" in (caught.value.www_authenticate or "")

    async def test_an_unknown_token_is_401(self, authed_app: Any) -> None:
        from loom.server import LoomClientError

        client = _client(authed_app, token="not-a-real-token")
        with pytest.raises(LoomClientError) as caught:
            await client.workflows()
        assert caught.value.status_code == 401

    async def test_the_protected_resource_metadata_route_is_public(
        self, authed_app: Any
    ) -> None:
        transport = httpx.ASGITransport(app=authed_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://loom.test"
        ) as http:
            response = await http.get("/.well-known/oauth-protected-resource")

        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == "https://loom.test"
        assert body["authorization_servers"] == ["https://auth.test"]

    async def test_a_valid_token_is_admitted(self, authed_app: Any) -> None:
        client = _client(authed_app, token="tok-alice")
        run = await client.start("http_auth_doubler", 5, wait=True)
        assert run["output"] == 10


class TestCreateAppScopes:
    async def test_a_read_only_token_cannot_start_a_run(self, authed_app: Any) -> None:
        from loom.server import LoomClientError

        client = _client(authed_app, token="tok-viewer")
        with pytest.raises(LoomClientError) as caught:
            await client.start("http_auth_doubler", 5)

        assert caught.value.status_code == 403
        assert caught.value.requires_reauth
        assert "runs:write" in caught.value.args[0]

    async def test_a_read_only_token_can_list_workflows(self, authed_app: Any) -> None:
        client = _client(authed_app, token="tok-viewer")
        workflows = await client.workflows()
        assert any(w["name"] == "http_auth_doubler" for w in workflows)


class TestCreateAppOwnership:
    async def test_a_run_started_by_one_principal_is_redacted_for_another(
        self, authed_app: Any
    ) -> None:
        alice = _client(authed_app, token="tok-alice")
        bob = _client(authed_app, token="tok-bob")

        started = await alice.start(
            "http_auth_doubler", 21, idempotency_key="shared", wait=True
        )
        replayed = await bob.start(
            "http_auth_doubler", 21, idempotency_key="shared", wait=True
        )

        assert started["run_id"] == replayed["run_id"]
        assert started["output"] == 42
        assert replayed["output"] is None

    async def test_journal_of_someone_elses_run_is_403(self, authed_app: Any) -> None:
        from loom.server import LoomClientError

        alice = _client(authed_app, token="tok-alice")
        bob = _client(authed_app, token="tok-bob")

        run = await alice.start("http_auth_doubler", 5, wait=True)
        with pytest.raises(LoomClientError) as caught:
            await bob.journal(run["run_id"])
        assert caught.value.status_code == 403

    async def test_admin_scope_bypasses_ownership(self, authed_app: Any) -> None:
        alice = _client(authed_app, token="tok-alice")
        root = _client(authed_app, token="tok-root")

        run = await alice.start("http_auth_doubler", 5, wait=True)
        journal = await root.journal(run["run_id"])
        assert isinstance(journal, list)

        fetched = await root.get(run["run_id"])
        assert fetched["output"] == 10  # not redacted for an admin


# ---------------------------------------------------------------------------
# LoomClient: bearer token attachment and refresh-on-401
# ---------------------------------------------------------------------------


class TestLoomClientBearerToken:
    def _echo_app(self):
        """A minimal ASGI app echoing back the Authorization header it saw."""

        async def app(scope, receive, send):
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization", b"").decode()
            body = json.dumps({"seen_authorization": auth}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        return app

    async def test_a_fixed_token_is_sent_as_a_bearer_header(self) -> None:
        from loom.server import LoomClient

        transport = httpx.ASGITransport(app=self._echo_app())
        http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        client = LoomClient(http=http, token="tok-123")

        result = await client._request("GET", "/anything")
        assert result["seen_authorization"] == "Bearer tok-123"

    async def test_no_token_sends_no_authorization_header(self) -> None:
        from loom.server import LoomClient

        transport = httpx.ASGITransport(app=self._echo_app())
        http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        client = LoomClient(http=http)

        result = await client._request("GET", "/anything")
        assert result["seen_authorization"] == ""

    def test_token_and_token_provider_together_is_rejected(self) -> None:
        from loom.server import LoomClient

        async def provider(force_refresh: bool) -> str:
            return "x"

        with pytest.raises(ValueError, match="not both"):
            LoomClient(base_url="http://loom.test", token="a", token_provider=provider)

    async def test_a_401_triggers_exactly_one_forced_refresh_and_retries(self) -> None:
        from loom.server import LoomClient

        calls: list[bool] = []
        tokens = {False: "stale", True: "fresh"}

        async def provider(force_refresh: bool) -> str:
            calls.append(force_refresh)
            return tokens[force_refresh]

        seen: list[str] = []

        async def app(scope, receive, send):
            headers = dict(scope["headers"])
            token = headers.get(b"authorization", b"").decode()
            seen.append(token)
            ok = token == "Bearer fresh"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200 if ok else 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer error="invalid_token"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})

        transport = httpx.ASGITransport(app=app)
        http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        client = LoomClient(http=http, token_provider=provider)

        result = await client._request("GET", "/anything")

        assert calls == [False, True]
        assert seen == ["Bearer stale", "Bearer fresh"]
        assert result == {}

    async def test_a_persistent_401_raises_with_www_authenticate_captured(self) -> None:
        from loom.server import LoomClient, LoomClientError

        async def provider(force_refresh: bool) -> str:
            return "always-rejected"

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (
                            b"www-authenticate",
                            b'Bearer error="invalid_token", resource_metadata="https://x/meta"',
                        ),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail": "nope"}'})

        transport = httpx.ASGITransport(app=app)
        http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        client = LoomClient(http=http, token_provider=provider)

        with pytest.raises(LoomClientError) as caught:
            await client._request("GET", "/anything")

        assert caught.value.status_code == 401
        assert caught.value.requires_reauth
        assert "resource_metadata" in caught.value.www_authenticate
        assert str(caught.value) == "nope"

    async def test_structured_insufficient_scope_detail_is_unwrapped(self) -> None:
        from loom.server import LoomClient, LoomClientError

        async def app(scope, receive, send):
            body = json.dumps(
                {
                    "detail": {
                        "error": "insufficient_scope",
                        "detail": "'bob' does not hold the 'runs:write' scope",
                        "required": "runs:write",
                    }
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        transport = httpx.ASGITransport(app=app)
        http = httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        client = LoomClient(http=http)

        with pytest.raises(LoomClientError) as caught:
            await client._request("GET", "/anything")

        assert caught.value.status_code == 403
        assert "runs:write" in str(caught.value)


# ---------------------------------------------------------------------------
# cmd_serve's loopback guard
# ---------------------------------------------------------------------------


class TestCmdServeRefusesUnauthenticatedNetworkBinds:
    def _args(self, **overrides: Any) -> Any:
        import argparse

        base = dict(
            host="127.0.0.1",
            port=8000,
            log_level="info",
            module=None,
            server=None,
            json=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_a_public_bind_with_no_identity_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.cli import commands

        monkeypatch.delenv("LOOM_AUTH_JWKS_URI", raising=False)
        monkeypatch.delenv("LOOM_AUTH_STATIC_TOKENS_FILE", raising=False)
        monkeypatch.delenv("LOOM_AUTH_INTROSPECTION_ENDPOINT", raising=False)
        code = commands.cmd_serve(self._args(host="0.0.0.0"))

        assert code == commands.Exit.USAGE

    def test_loopback_is_fine_with_no_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from loom.cli import commands

        monkeypatch.delenv("LOOM_AUTH_JWKS_URI", raising=False)
        monkeypatch.delenv("LOOM_AUTH_STATIC_TOKENS_FILE", raising=False)
        monkeypatch.delenv("LOOM_AUTH_INTROSPECTION_ENDPOINT", raising=False)
        monkeypatch.chdir(tmp_path)

        ran = {}

        def fake_run(app: Any, **kwargs: Any) -> None:
            ran["called"] = True

        monkeypatch.setattr(commands, "resolve", lambda *a, **k: _fake_target())
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_run)

        code = commands.cmd_serve(self._args(host="127.0.0.1"))
        assert code == commands.Exit.OK
        assert ran["called"]


def _fake_target() -> Any:
    from loom.cli.targets import Target
    from loom.facade import LocalFacade

    rt = Runtime(store=MemoryStore())
    return Target(LocalFacade(rt), None)
