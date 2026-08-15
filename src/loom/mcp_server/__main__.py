"""Entry point: ``python -m loom.mcp_server``.

Equivalent to ``loom mcp``, kept so the server can be launched without the CLI
extra installed. Both delegate to the same resolution and serving code.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from loom.cli import build_parser
    from loom.cli.commands import cmd_mcp

    args = build_parser().parse_args(["mcp", *(argv if argv is not None else sys.argv[1:])])
    return int(cmd_mcp(args))


if __name__ == "__main__":
    sys.exit(main())
