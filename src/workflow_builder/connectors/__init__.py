"""Outbound credentials — what LOOM authenticates *with* to call Jira, Google,
and other third-party APIs on a run's behalf.

This is the other half of a deliberate split from :mod:`workflow_builder.identity`
(inbound — who is calling a LOOM surface). They never meet: a bearer token that
arrives at the MCP server is never forwarded to a toolset, it only selects which
stored credential the run may use. See the module docs on
:class:`~workflow_builder.connectors.credentials.CredentialStore` for the port
and its reference implementations.
"""

from __future__ import annotations

from workflow_builder.connectors.credentials import (
    CredentialStore,
    EncryptedFileCredentialStore,
    KeyringCredentialStore,
    MemoryCredentialStore,
    Peekable,
    Refresher,
    StoredCredential,
)
from workflow_builder.connectors.oauth_client import (
    DeviceAuthorization,
    MetadataRefresher,
    OAuthClient,
)

__all__ = [
    "CredentialStore",
    "DeviceAuthorization",
    "EncryptedFileCredentialStore",
    "KeyringCredentialStore",
    "MemoryCredentialStore",
    "MetadataRefresher",
    "OAuthClient",
    "Peekable",
    "Refresher",
    "StoredCredential",
]
