"""Command-line entry point.

Installed as both ``loom`` and ``loomflow``.

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
import signal
import sys

from loom import __version__
from loom.cli import auth_commands, commands, mcp_setup
from loom.cli.output import Exit

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
    "pending": commands.cmd_pending,
    "respond": commands.cmd_respond,
    "toolsets": commands.cmd_toolsets,
    "toolset": commands.cmd_toolset,
    "nodes": commands.cmd_nodes,
    "node": commands.cmd_node,
    "send": commands.cmd_send,
    "cancel": commands.cmd_cancel,
    "retry": commands.cmd_retry,
    "replay": commands.cmd_replay,
    "workflows": commands.cmd_workflows,
    "publish": commands.cmd_publish,
    "serve": commands.cmd_serve,
    "mcp": commands.cmd_mcp,
    "ui": commands.cmd_ui,
    "artifacts": commands.cmd_artifacts,
    "login": auth_commands.cmd_login,
    "logout": auth_commands.cmd_logout,
    "whoami": auth_commands.cmd_whoami,
    "refresh": auth_commands.cmd_refresh,
    "connect": auth_commands.cmd_connect,
    "disconnect": auth_commands.cmd_disconnect,
    "providers": auth_commands.cmd_providers,
    "setup": mcp_setup.cmd_setup,
}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code.

    Wrapped so that no window is uncovered. ``run_async`` handles a signal that
    lands while a command's event loop is running, which is most of them; this
    covers the rest — the startup imports (long enough that a Ctrl+C there used
    to print forty frames of ``dataclasses.py``), argument parsing, and the
    commands that never open a loop at all.

    ``terminate_on`` is a fallback, not a claim on the process: a server started
    below installs its own handlers and gets them back on the way out. uvicorn
    and FastMCP both do, and both already shut down cleanly.
    """
    from loom.runtime.shutdown import Interrupted, terminate_on

    with terminate_on(signal.SIGTERM):
        try:
            return _dispatch(argv)
        except Interrupted as stop:
            commands.interrupted(stop.exit_code)
            return stop.exit_code
        except KeyboardInterrupt:
            commands.interrupted(Exit.INTERRUPTED)
            return Exit.INTERRUPTED


def _dispatch(argv: list[str] | None) -> int:
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
    _authenticating(sub)
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
    run.add_argument(
        "--env",
        action="append",
        metavar="KEY=VAL",
        help="Per-run environment override (repeatable). Not for secrets.",
    )
    run.add_argument(
        "--env-file",
        metavar="PATH",
        help="Load KEY=VAL lines as per-run environment overrides",
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

    artifacts = sub.add_parser("artifacts", help="List, show, or download artifacts")
    artifacts.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "show", "download"),
        help="list (default), show NAME, or download NAME",
    )
    artifacts.add_argument("name", nargs="?", help="Artifact name")
    artifacts.add_argument("--version", type=int, default=None, help="Specific version")
    artifacts.add_argument(
        "--output", "-o", help="Path to write downloaded bytes (download only)"
    )
    _add_backend(artifacts)
    _add_output(artifacts)


def _acting(sub: argparse._SubParsersAction) -> None:
    approve = sub.add_parser("approve", help="Resolve a pending human approval")
    approve.add_argument("run_id", help="Run identifier")
    approve.add_argument("subject", help="Approval subject, e.g. 'refund'")
    approve.add_argument(
        "--reject", action="store_true", help="Deny instead of approving"
    )
    _add_backend(approve)
    _add_output(approve)

    pending = sub.add_parser(
        "pending", help="List runs parked on a person, and what each is asked"
    )
    pending.add_argument(
        "run_id", nargs="?", help="Only this run, instead of every parked one"
    )
    _add_backend(pending)
    _add_output(pending)

    respond = sub.add_parser(
        "respond", help="Answer a parked human request with a typed payload"
    )
    respond.add_argument("run_id", help="Run identifier")
    respond.add_argument("subject", help="Request subject, e.g. 'refund'")
    respond.add_argument(
        "payload", nargs="?", help="Answer as JSON, @file.json, or a bare string"
    )
    respond.add_argument("--approve", action="store_true", help="Set approved=true")
    respond.add_argument("--reject", action="store_true", help="Set approved=false")
    respond.add_argument(
        "--select", action="append", help="Choose an option (repeatable)"
    )
    respond.add_argument("--comment", help="Free-text note recorded with the answer")
    respond.add_argument("--responder", help="Who answered, recorded on the run")
    _add_backend(respond)
    _add_output(respond)

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

    toolsets = sub.add_parser(
        "toolsets", help="List integrations a workflow or agent can call"
    )
    toolsets.add_argument("query", nargs="?", help="Keywords to match")
    _add_output(toolsets)

    toolset = sub.add_parser(
        "toolset", help="Show one toolset, its operations and import line"
    )
    toolset.add_argument("toolset_id", help="Toolset id, e.g. salesforce")
    _add_output(toolset)

    nodes = sub.add_parser("nodes", help="List catalogued nodes a workflow can call")
    nodes.add_argument("query", nargs="?", help="Keywords to match")
    nodes.add_argument(
        "--category",
        help="human | guard | control | transform | io | agent | custom",
    )
    _add_backend(nodes)
    _add_output(nodes)

    node = sub.add_parser("node", help="Show one node, with the code to call it")
    node.add_argument("node_id", help="Node id, e.g. human.approval")
    _add_backend(node)
    _add_output(node)

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
    mcp.add_argument(
        "--no-authoring",
        action="store_true",
        help="Do not register the code-authoring tools (get_tool_contract, "
        "validate_workflow_code, smoke_test_workflow, save_workflow, ...); "
        "serve only the run-management tools. Same effect as "
        "LOOM_MCP_AUTHORING=0",
    )
    _add_backend(mcp)
    _add_output(mcp)

    ui = sub.add_parser("ui", help="Launch the terminal UI")
    _add_backend(ui)
    _add_output(ui)

    setup = sub.add_parser(
        "setup",
        help="Write this install into Claude/Cursor/Codex's MCP config",
    )
    setup.add_argument(
        "client", choices=("claude", "cursor", "codex", "all"), help="Which client to configure"
    )
    setup.add_argument(
        "--module",
        "-m",
        action="append",
        metavar="MOD",
        help="Workflow module to load (repeatable). Omit to let the client "
        "read [tool.loom] modules from pyproject.toml itself",
    )
    setup.add_argument(
        "--server",
        metavar="URL",
        help="Point at a running LOOM server instead of importing locally",
    )
    setup.add_argument("--name", default="loom", help="Server name in the client config")
    setup.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="Write the client's global config instead of the project-local one "
        "(Claude Desktop and Codex are always global)",
    )
    setup.add_argument(
        "--project",
        metavar="DIR",
        help="Project directory for project-scoped configs (default: cwd)",
    )
    setup.add_argument(
        "--path", metavar="FILE", help="Write to this exact file instead of the client's default"
    )
    setup.add_argument(
        "--dry-run", action="store_true", help="Print the config without writing it"
    )
    _add_output(setup)


