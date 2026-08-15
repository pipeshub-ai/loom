"""Google Workspace toolsets — Gmail and Calendar.

Two separately-grantable toolsets over one OAuth layer. They are separate
because a workflow that reads a calendar has no business holding a mail-send
scope, and ``GrantSet(toolsets=["google_calendar"])`` should mean exactly that.
They share :mod:`loom.toolsets.google.auth` because they
authenticate against the same account, and one cached token serves both.

Credentials come from the environment — see that module for the three forms.
Importing anything here reads none of them.
"""

from __future__ import annotations

from loom.toolsets.google.auth import (
    GoogleAuth,
    GoogleCredentials,
    get_default_auth,
)
from loom.toolsets.google.calendar import GOOGLE_CALENDAR_MANIFEST
from loom.toolsets.google.errors import (
    GoogleAPIError,
    GoogleAuthError,
    GooglePermanentError,
    GoogleRateLimited,
)
from loom.toolsets.google.gmail import GMAIL_MANIFEST

__all__ = [
    "GMAIL_MANIFEST",
    "GOOGLE_CALENDAR_MANIFEST",
    "GoogleAPIError",
    "GoogleAuth",
    "GoogleAuthError",
    "GoogleCredentials",
    "GooglePermanentError",
    "GoogleRateLimited",
    "get_default_auth",
]
