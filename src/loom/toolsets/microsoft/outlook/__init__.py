"""Outlook toolsets — mail and calendar, over one Microsoft Graph layer.

Two separately-grantable toolsets rather than one "Outlook", for the reason the
Google package already states: a workflow reading a calendar has no business
holding a mail-send scope, and ``GrantSet(toolsets=["outlook_calendar"])``
should mean exactly that. They share ``models.py``, because Exchange's
vocabulary for a person and a time is the same on both sides.
"""

from __future__ import annotations
