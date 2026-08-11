"""Entry point: python -m workflow_builder.mcp_server"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    """Run the LOOM MCP server."""
    parser = argparse.ArgumentParser(
        description="LOOM MCP Server",
    )
    parser.add_argument(
        "--store",
        default="memory://",
        help="Store URL (default: memory://)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport (default: stdio)",
    )
    parser.add_argument(
        "--name",
        default="loom",
        help="Server name",
    )
    args = parser.parse_args()

    try:
        from workflow_builder.mcp_server import create_server

        create_server(store_url=args.store, name=args.name)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"LOOM MCP server '{args.name}' ready ({args.transport})"
    )


if __name__ == "__main__":
    main()
