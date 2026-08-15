"""LOOM as an MCP server.

Exposes a Runtime to any MCP client — Claude Code, Claude Desktop, Cursor — as
tools (run a workflow, approve a parked one, read a journal), resources
(``loom://workflows``, ``loom://runs/{id}``), and prompts (debug this run).

    pip install workflow-builder[mcp]
    loom mcp --module flows.py

The layering is deliberate:

``tools`` / ``resources`` / ``prompts``
    Pure functions over a :class:`RuntimeFacade`. No ``mcp`` import, so they are
    testable without a protocol.
``server``
    The only module that imports ``mcp``. Binds those functions to FastMCP.
``bridge``
    Deprecated. The old ``RuntimeBridge``, now a shim over the shared facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from workflow_builder.facade import RuntimeFacade
    from workflow_builder.identity.config import IdentitySettings

__all__ = ["build_server", "create_server", "serve"]

_MISSING = (
    "The MCP server needs the 'mcp' package. "
    "Install with: pip install 'workflow-builder[mcp]'"
)


def _require_mcp() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        raise ImportError(_MISSING) from None


def build_server(
    facade: RuntimeFacade,
    *,
    name: str = "loom",
    host: str = "127.0.0.1",
    port: int = 8000,
    scheduler: bool = True,
    identity: IdentitySettings | None = None,
    transport: str = "stdio",
) -> FastMCP:
    """Bind a :class:`RuntimeFacade` to a FastMCP server.

    ``identity`` defaults to reading ``LOOM_AUTH_*`` env vars; pass one
    explicitly to test auth without touching the environment. Unset, an
    install behaves exactly as it did before identity existed. ``transport``
    matters here too: auth only ever applies over a networked transport,
    never over ``stdio`` — see ``mcp_server/server.py::build_server`` for why.
    """
    _require_mcp()
    from workflow_builder.mcp_server.server import build_server as _build

    return _build(
        facade,
        name=name,
        host=host,
        port=port,
        scheduler=scheduler,
        identity=identity,
        transport=transport,
    )


def create_server(store_url: str = "memory://", name: str = "loom") -> Any:
    """Build a server over a bare Runtime. See :func:`build_server`."""
    _require_mcp()
    from workflow_builder.mcp_server.server import create_server as _create

    return _create(store_url=store_url, name=name)


def serve(
    facade: RuntimeFacade,
    *,
    name: str = "loom",
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    scheduler: bool = True,
    identity: IdentitySettings | None = None,
) -> None:
    """Run the server until the client disconnects.

    ``stdio`` is the default because that is what Claude Code and Claude Desktop
    speak; ``http`` (streamable HTTP) is for clients that connect over a network.
    ``host`` and ``port`` apply to those, and are ignored by stdio. ``scheduler``
    runs the timer loop while the server is up, so ``ctx.sleep()`` resumes.

    Refuses to bind a non-loopback host over a networked transport with no
    identity configured — that combination would serve every workflow this
    process can run to anyone who can reach the port, with no auth at all.
    stdio never opens a socket, so it is exempt regardless of ``host``.
    """
    from workflow_builder.identity.config import LOOPBACK_HOSTS, IdentitySettings

    identity = identity if identity is not None else IdentitySettings()
    if transport != "stdio" and not identity.is_configured() and host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host}:{port} over {transport} with no identity "
            "configured — that serves every workflow to anyone who can reach "
            "this port. Set LOOM_AUTH_JWKS_URI (or another LOOM_AUTH_* verifier "
            "config) or bind to a loopback host."
        )

    server = build_server(
        facade,
        name=name,
        host=host,
        port=port,
        scheduler=scheduler,
        identity=identity,
        transport=transport,
    )
    if transport == "stdio":
        server.run("stdio")
    elif transport in ("http", "streamable-http"):
        server.run("streamable-http")
    elif transport == "sse":
        server.run("sse")
    else:
        raise ValueError(
            f"unsupported transport {transport!r}; use stdio, http, or sse"
        )
