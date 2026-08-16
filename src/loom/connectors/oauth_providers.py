"""Pre-configured OAuth 2.0 endpoints for well-known providers.

Authorization and token URLs are public knowledge. Users should only have
to supply ``client_id`` and ``client_secret``. Custom providers register
with :func:`register_oauth_provider`; OIDC issuers can be discovered with
:func:`discover_oidc`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "OAuthProviderConfig",
    "discover_oidc",
    "get_oauth_provider",
    "list_oauth_providers",
    "register_oauth_provider",
]


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Pre-configured OAuth endpoints for a well-known provider.

    Users supply ``client_id`` and ``client_secret``; everything else is
    known for each provider. ``extra_auth_params`` are merged into the
    authorization URL (Google's ``access_type=offline`` is how a refresh
    token is issued at all).
    """

    id: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    device_authorization_endpoint: str | None = None
    default_scopes: tuple[str, ...] = ()
    supports_pkce: bool = True
    discovery_url: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    docs_url: str = ""


_PROVIDERS: dict[str, OAuthProviderConfig] = {}
_builtins_loaded = False


def register_oauth_provider(config: OAuthProviderConfig) -> None:
    """Register a custom provider, or override a built-in one."""
    _ensure_builtins()
    _PROVIDERS[config.id] = config


def get_oauth_provider(provider_id: str) -> OAuthProviderConfig | None:
    _ensure_builtins()
    return _PROVIDERS.get(provider_id)


def list_oauth_providers() -> list[OAuthProviderConfig]:
    _ensure_builtins()
    return [_PROVIDERS[key] for key in sorted(_PROVIDERS)]


async def discover_oidc(issuer: str) -> OAuthProviderConfig:
    """Fetch ``/.well-known/openid-configuration`` and build a config."""
    import httpx

    base = issuer.rstrip("/")
    url = f"{base}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    host = base.split("//", 1)[-1].split("/", 1)[0].replace(".", "_")
    methods = data.get("code_challenge_methods_supported") or []
    return OAuthProviderConfig(
        id=host,
        display_name=issuer,
        authorization_endpoint=str(data["authorization_endpoint"]),
        token_endpoint=str(data["token_endpoint"]),
        device_authorization_endpoint=(
            str(data["device_authorization_endpoint"])
            if data.get("device_authorization_endpoint")
            else None
        ),
        supports_pkce="S256" in methods or not methods,
        discovery_url=url,
    )


def _ensure_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    for config in _BUILTIN_PROVIDERS:
        _PROVIDERS.setdefault(config.id, config)


_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_DEVICE = "https://oauth2.googleapis.com/device/code"
_GOOGLE_OFFLINE = {"access_type": "offline", "prompt": "consent"}

_BUILTIN_PROVIDERS = (
    OAuthProviderConfig(
        id="google",
        display_name="Google",
        authorization_endpoint=_GOOGLE_AUTH,
        token_endpoint=_GOOGLE_TOKEN,
        device_authorization_endpoint=_GOOGLE_DEVICE,
        default_scopes=(
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ),
        extra_auth_params=dict(_GOOGLE_OFFLINE),
        docs_url="https://developers.google.com/identity/protocols/oauth2",
    ),
    OAuthProviderConfig(
        id="google_gmail",
        display_name="Google (Gmail)",
        authorization_endpoint=_GOOGLE_AUTH,
        token_endpoint=_GOOGLE_TOKEN,
        device_authorization_endpoint=_GOOGLE_DEVICE,
        default_scopes=(
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ),
        extra_auth_params=dict(_GOOGLE_OFFLINE),
        docs_url="https://developers.google.com/gmail/api/auth/scopes",
    ),
    OAuthProviderConfig(
        id="google_calendar",
        display_name="Google (Calendar)",
        authorization_endpoint=_GOOGLE_AUTH,
        token_endpoint=_GOOGLE_TOKEN,
        device_authorization_endpoint=_GOOGLE_DEVICE,
        default_scopes=("https://www.googleapis.com/auth/calendar",),
        extra_auth_params=dict(_GOOGLE_OFFLINE),
        docs_url="https://developers.google.com/calendar/api/auth",
    ),
    OAuthProviderConfig(
        id="atlassian",
        display_name="Atlassian (Jira/Confluence)",
        authorization_endpoint="https://auth.atlassian.com/authorize",
        token_endpoint="https://auth.atlassian.com/oauth/token",
        default_scopes=("read:jira-work", "write:jira-work", "read:confluence-content.all"),
        extra_auth_params={"audience": "api.atlassian.com", "prompt": "consent"},
        docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
    ),
    OAuthProviderConfig(
        id="github",
        display_name="GitHub",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        device_authorization_endpoint="https://github.com/login/device/code",
        default_scopes=("repo", "read:user"),
        supports_pkce=False,
        docs_url="https://docs.github.com/en/apps/oauth-apps",
    ),
    OAuthProviderConfig(
        id="slack",
        display_name="Slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        default_scopes=("chat:write", "channels:read"),
        supports_pkce=False,
        docs_url="https://api.slack.com/authentication/oauth-v2",
    ),
    OAuthProviderConfig(
        id="zoom",
        display_name="Zoom",
        authorization_endpoint="https://zoom.us/oauth/authorize",
        token_endpoint="https://zoom.us/oauth/token",
        default_scopes=("meeting:read", "meeting:write", "user:read"),
        supports_pkce=True,
        docs_url="https://developers.zoom.us/docs/integrations/oauth/",
    ),
    OAuthProviderConfig(
        id="microsoft",
        display_name="Microsoft (Azure AD)",
        authorization_endpoint=(
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        ),
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        device_authorization_endpoint=(
            "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
        ),
        default_scopes=("openid", "profile", "email", "offline_access"),
        docs_url=(
            "https://learn.microsoft.com/en-us/entra/identity-platform/"
            "v2-oauth2-auth-code-flow"
        ),
    ),
    OAuthProviderConfig(
        id="linear",
        display_name="Linear",
        authorization_endpoint="https://linear.app/oauth/authorize",
        token_endpoint="https://api.linear.app/oauth/token",
        default_scopes=("read", "write"),
    ),
    OAuthProviderConfig(
        id="notion",
        display_name="Notion",
        authorization_endpoint="https://api.notion.com/v1/oauth/authorize",
        token_endpoint="https://api.notion.com/v1/oauth/token",
        supports_pkce=False,
    ),
    OAuthProviderConfig(
        id="hubspot",
        display_name="HubSpot",
        authorization_endpoint="https://app.hubspot.com/oauth/authorize",
        token_endpoint="https://api.hubapi.com/oauth/v1/token",
        default_scopes=("crm.objects.contacts.read",),
        supports_pkce=False,
        docs_url="https://developers.hubspot.com/docs/api/oauth-quickstart-guide",
    ),
)
