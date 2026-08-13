"""Command-line entry point.

Installed as both ``loom`` and ``workflow-builder``.

Commands fall into four groups: authoring a workflow, running one, acting on a
run that already exists, and serving. Everything that touches a run works
identically against an in-process Runtime and a remote server — pass
``--server URL`` to switch, and nothing else changes.

Exit codes are part of the contract:

======  ==========================================================
``0``   completed
``1``   failed
``2``   usage error
``3``   suspended — parked on a timer or a human, neither done nor failed
``4``   cancelled
======  ==========================================================
"""

from __future__ import annotations

import argparse
import sys

from workflow_builder import __version__
from workflow_builder.cli import commands
from workflow_builder.cli.output import Exit

__all__ = ["main"]

_HANDLERS = {
    "check": commands.cmd_check,
    "graph": commands.cmd_graph,
    "describe": commands.cmd_describe,
    "init": commands.cmd_init,
    "run": commands.cmd_run,
    "runs": commands.cmd_runs,
    "show": commands.cmd_show,
    "watch": commands.cmd_watch,
    "approve": commands.cmd_approve,
    "send": commands.cmd_send,
    "cancel": commands.cmd_cancel,
    "retry": commands.cmd_retry,
    "replay": commands.cmd_replay,
    "workflows": commands.cmd_workflows,
    "publish": commands.cmd_publish,
    "serve": commands.cmd_serve,
    "mcp": commands.cmd_mcp,
    "ui": commands.cmd_ui,
}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return Exit.OK
    return int(_HANDLERS[args.command](args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loom",
        description="Author, run, and inspect LOOM workflows.",
        epilog="Exit codes: 0 completed, 1 failed, 2 usage, 3 suspended, 4 cancelled.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    _authoring(sub)
    _running(sub)
    _acting(sub)
    _serving(sub)
    return parser


# ---------------------------------------------------------------------------
# Shared argument groups
# ---------------------------------------------------------------------------


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON only"
    )


def _add_backend(parser: argparse.ArgumentParser) -> None:
    """Flags that decide which Runtime a command talks to."""
    parser.add_argument(
        "--server",
        metavar="URL",
        help="Operate against a running LOOM server instead of importing locally",
    )
    parser.add_argument(
        "--module",
        "-m",
        action="append",
        metavar="MOD",
        help="Import this module to find workflows (repeatable). "
        "Defaults to [tool.loom] modules in pyproject.toml",
    )


def _add_run_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id", help="Run identifier")
    _add_backend(parser)
    _add_output(parser)


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


def _authoring(sub: argparse._SubParsersAction) -> None:
    check = sub.add_parser(
        "check", help="Write <flow>.graph.json and <flow>.description.md"
    )
    check.add_argument("path", type=_path, help="Workflow source file")
    check.add_argument(
        "--no-write", action="store_true", help="Report changes without writing"
    )
    check.add_argument(
        "--fail-on-change",
        action="store_true",
        help="Exit non-zero if the graph differs from the committed one (for CI)",
    )
    _add_output(check)

    graph = sub.add_parser("graph", help="Print the workflow graph")
    graph.add_argument("path", type=_path, help="Workflow source file")
    graph.add_argument(
        "--format",
        choices=("mermaid", "json", "react-flow"),
        default="mermaid",
        help="Output format (default: mermaid)",
    )
    _add_output(graph)

    describe = sub.add_parser("describe", help="Print the narrated description")
    describe.add_argument("path", type=_path, help="Workflow source file")
    _add_output(describe)

    init = sub.add_parser("init", help="Scaffold a new workflow project")
    init.add_argument("directory", type=_path, help="Directory to create files in")
    init.add_argument(
        "--dry-run", action="store_true", help="List files without writing them"
    )
    _add_output(init)


def _running(sub: argparse._SubParsersAction) -> None:
    run = sub.add_parser("run", help="Run a workflow")
    run.add_argument(
        "target", help="Workflow name, or path.py::name"
    )
    run.add_argument(
        "--input",
        "-i",
        metavar="JSON",
        help="Input as JSON, @file.json, or a bare string",
    )
    run.add_argument(
        "--follow", "-f", action="store_true", help="Stream steps as they complete"
    )
    run.add_argument(
        "--detach", "-d", action="store_true", help="Start it and return immediately"
    )
    run.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Reuse to make a retried invocation return the original run",
    )
    _add_backend(run)
    _add_output(run)

    runs = sub.add_parser("runs", help="List runs")
    runs.add_argument("--workflow", "-w", help="Only this workflow")
    runs.add_argument(
        "--status",
        "-s",
        choices=("pending", "running", "suspended", "completed", "failed", "cancelled"),
        help="Only this status",
    )
    runs.add_argument("--limit", "-n", type=int, default=50, help="Maximum rows")
    _add_backend(runs)
    _add_output(runs)

    show = sub.add_parser("show", help="Show one run and its journal")
    _add_run_id(show)

    watch = sub.add_parser("watch", help="Follow a run until it settles")
    watch.add_argument("run_id", help="Run identifier")
    watch.add_argument(
        "--timeout", type=float, default=3600.0, help="Give up after this many seconds"
    )
    _add_backend(watch)
    _add_output(watch)


