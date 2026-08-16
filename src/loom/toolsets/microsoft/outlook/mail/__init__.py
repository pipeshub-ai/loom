"""Outlook mail toolset — messages, folders, and sending."""

from __future__ import annotations

from loom.toolsets.microsoft.outlook.mail.client import (
    MESSAGE_FIELDS,
    OutlookMailClient,
    get_default_client,
    reset_default_client,
)

__all__ = [
    "MESSAGE_FIELDS",
    "OutlookMailClient",
    "get_default_client",
    "reset_default_client",
]
