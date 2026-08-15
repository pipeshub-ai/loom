"""Pre-configured OAuth provider registry."""

from __future__ import annotations

import httpx
import pytest

from loom.connectors.oauth_providers import (
    OAuthProviderConfig,
    discover_oidc,
    get_oauth_provider,
    list_oauth_providers,
    register_oauth_provider,
)

_EXPECTED = {
    "google",
    "google_gmail",
    "google_calendar",
    "atlassian",
    "github",
    "slack",
    "microsoft",
    "linear",
    "notion",
    "hubspot",
}


class TestOAuthProviderRegistry:
    def test_builtins_are_complete(self) -> None:
        ids = {p.id for p in list_oauth_providers()}
        assert ids >= _EXPECTED

    def test_google_requests_offline_access(self) -> None:
        google = get_oauth_provider("google")
        assert google is not None
        assert google.extra_auth_params["access_type"] == "offline"
        assert google.supports_pkce is True

    def test_github_slack_notion_hubspot_are_non_pkce(self) -> None:
        for provider_id in ("github", "slack", "notion", "hubspot"):
            config = get_oauth_provider(provider_id)
            assert config is not None
            assert config.supports_pkce is False

    def test_atlassian_sends_audience(self) -> None:
        atlassian = get_oauth_provider("atlassian")
        assert atlassian is not None
        assert atlassian.extra_auth_params["audience"] == "api.atlassian.com"

    def test_unknown_provider_is_none(self) -> None:
        assert get_oauth_provider("not-real") is None

    def test_register_oauth_provider_overrides(self) -> None:
        register_oauth_provider(
            OAuthProviderConfig(
                id="custom_test_provider",
                display_name="Custom",
                authorization_endpoint="https://example.test/auth",
                token_endpoint="https://example.test/token",
            )
        )
        found = get_oauth_provider("custom_test_provider")
        assert found is not None
        assert found.display_name == "Custom"


class TestDiscoverOidc:
    async def test_builds_a_config_from_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "authorization_endpoint": "https://idp.example/authorize",
            "token_endpoint": "https://idp.example/token",
            "device_authorization_endpoint": "https://idp.example/device",
            "code_challenge_methods_supported": ["S256"],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/.well-known/openid-configuration")
            return httpx.Response(200, json=payload)

        original = httpx.AsyncClient.__init__

        def patched(self: httpx.AsyncClient, **kwargs: object) -> None:
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            original(self, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        config = await discover_oidc("https://idp.example")
        assert config.authorization_endpoint == "https://idp.example/authorize"
        assert config.token_endpoint == "https://idp.example/token"
        assert config.supports_pkce is True
