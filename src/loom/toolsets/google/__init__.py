"""Google Workspace toolsets — Gmail, Calendar, Drive, and Meet.

Four separately-grantable toolsets over one OAuth layer. They are separate
because a workflow that reads a calendar has no business holding a mail-send
scope or a Drive delete scope, and ``GrantSet(toolsets=["google_calendar"])``
should mean exactly that. They share :mod:`loom.toolsets.google.auth` because
they authenticate against the same account, and one cached token serves all
four.

Credentials come from the environment — see that module for the three forms.
Importing anything here reads none of them.

**Where the four meet.** Two seams are worth knowing before writing a workflow
across them, because in both cases the obvious toolset is the wrong one:

- **Scheduling a meeting is a Calendar operation.** The Meet API cannot
  schedule anything; ``calendar_create_event(..., add_meet=True)`` is what
  creates a meeting with a time, invitees, and a Meet link.
- **A meeting's recording and transcript live in Drive.** Meet reports their
  ids; the Drive toolset fetches the bytes — ``drive_download_file`` for a
  recording, ``drive_export_file`` for a transcript, which is a Google Doc and
  therefore has no bytes to download.
"""

from __future__ import annotations

from loom.toolsets.google.auth import (
    GoogleAuth,
    GoogleCredentials,
    get_default_auth,
)
from loom.toolsets.google.calendar import GOOGLE_CALENDAR_MANIFEST
from loom.toolsets.google.drive import GOOGLE_DRIVE_MANIFEST
from loom.toolsets.google.errors import (
    GoogleAPIError,
    GoogleAuthError,
    GooglePermanentError,
    GoogleRateLimited,
)
from loom.toolsets.google.gmail import GMAIL_MANIFEST
from loom.toolsets.google.meet import GOOGLE_MEET_MANIFEST
from loom.toolsets.google.sheets.manifest import GOOGLE_SHEETS_MANIFEST

#: Every Google toolset LOOM ships, for registering them in one line:
#: ``for manifest in GOOGLE_MANIFESTS: registry.register(manifest)``.
GOOGLE_MANIFESTS = (
    GMAIL_MANIFEST,
    GOOGLE_CALENDAR_MANIFEST,
    GOOGLE_DRIVE_MANIFEST,
    GOOGLE_MEET_MANIFEST,
    GOOGLE_SHEETS_MANIFEST,
)

__all__ = [
    "GMAIL_MANIFEST",
    "GOOGLE_CALENDAR_MANIFEST",
    "GOOGLE_DRIVE_MANIFEST",
    "GOOGLE_MANIFESTS",
    "GOOGLE_MEET_MANIFEST",
    "GOOGLE_SHEETS_MANIFEST",
    "GoogleAPIError",
    "GoogleAuth",
    "GoogleAuthError",
    "GoogleCredentials",
    "GooglePermanentError",
    "GoogleRateLimited",
    "get_default_auth",
]