def _acting(sub: argparse._SubParsersAction) -> None:
    approve = sub.add_parser("approve", help="Resolve a pending human approval")
    approve.add_argument("run_id", help="Run identifier")
    approve.add_argument("subject", help="Approval subject, e.g. 'refund'")
    approve.add_argument(
        "--reject", action="store_true", help="Deny instead of approving"
    )
    _add_backend(approve)
    _add_output(approve)

    send = sub.add_parser("send", help="Deliver an event to a parked run")
    send.add_argument("run_id", help="Run identifier")
    send.add_argument("event", help="Event name")
    send.add_argument(
        "payload", nargs="?", help="Payload as JSON, @file.json, or a bare string"
    )
    _add_backend(send)
    _add_output(send)

    cancel = sub.add_parser("cancel", help="Cancel a run")
    cancel.add_argument("run_id", help="Run identifier")
    cancel.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    _add_backend(cancel)
    _add_output(cancel)

    retry = sub.add_parser("retry", help="Re-run a failed execution from its failure")
    _add_run_id(retry)

    replay = sub.add_parser("replay", help="Re-execute from the journal, repeating nothing")
    _add_run_id(replay)


def _serving(sub: argparse._SubParsersAction) -> None:
    workflows = sub.add_parser("workflows", help="List available workflows")
    _add_backend(workflows)
    _add_output(workflows)

    publish = sub.add_parser("publish", help="Record a workflow in the catalog")
    publish.add_argument("target", help="Workflow name, or path.py::name")
    _add_backend(publish)
    _add_output(publish)

    serve = sub.add_parser("serve", help="Start the HTTP API")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve.add_argument("--port", type=int, default=8000, help="Bind port")
    serve.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug"),
    )
    _add_backend(serve)
    _add_output(serve)

    mcp = sub.add_parser(
        "mcp",
        help="Serve this Runtime over MCP (Claude Code, Claude Desktop, Cursor)",
    )
    mcp.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default="stdio",
        help="stdio for desktop clients (default), http for networked ones",
    )
    mcp.add_argument("--name", default="loom", help="Server name shown to clients")
    mcp.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Do not run the timer loop. Without it ctx.sleep() never resumes; "
        "use only when another process schedules this store",
    )
    mcp.add_argument(
        "--host", default="127.0.0.1", help="Bind address (http and sse transports)"
    )
    mcp.add_argument(
        "--port", type=int, default=8000, help="Bind port (http and sse transports)"
    )
    _add_backend(mcp)
    _add_output(mcp)

    ui = sub.add_parser("ui", help="Launch the terminal UI")
    _add_backend(ui)
    _add_output(ui)


def _path(value: str):
    from pathlib import Path

    return Path(value)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
