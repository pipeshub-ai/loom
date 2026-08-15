"""Gate for the MCP authoring tools.

Separate from :mod:`.server` so a caller can construct one without importing
``mcp`` — the same reason :mod:`.tools` and :mod:`.authoring` stay
protocol-free. Follows the same env-var-driven shape as
``identity.config.IdentitySettings``: unset, an install behaves exactly as if
this module did not exist (authoring tools registered, defaults applied).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["AuthoringConfig"]

_DISABLED_VALUES = frozenset({"0", "false", "no"})


@dataclass(frozen=True)
class AuthoringConfig:
    """Configuration for the MCP authoring tools.

    Env vars:
        ``LOOM_MCP_AUTHORING``: ``"0"``/``"false"``/``"no"`` disables the six
            authoring tools entirely. Default: enabled.
        ``LOOM_MCP_SMOKE_TIMEOUT``: seconds before a smoke-test subprocess is
            killed. Default: 30.
        ``LOOM_MCP_MAX_CODE_SIZE``: bytes of source ``validate_workflow_code``/
            ``smoke_test_workflow`` will accept before refusing. Default: 64000.
    """

    enabled: bool = True
    smoke_timeout: float = 30.0
    max_code_size: int = 64_000

    @classmethod
    def from_env(cls) -> AuthoringConfig:
        """Read gating from ``LOOM_MCP_*`` env vars; enabled by default."""
        enabled = os.environ.get("LOOM_MCP_AUTHORING", "1").strip().lower() not in (
            _DISABLED_VALUES
        )
        timeout = float(os.environ.get("LOOM_MCP_SMOKE_TIMEOUT", "30"))
        max_size = int(os.environ.get("LOOM_MCP_MAX_CODE_SIZE", "64000"))
        return cls(enabled=enabled, smoke_timeout=timeout, max_code_size=max_size)
