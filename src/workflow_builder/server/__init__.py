"""HTTP surface for a Runtime, and a client for it.

``create_app`` needs the ``api`` extra (``pip install workflow-builder[api]``);
``LoomClient`` needs ``httpx``. Both are imported lazily so the rest of the SDK
stays installable without either.
"""

from __future__ import annotations

from workflow_builder.server.client import LoomClient, LoomClientError

__all__ = ["LoomClient", "LoomClientError", "create_app"]


def __getattr__(name: str) -> object:
    """Defer the FastAPI import until someone actually builds an app."""
    if name == "create_app":
        from workflow_builder.server.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
