"""Gmail toolset.

Lazy: importing this package needs no credentials and pulls in no vendor SDK.
The client reads the environment when a tool is first called.
"""

from __future__ import annotations

from workflow_builder.toolsets.google.gmail.manifest import GMAIL_MANIFEST
from workflow_builder.toolsets.google.gmail.models import (
    AttachmentRef,
    EmailMessage,
    GmailLabel,
    GmailProfile,
    MessageRef,
    SentMessage,
)

__all__ = [
    "GMAIL_MANIFEST",
    "AttachmentRef",
    "EmailMessage",
    "GmailLabel",
    "GmailProfile",
    "MessageRef",
    "SentMessage",
]
