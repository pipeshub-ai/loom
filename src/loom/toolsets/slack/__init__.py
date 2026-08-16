"""Slack toolset.

Lazy: importing this package needs no credentials and pulls in no vendor SDK.
The client reads the environment, or the run's connected credential, when a
tool is first called.
"""

from __future__ import annotations

from loom.toolsets.slack.errors import (
    SlackAPIError,
    SlackAuthError,
    SlackMissingScope,
    SlackPermanentError,
    SlackRateLimited,
)
from loom.toolsets.slack.manifest import SLACK_MANIFEST
from loom.toolsets.slack.models import (
    PostedMessage,
    SlackChannel,
    SlackFileRef,
    SlackMessage,
    SlackUser,
)

__all__ = [
    "SLACK_MANIFEST",
    "PostedMessage",
    "SlackAPIError",
    "SlackAuthError",
    "SlackChannel",
    "SlackFileRef",
    "SlackMessage",
    "SlackMissingScope",
    "SlackPermanentError",
    "SlackRateLimited",
    "SlackUser",
]
