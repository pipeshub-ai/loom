"""MCP server auth: token verifiers, settings, and the FastMCP wiring.

Three layers, matching ``test_mcp_server.py``'s own split:

- **Verifiers** — ``identity/verifier.py`` against a real, locally-signed JWT
  (RS256) or a fake introspection server (``httpx.MockTransport``), never
  mocked at the verifier's own boundary.
- **Settings and factory** — ``identity/config.py`` and
  ``mcp_server/auth.py::build_mcp_auth``.
- **Wiring** — ``mcp_server/server.py``'s per-request facade, and the
  stdio-is-always-exempt guarantee, driven through a real ``FastMCP``
  instance with the SDK's own auth contextvar set exactly as its ASGI
  middleware would set it for a real HTTP request.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.identity.config import IdentitySettings, StaticPrincipalToken
from workflow_builder.identity.verifier import (
    IntrospectionTokenVerifier,
    StaticTokenVerifier,
    build_verifier,
)

pytest.importorskip("mcp", reason="needs the mcp extra")
jwt = pytest.importorskip("jwt", reason="needs the identity extra")


# ---------------------------------------------------------------------------
# A real RSA keypair and JWKS, for JWKSTokenVerifier
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keys() -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).to_jwk(
        key.public_key()
    ))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    return {"private_key": key, "jwks": {"keys": [jwk]}}


def _mint(rsa_keys: dict[str, Any], **claims: Any) -> str:
    return jwt.encode(
        claims, rsa_keys["private_key"], algorithm="RS256", headers={"kid": "test-key"}
    )


def _jwks_verifier(rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
    from workflow_builder.identity.verifier import JWKSTokenVerifier

    verifier = JWKSTokenVerifier(
        jwks_uri="https://auth.test/.well-known/jwks.json",
        issuer="https://auth.test",
        resource="https://loom.test",
        **kwargs,
    )
    monkeypatch.setattr(
        verifier._jwks_client, "fetch_data", lambda: dict(rsa_keys["jwks"])
    )
    return verifier


class TestJWKSTokenVerifier:
    async def test_a_valid_token_becomes_an_access_token(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            scope="runs:write runs:read",
            azp="loom-cli",
            exp=9999999999,
        )

        access = await verifier.verify_token(token)

        assert access is not None
        assert access.subject == "alice"
        assert set(access.scopes) == {"runs:write", "runs:read"}
        assert access.client_id == "loom-cli"
        assert access.resource == "https://loom.test"

    async def test_scp_list_claim_is_read_too(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            scp=["runs:read"],
            exp=9999999999,
        )

        access = await verifier.verify_token(token)
        assert access is not None
        assert access.scopes == ["runs:read"]

    async def test_wrong_audience_is_rejected(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RFC 8707 — the confused-deputy hole the plan calls out by name."""
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://someone-elses-resource.test",
            sub="alice",
            exp=9999999999,
        )

        assert await verifier.verify_token(token) is None

    async def test_wrong_issuer_is_rejected(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys,
            iss="https://not-our-auth-server.test",
            aud="https://loom.test",
            sub="alice",
            exp=9999999999,
        )

        assert await verifier.verify_token(token) is None

    async def test_an_expired_token_is_rejected(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch, leeway=0.0)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            exp=1,
        )

        assert await verifier.verify_token(token) is None

    async def test_leeway_forgives_small_clock_skew(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        verifier = _jwks_verifier(rsa_keys, monkeypatch, leeway=60.0)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            exp=int(time.time()) - 10,
        )

        assert await verifier.verify_token(token) is not None

    async def test_a_missing_subject_is_rejected(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys, iss="https://auth.test", aud="https://loom.test", exp=9999999999
        )

        assert await verifier.verify_token(token) is None

    async def test_a_tampered_signature_is_rejected(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verifier = _jwks_verifier(rsa_keys, monkeypatch)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            exp=9999999999,
        )
        tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]

        assert await verifier.verify_token(tampered) is None

    async def test_a_jwks_fetch_failure_fails_closed(
        self, rsa_keys: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A network blip verifying a token is a 401, not a 500."""
        verifier = _jwks_verifier(rsa_keys, monkeypatch)

        def _boom() -> None:
            raise jwt.PyJWKClientConnectionError("jwks endpoint unreachable")

        monkeypatch.setattr(verifier._jwks_client, "fetch_data", _boom)
        token = _mint(
            rsa_keys,
            iss="https://auth.test",
            aud="https://loom.test",
            sub="alice",
            exp=9999999999,
        )

        assert await verifier.verify_token(token) is None


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs.setdefault("transport", transport)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


class TestIntrospectionTokenVerifier:
    def _verifier(self, monkeypatch: pytest.MonkeyPatch, handle) -> IntrospectionTokenVerifier:
        _patch_transport(monkeypatch, httpx.MockTransport(handle))
        return IntrospectionTokenVerifier(
            introspection_endpoint="https://auth.test/introspect",
            client_id="loom-mcp",
            client_secret="s3cr3t",
            resource="https://loom.test",
        )

    async def test_an_active_token_becomes_an_access_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "sub": "bob",
                    "scope": "runs:read",
                    "aud": "https://loom.test",
                    "client_id": "loom-mcp",
                    "exp": 9999999999,
                },
            )

        verifier = self._verifier(monkeypatch, handle)
        access = await verifier.verify_token("opaque-token")

        assert access is not None
        assert access.subject == "bob"
        assert access.scopes == ["runs:read"]

    async def test_an_inactive_token_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"active": False})

        verifier = self._verifier(monkeypatch, handle)
        assert await verifier.verify_token("revoked") is None

    async def test_wrong_audience_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"active": True, "sub": "bob", "aud": "someone-else"}
            )

        verifier = self._verifier(monkeypatch, handle)
        assert await verifier.verify_token("tok") is None

    async def test_a_missing_subject_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"active": True, "aud": "https://loom.test"})

        verifier = self._verifier(monkeypatch, handle)
        assert await verifier.verify_token("tok") is None

    async def test_an_http_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        verifier = self._verifier(monkeypatch, handle)
        assert await verifier.verify_token("tok") is None

    async def test_the_client_secret_is_sent_as_basic_auth_not_the_token_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["auth_header"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"active": True, "sub": "bob"})

        verifier = self._verifier(monkeypatch, handle)
        await verifier.verify_token("tok")

        assert seen["auth_header"].startswith("Basic ")


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


class TestStaticTokenVerifier:
    async def test_a_known_token_resolves(self) -> None:
        verifier = StaticTokenVerifier(
            {"tok-alice": StaticPrincipalToken(subject="alice", scopes=["runs:write"])},
            resource="https://loom.test",
        )

        access = await verifier.verify_token("tok-alice")
        assert access is not None
        assert access.subject == "alice"
        assert access.scopes == ["runs:write"]

    async def test_an_unknown_token_is_rejected(self) -> None:
        verifier = StaticTokenVerifier({})
        assert await verifier.verify_token("nope") is None


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


class TestBuildVerifier:
    def test_nothing_configured_yields_no_verifier(self) -> None:
        assert build_verifier(IdentitySettings()) is None

    def test_jwks_without_issuer_or_resource_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="LOOM_AUTH_ISSUER"):
            build_verifier(IdentitySettings(jwks_uri="https://auth.test/jwks.json"))

    def test_introspection_without_client_credentials_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="INTROSPECTION_CLIENT"):
            build_verifier(
                IdentitySettings(introspection_endpoint="https://auth.test/introspect")
            )

    def test_static_tokens_take_priority_over_jwks(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        settings = IdentitySettings(
            jwks_uri="https://auth.test/jwks.json",
            issuer="https://auth.test",
            resource="https://loom.test",
            static_tokens_file=str(path),
        )

        assert isinstance(build_verifier(settings), StaticTokenVerifier)

    def test_a_valid_jwks_config_builds_the_jwks_verifier(self) -> None:
        from workflow_builder.identity.verifier import JWKSTokenVerifier

        settings = IdentitySettings(
            jwks_uri="https://auth.test/jwks.json",
            issuer="https://auth.test",
            resource="https://loom.test",
        )
        assert isinstance(build_verifier(settings), JWKSTokenVerifier)


# ---------------------------------------------------------------------------
# IdentitySettings
# ---------------------------------------------------------------------------


class TestIdentitySettings:
    def test_env_vars_populate_the_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_AUTH_JWKS_URI", "https://auth.test/jwks.json")
        monkeypatch.setenv("LOOM_AUTH_ISSUER", "https://auth.test")
        monkeypatch.setenv("LOOM_AUTH_RESOURCE", "https://loom.test")

        settings = IdentitySettings()
        assert settings.jwks_uri == "https://auth.test/jwks.json"
        assert settings.is_configured()

    def test_unset_is_not_configured(self) -> None:
        assert not IdentitySettings().is_configured()

    def test_load_static_tokens_parses_the_file(self, tmp_path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text(
            json.dumps({"tok-alice": {"subject": "alice", "scopes": ["runs:read"]}})
        )
        settings = IdentitySettings(static_tokens_file=str(path))

        tokens = settings.load_static_tokens()
        assert tokens["tok-alice"].subject == "alice"
        assert tokens["tok-alice"].scopes == ["runs:read"]

    def test_no_file_configured_yields_no_tokens(self) -> None:
        assert IdentitySettings().load_static_tokens() == {}

    def test_a_missing_file_fails_with_the_path_named(self) -> None:
        settings = IdentitySettings(static_tokens_file="/no/such/file.json")
        with pytest.raises(ConfigurationError, match=r"no/such/file\.json"):
            settings.load_static_tokens()

    def test_invalid_json_fails_with_a_clear_message(self, tmp_path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text("{not json")
        settings = IdentitySettings(static_tokens_file=str(path))

        with pytest.raises(ConfigurationError, match="not valid JSON"):
            settings.load_static_tokens()

    def test_a_json_array_instead_of_an_object_is_refused(self, tmp_path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text("[]")
        settings = IdentitySettings(static_tokens_file=str(path))

        with pytest.raises(ConfigurationError, match="JSON object"):
            settings.load_static_tokens()

    def test_an_entry_missing_subject_fails_at_load_not_at_first_use(self, tmp_path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"scopes": ["runs:read"]}}))
        settings = IdentitySettings(static_tokens_file=str(path))

        with pytest.raises(ConfigurationError):
            settings.load_static_tokens()


# ---------------------------------------------------------------------------
# build_mcp_auth
# ---------------------------------------------------------------------------


class TestBuildMcpAuth:
    def test_unconfigured_settings_yield_no_auth(self) -> None:
        from workflow_builder.mcp_server.auth import build_mcp_auth

        assert build_mcp_auth(IdentitySettings()) is None

    def test_a_verifier_with_no_issuer_or_resource_is_refused(self, tmp_path) -> None:
        """Reachable via static tokens, which need neither on their own —
        but AuthSettings itself requires both to publish RFC 9728 metadata."""
        from workflow_builder.mcp_server.auth import build_mcp_auth

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice"}}))
        settings = IdentitySettings(static_tokens_file=str(path))

        with pytest.raises(ConfigurationError, match="LOOM_AUTH_ISSUER"):
            build_mcp_auth(settings)

    def test_a_fully_configured_settings_builds_everything(self, tmp_path) -> None:
        from workflow_builder.mcp_server.auth import build_mcp_auth

        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({"tok": {"subject": "alice", "scopes": ["runs:read"]}}))
        settings = IdentitySettings(
            issuer="https://auth.test",
            resource="https://loom.test",
            static_tokens_file=str(path),
            required_scopes=["runs:read"],
            allowed_hosts=["loom.example.com"],
        )

        auth = build_mcp_auth(settings)
        assert auth is not None
        assert str(auth.auth_settings.issuer_url).rstrip("/") == "https://auth.test"
        assert auth.auth_settings.required_scopes == ["runs:read"]
        assert auth.transport_security.allowed_hosts == ["loom.example.com"]


# ---------------------------------------------------------------------------
# Wiring: per-request facade and the stdio exemption
# ---------------------------------------------------------------------------


@pytest.fixture
def static_identity(tmp_path) -> IdentitySettings:
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "tok-alice": {
                    "subject": "alice",
                    "scopes": ["runs:write", "runs:read", "workflows:read"],
                },
                "tok-bob": {"subject": "bob", "scopes": ["workflows:read"]},
            }
        )
    )
    return IdentitySettings(
        issuer="https://auth.test", resource="https://loom.test", static_tokens_file=str(path)
    )


def _authenticated(scopes_token: str):
    """Set the SDK's own contextvar exactly as its ASGI middleware would for
    a request bearing *scopes_token* — the same value ``get_access_token()``
    reads inside a tool handler, whether set by real middleware or, as here,
    directly by a test standing in for one."""
    from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
    from mcp.server.auth.provider import AccessToken

    return auth_context_var.set(
        AuthenticatedUser(AccessToken(token=scopes_token, client_id="test", scopes=[]))
    )


class TestPrincipalFacadeHelper:
    def test_unwrapped_when_auth_is_disabled(self) -> None:
        from workflow_builder.mcp_server.server import _principal_facade

        sentinel = object()
        assert _principal_facade(sentinel, False) is sentinel

    async def test_wraps_with_the_contextvars_principal_when_enabled(self) -> None:
        from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
        from mcp.server.auth.provider import AccessToken

        from workflow_builder.identity.facade import AuthorizedFacade
        from workflow_builder.mcp_server.server import _principal_facade

        access = AccessToken(
            token="tok", client_id="loom-cli", scopes=["runs:write"], subject="alice"
        )
        reset = auth_context_var.set(AuthenticatedUser(access))
        try:
            wrapped = _principal_facade(object(), True)
        finally:
            auth_context_var.reset(reset)

        assert isinstance(wrapped, AuthorizedFacade)
        assert wrapped.principal.subject == "alice"
        assert wrapped.principal.has("runs:write")

    async def test_no_token_in_context_falls_back_to_anonymous(self) -> None:
        from workflow_builder.identity.principal import ANONYMOUS
        from workflow_builder.mcp_server.server import _principal_facade

        wrapped = _principal_facade(object(), True)
        assert wrapped.principal is ANONYMOUS


class TestServeRefusesUnauthenticatedNetworkBinds:
    def test_a_public_bind_with_no_identity_is_refused(self) -> None:
        from workflow_builder.mcp_server import serve

        with pytest.raises(ValueError, match="refusing to bind"):
            serve(object(), transport="http", host="0.0.0.0", identity=IdentitySettings())

    def test_loopback_is_fine_with_no_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the guard is under test — stub `build_server`/`.run()` so this
        does not actually open a socket."""
        from workflow_builder import mcp_server

        called = {}

        def fake_build_server(*args: Any, **kwargs: Any):
            called.update(kwargs)

            class _Stub:
                def run(self, transport: str) -> None:
                    called["ran"] = transport

            return _Stub()

        monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
        mcp_server.serve(
            object(), transport="http", host="127.0.0.1", identity=IdentitySettings()
        )
        assert called["ran"] == "streamable-http"

    def test_stdio_is_always_fine_regardless_of_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from workflow_builder import mcp_server

        def fake_build_server(*args: Any, **kwargs: Any):
            class _Stub:
                def run(self, transport: str) -> None:
                    pass

            return _Stub()

        monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
        mcp_server.serve(object(), transport="stdio", identity=IdentitySettings())


class TestBuildServerHonoursTransport:
    """The end-to-end proof, over a real ``FastMCP`` instance: identity is
    enforced over a networked transport and never over stdio, no matter
    what ``LOOM_AUTH_*`` says."""

    def _facade(self):
        from workflow_builder import Context, Runtime, step, workflow
        from workflow_builder.facade import LocalFacade
        from workflow_builder.state.memory import MemoryStore

        @step
        async def double(n: int) -> int:
            return n * 2

        @workflow(name="doubler")
        async def doubler(ctx: Context, n: int) -> int:
            return await ctx.step(double, n)

        rt = Runtime(store=MemoryStore())
        rt.register_all([doubler])
        return LocalFacade(rt)

    async def test_stdio_ignores_identity_entirely(
        self, static_identity: IdentitySettings
    ) -> None:
        from workflow_builder.mcp_server import build_server

        server = build_server(
            self._facade(), identity=static_identity, transport="stdio"
        )
        # No AuthenticatedUser in context at all, and yet this must succeed —
        # stdio was never wrapped in the first place.
        result = await server.call_tool("list_workflows", {})
        assert result is not None

    async def test_http_transport_rejects_an_unauthenticated_call(
        self, static_identity: IdentitySettings
    ) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        from workflow_builder.mcp_server import build_server

        server = build_server(
            self._facade(), identity=static_identity, transport="streamable-http"
        )
        with pytest.raises(ToolError, match=r"InsufficientScope|does not hold"):
            await server.call_tool("list_workflows", {})

    async def test_http_transport_honours_the_callers_scopes(
        self, static_identity: IdentitySettings
    ) -> None:
        from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
        from mcp.server.auth.provider import AccessToken

        from workflow_builder.mcp_server import build_server

        server = build_server(
            self._facade(), identity=static_identity, transport="streamable-http"
        )
        access = AccessToken(
            token="tok-alice", client_id="", scopes=["workflows:read"], subject="alice"
        )
        reset = auth_context_var.set(AuthenticatedUser(access))
        try:
            result = await server.call_tool("list_workflows", {})
        finally:
            auth_context_var.reset(reset)

        assert result is not None

    async def test_a_run_started_by_one_principal_is_redacted_for_another(
        self, static_identity: IdentitySettings
    ) -> None:
        """The idempotent-start corner case, now through the real protocol."""
        from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
        from mcp.server.auth.provider import AccessToken

        from workflow_builder.mcp_server import build_server

        server = build_server(
            self._facade(), identity=static_identity, transport="streamable-http"
        )

        as_alice = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="tok-alice",
                    client_id="",
                    scopes=["runs:write", "runs:read", "workflows:read"],
                    subject="alice",
                )
            )
        )
        try:
            started = json.loads(
                (
                    await server.call_tool(
                        "run_workflow",
                        {
                            "workflow": "doubler",
                            "input_json": "21",
                            "idempotency_key": "shared",
                        },
                    )
                )[1]["result"]
            )
        finally:
            auth_context_var.reset(as_alice)

        as_bob = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="tok-bob-runs",
                    client_id="",
                    scopes=["runs:write", "runs:read", "workflows:read"],
                    subject="bob",
                )
            )
        )
        try:
            replayed = json.loads(
                (
                    await server.call_tool(
                        "run_workflow",
                        {
                            "workflow": "doubler",
                            "input_json": "21",
                            "idempotency_key": "shared",
                        },
                    )
                )[1]["result"]
            )
        finally:
            auth_context_var.reset(as_bob)

        assert started["run_id"] == replayed["run_id"]
        assert started["output"] == 42
        assert replayed["output"] is None