def _authenticating(sub: argparse._SubParsersAction) -> None:
    login = sub.add_parser(
        "login", help="Authenticate this CLI with a LOOM server (browser PKCE by default)"
    )
    login.add_argument(
        "--server",
        metavar="URL",
        help=f"Server to log in to (default: {auth_commands.DEFAULT_SERVER})",
    )
    _add_oauth_flags(login)
    _add_output(login)

    logout = sub.add_parser("logout", help="Forget a stored LOOM server login")
    logout.add_argument(
        "--server",
        metavar="URL",
        help=f"Server to log out of (default: {auth_commands.DEFAULT_SERVER})",
    )
    _add_output(logout)

    whoami = sub.add_parser("whoami", help="Show what this CLI is connected to")
    _add_output(whoami)

    refresh = sub.add_parser(
        "refresh", help="Renew stored OAuth credentials that are near expiry"
    )
    refresh.add_argument(
        "name",
        nargs="*",
        help="Credential names to renew. Defaults to every stored credential.",
    )
    refresh.add_argument(
        "--force",
        action="store_true",
        help="Renew even when not yet due, ignoring the retry backoff",
    )
    _add_output(refresh)

    connect = sub.add_parser(
        "connect", help="Authenticate a named credential (e.g. a toolset) via OAuth"
    )
    connect.add_argument("name", help="Credential name, e.g. 'jira' or 'google'")
    connect.add_argument(
        "--provider",
        metavar="ID",
        help="OAuth provider id (see 'loom providers'). Defaults to NAME.",
    )
    _add_oauth_flags(connect)
    _add_output(connect)

    disconnect = sub.add_parser(
        "disconnect", help="Forget a named credential connected via 'loom connect'"
    )
    disconnect.add_argument("name", help="Credential name, e.g. 'jira' or 'google'")
    _add_output(disconnect)

    providers = sub.add_parser(
        "providers", help="List pre-configured OAuth providers"
    )
    _add_output(providers)


def _add_oauth_flags(parser: argparse.ArgumentParser) -> None:
    """Flags shared by ``login`` and ``connect``: where the authorization
    server is, and which flow to use. Each also has a ``LOOM_LOGIN_*`` /
    ``LOOM_CONNECT_<NAME>_*`` environment variable fallback."""
    parser.add_argument("--authorization-endpoint", metavar="URL", help="Authorization endpoint")
    parser.add_argument("--token-endpoint", metavar="URL", help="Token endpoint")
    parser.add_argument(
        "--device-authorization-endpoint",
        metavar="URL",
        help="Device authorization endpoint (RFC 8628)",
    )
    parser.add_argument("--client-id", help="OAuth client id")
    parser.add_argument(
        "--client-secret", help="OAuth client secret, for a confidential client"
    )
    parser.add_argument(
        "--scope",
        action="append",
        metavar="SCOPE",
        help="Requested scope (repeatable). Replaces provider defaults.",
    )
    flow = parser.add_mutually_exclusive_group()
    flow.add_argument(
        "--pkce", action="store_true", help="Force the browser PKCE flow"
    )
    flow.add_argument(
        "--device",
        action="store_true",
        help="Force the device-code flow, for headless machines",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Give up waiting for the user to finish authorizing after this many seconds",
    )
    parser.add_argument(
        "--redirect-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            f"Loopback port for the PKCE redirect "
            f"(default: {auth_commands.DEFAULT_REDIRECT_PORT}; "
            f"or ${auth_commands.REDIRECT_PORT_ENV} in .env)"
        ),
    )


def _path(value: str):
    from pathlib import Path

    return Path(value)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
