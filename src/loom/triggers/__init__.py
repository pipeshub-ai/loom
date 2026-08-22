"""Trigger declarations: how the outside world starts a workflow."""

from __future__ import annotations

from loom.triggers.base import TriggerBinding, TriggerEvent, TriggerSpec
from loom.triggers.cron import CronError, CronSchedule
from loom.triggers.specs import (
    After,
    AuthMode,
    CalledByWorkflow,
    Chat,
    EmailInbox,
    Form,
    FormField,
    Interval,
    Manual,
    OnEvent,
    OnFailure,
    Poll,
    ResponseMode,
    Schedule,
    Webhook,
    describe_all,
)

__all__ = [
    "After",
    "AuthMode",
    "CalledByWorkflow",
    "Chat",
    "CronError",
    "CronSchedule",
    "EmailInbox",
    "Form",
    "FormField",
    "Interval",
    "Manual",
    "OnEvent",
    "OnFailure",
    "Poll",
    "ResponseMode",
    "Schedule",
    "TriggerBinding",
    "TriggerEvent",
    "TriggerSpec",
    "Webhook",
    "describe_all",
]
