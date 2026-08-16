"""Outlook calendar toolset — calendars, events, and availability."""

from __future__ import annotations

from loom.toolsets.microsoft.outlook.calendar.client import (
    OutlookCalendarClient,
    get_default_client,
    reset_default_client,
)

__all__ = ["OutlookCalendarClient", "get_default_client", "reset_default_client"]
