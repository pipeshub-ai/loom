"""LOOM MCP Server -- exposes workflows to MCP clients."""
from __future__ import annotations

from typing import Any


def create_server(
    store_url: str = "memory://",
    name: str = "loom",
) -> Any:
    """Create and configure the LOOM MCP server.

    Requires ``mcp`` package: pip install workflow-builder[mcp]
    """
    try:
        from mcp.server import Server  # type: ignore[import-untyped]
    except ImportError:
        msg = (
            "MCP server requires the 'mcp' package. "
            "Install with: pip install workflow-builder[mcp]"
        )
        raise ImportError(msg) from None

    from workflow_builder.mcp_server.bridge import RuntimeBridge
    from workflow_builder.mcp_server.prompts import register_prompts
    from workflow_builder.mcp_server.resources import register_resources
    from workflow_builder.mcp_server.tools import register_tools

    server = Server(name)
    bridge = RuntimeBridge(store_url=store_url)
    register_tools(server, bridge)
    register_resources(server, bridge)
    register_prompts(server, bridge)
    return server
