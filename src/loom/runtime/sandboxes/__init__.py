"""Adapters for :class:`~loom.runtime.sandbox.ExecutionSandbox`.

The port and its free default (``InlineSandbox``) live in
``loom.runtime.sandbox``; anything that isolates lives here, one module per
mechanism. Kept separate because an isolating sandbox has platform-specific
imports — ``resource`` is POSIX-only — and the port must import on every
platform Loom runs on.

Nothing is re-exported eagerly: ``SubprocessSandbox`` is imported from its own
module, the same way a store backend is, so the package costs nothing to have.
"""

from __future__ import annotations

__all__ = ["SubprocessSandbox"]


def __getattr__(name: str) -> object:
    """Import the adapter on first use, so a platform-specific one only loads
    for a caller that actually asked for it."""
    if name == "SubprocessSandbox":
        from loom.runtime.sandboxes.subprocess import SubprocessSandbox

        return SubprocessSandbox
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
