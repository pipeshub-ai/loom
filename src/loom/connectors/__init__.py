"""Outbound credentials — what LOOM authenticates *with* to call Jira, Google,
and other third-party APIs on a run's behalf.

This is the other half of a deliberate split from :mod:`loom.identity`
(inbound — who is calling a LOOM surface). They never meet: a bearer token that
arrives at the MCP server is never forwarded to a toolset, it only selects which
stored credential the run may use. See the module docs on
:class:`~loom.connectors.credentials.CredentialStore` for the port
and its reference implementations.
"""

from __future__ import annotations

from loom.connectors.credentials import (
    CredentialStore,
    EncryptedFileCredentialStore,
    KeyringCredentialStore,
    LayeredCredentialStore,
    MemoryCredentialStore,
    Peekable,
    Refresher,
    StoredCredential,
)
from loom.connectors.oauth_client import (
    DeviceAuthorization,
    MetadataRefresher,
    OAuthClient,
)
from loom.connectors.oauth_providers import (
    OAuthProviderConfig,
    discover_oidc,
    get_oauth_provider,
    list_oauth_providers,
    register_oauth_provider,
)

__all__ = [
    "CredentialStore",
    "DeviceAuthorization",
    "EncryptedFileCredentialStore",
    "KeyringCredentialStore",
    "LayeredCredentialStore",
    "MemoryCredentialStore",
    "MetadataRefresher",
    "OAuthClient",
    "OAuthProviderConfig",
    "Peekable",
    "Refresher",
    "StoredCredential",
    "discover_oidc",
    "get_oauth_provider",
    "list_oauth_providers",
    "register_oauth_provider",
]
