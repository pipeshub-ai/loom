"""Microsoft Graph toolsets — OneDrive and SharePoint Online.

One API, one token, one paging dialect, two separately-grantable toolsets. The
shared layer lives here; the products are sub-packages:

``loom.toolsets.microsoft.onedrive``
    A person's or an app's files: browse, search, upload, download, share.
``loom.toolsets.microsoft.sharepoint``
    Sites, document libraries, and lists.

They stay separate toolsets because the grant boundary is real — a workflow
reading a team's SharePoint library has no business reading an individual's
OneDrive, and ``GrantSet(toolsets=["sharepoint"])`` is how that gets said.

Importing this package pulls in no vendor SDK and performs no I/O; everything
below is plain ``httpx``.
"""

from __future__ import annotations

from loom.toolsets.microsoft.auth import (
    AUTHORITY_HOST,
    GRAPH_BASE_URL,
    MicrosoftAuth,
    MicrosoftCredentials,
    get_default_auth,
    reset_default_auth,
)
from loom.toolsets.microsoft.errors import (
    GraphAPIError,
    GraphAuthError,
    GraphPermanentError,
    GraphThrottled,
    classify,
)
from loom.toolsets.microsoft.http import GraphSession

__all__ = [
    "AUTHORITY_HOST",
    "GRAPH_BASE_URL",
    "GraphAPIError",
    "GraphAuthError",
    "GraphPermanentError",
    "GraphSession",
    "GraphThrottled",
    "MicrosoftAuth",
    "MicrosoftCredentials",
    "classify",
    "get_default_auth",
    "reset_default_auth",
]
