"""Microsoft Teams toolset — teams, channels, messages, and chats."""

from __future__ import annotations

from loom.toolsets.microsoft.teams.client import (
    MESSAGE_PAGE_MAX,
    TeamsClient,
    get_default_client,
    reset_default_client,
)

__all__ = [
    "MESSAGE_PAGE_MAX",
    "TeamsClient",
    "get_default_client",
    "reset_default_client",
]
