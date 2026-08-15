"""Command handlers.

Each returns a process exit code. Every one that touches a run goes through
:class:`CliBackend`, so the same handler serves both a local Runtime and a
remote server — the only difference is which backend ``resolve()`` built.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from workflow_builder.cli.output import Exit, Printer, exit_for
from workflow_builder.cli.targets import CliBackend, Target, resolve
from workflow_builder.core.exceptions import (
    ConfigurationError,
    InputMismatch,
    RegistryError,
)

#: How often ``--follow`` and ``watch`` re-read a run, in seconds.
POLL_INTERVAL = 0.4


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def printer_for(args: argparse.Namespace) -> Printer:
    return Printer(as_json=getattr(args, "json", False))


def with_backend(args: argparse.Namespace, target: str | None = None) -> Target:
    """Resolve the backend, or raise a ConfigurationError the caller reports."""
    return resolve(
        target,
        server=getattr(args, "server", None),
        modules=getattr(args, "module", None) or None,
    )


def run_async(coro: Any) -> int:
    """Drive a command coroutine, turning known errors into exit codes."""
    try:
        return asyncio.run(coro)
    except (ConfigurationError, InputMismatch, RegistryError) as exc:
        # InputMismatch is USAGE rather than FAILED on purpose: the payload was
        # refused at the door, so there is no run to have failed. A script that
        # branches on the exit code has to be able to tell "fix your input"
        # from "the workflow broke".
        print(str(exc), file=sys.stderr)
        return Exit.USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return Exit.USAGE


def parse_input(raw: str | None) -> Any:
    """Decode ``--input``: JSON, ``@file.json``, or a bare string.

    Falling back to a bare string matters — most workflows take one, and
    demanding ``'"text"'`` for that case would be hostile.
    """
    if raw is None:
        return None
    if raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists():
            raise ConfigurationError(f"no such input file: {path}")
        raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_env(pairs: list[str] | None, env_file: str | None) -> dict[str, str]:
    """Decode ``--env KEY=VAL`` and ``--env-file`` into a dict.

    File lines are ``KEY=VAL``, with comments and blanks skipped. Flag pairs
    override file entries.
    """
    result: dict[str, str] = {}
    if env_file:
        path = Path(env_file)
        if not path.exists():
            raise ConfigurationError(f"no such env file: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("'\"")
    for item in pairs or []:
        if "=" not in item:
            raise ConfigurationError(f"--env expects KEY=VAL, got {item!r}")
        key, _, value = item.partition("=")
        if not key:
            raise ConfigurationError(f"--env expects KEY=VAL, got {item!r}")
        result[key] = value
    return result


def report_run(out: Printer, run: dict[str, Any], *, journal: list | None = None) -> None:
    """The standard human rendering of one run."""
    out.line()
    out.status(run, prefix="  ")
    out.line()
    if journal:
        out.journal(journal)
        out.line()
    out.value("output", run.get("output"))
    out.value("error", run.get("error"))


def suspended_hint(out: Printer, run: dict[str, Any]) -> None:
    """Explain a parked run and name the command that unparks it."""
    awaiting = run.get("awaiting_event") or ""
    run_id = run.get("run_id", "")
    out.line()
    if awaiting.startswith("approval:"):
        subject = awaiting.split(":", 1)[1]
        out.line(
            f"  [yellow]Waiting for approval '{subject}'. "
            "This run costs nothing while parked.[/yellow]"
        )
        out.hint(f"loom approve {run_id} {subject}")
    elif awaiting:
        out.line(f"  [yellow]Waiting for event '{awaiting}'.[/yellow]")
        out.hint(f"loom send {run_id} {awaiting}")
    else:
        out.line("  [yellow]Parked on a timer.[/yellow]")
    out.hint(f"loom watch {run_id}")


async def follow(
    backend: CliBackend, run_id: str, out: Printer, *, timeout: float = 3600.0
) -> dict[str, Any]:
    """Stream journal entries until the run stops moving.

    Polls rather than subscribes: it is the one approach that works identically
    against an in-process Runtime and a remote server, and a durable run's
    granularity is steps rather than tokens.
    """
    seen = 0
    said = 0
    waited = 0.0
    run: dict[str, Any] = {}

    while waited < timeout:
        run = await backend.get(run_id) or {}
        entries = await backend.journal(run_id)
        for entry in entries[seen:]:
            status = entry.get("status", "")
            name = entry.get("step_id", "")
            out.line(f"  [dim]{entry.get('seq', ''):>3}[/dim]  {name:<24} {status}")
        seen = len(entries)

        # Anything the run narrated since the last poll. A step that takes four
        # minutes is one journal line and no news; this is where it says what it
        # is actually doing.
        fresh = await backend.reports(run_id, said)
        for report in fresh:
            out.line(f"       [dim]{report.get('message', '')}[/dim]")
        said += len(fresh)

        status = str(run.get("status", ""))
        if status in ("completed", "failed", "cancelled", "suspended"):
            return run
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    return run


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    """Emit ``<flow>.graph.json`` and ``<flow>.description.md`` beside the source."""
    from workflow_builder.graph.pipeline import check_file

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    report = check_file(source, write=not args.no_write)

    if args.json:
        out.json(
            {
                "flow_id": report.flow_id,
                "nodes": report.node_count,
                "edges": report.edge_count,
                "changed": report.graph_changed,
                "written": [str(p) for p in report.written],
                "problems": report.problems,
            }
        )
    else:
        out.line(f"{report.flow_id}: {report.node_count} nodes, {report.edge_count} edges")
        for path in report.written:
            out.line(f"  wrote {path}")
        for path in report.unchanged:
            out.line(f"  [dim]unchanged {path}[/dim]")
    for problem in report.problems:
        out.error(f"warning: {problem}")

    if args.fail_on_change and report.graph_changed:
        out.error("graph differs from the committed version; run 'loom check' and commit")
        return Exit.FAILED
    return Exit.FAILED if report.problems else Exit.OK


def cmd_graph(args: argparse.Namespace) -> int:
    """Print the extracted graph in the requested format."""
    from workflow_builder.graph.export import to_mermaid
    from workflow_builder.graph.pipeline import build_graph
    from workflow_builder.graph.reactflow import to_react_flow

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    graph = build_graph(source)
    if args.format == "mermaid":
        print(to_mermaid(graph))
    elif args.format == "json":
        print(graph.model_dump_json(indent=2))
    else:
        print(json.dumps(to_react_flow(graph), indent=2))
    return Exit.OK


def cmd_describe(args: argparse.Namespace) -> int:
    """Print the narration without writing it to disk."""
    from workflow_builder.graph.explainer import SkeletonExplainer
    from workflow_builder.graph.pipeline import build_graph

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    narration = asyncio.run(SkeletonExplainer().narrate(build_graph(source)))
    print(narration.full_text)
    return Exit.OK


def cmd_init(args: argparse.Namespace) -> int:
    """Create a runnable project skeleton."""
    from workflow_builder.cli.scaffold import scaffold_project, write_project

    out = printer_for(args)
    if args.dry_run:
        paths = scaffold_project(str(args.directory))
        out.json({"would_create": paths})
        for path in paths:
            out.line(path)
        return Exit.OK

    written = write_project(str(args.directory))
    out.json({"created": written})
    for path in written:
        out.line(f"created {path}")
    out.line()
    out.line(f"Next: cd {args.directory} && pip install -e '.[dev]' && pytest")
    return Exit.OK


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Start a workflow and report what happened."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args, args.target)
        if target.workflow is None:
            out.error("name a workflow: loom run <name> or loom run path.py::<name>")
            return Exit.USAGE

        try:
            payload = parse_input(args.input)
            env = parse_env(getattr(args, "env", None), getattr(args, "env_file", None))
            run = await target.backend.start(
                target.workflow,
                payload,
                idempotency_key=args.idempotency_key,
                wait=not args.detach,
                env=env or None,
            )
            run_id = run["run_id"]

            if args.follow and not args.detach:
                out.line()
                run = await follow(target.backend, run_id, out)
            elif args.detach:
                out.json(run)
                out.status(run, prefix="  ")
                return Exit.OK

            journal = None if args.follow else await target.backend.journal(run_id)
            out.json(run)
            report_run(out, run, journal=journal)
            if run.get("status") == "suspended":
                suspended_hint(out, run)
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_runs(args: argparse.Namespace) -> int:
    """List runs."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            runs = await target.backend.list_runs(
                workflow=args.workflow, status=args.status, limit=args.limit
            )
            out.json(runs)
            out.table(
                ["run", "workflow", "status", "created"],
                [
                    [
                        run.get("run_id", ""),
                        run.get("workflow", ""),
                        run.get("status", ""),
                        (run.get("created_at") or "")[:19],
                    ]
                    for run in runs
                ],
                status_column=2,
            )
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_show(args: argparse.Namespace) -> int:
    """Show one run and its journal."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            run = await target.backend.get(args.run_id)
            if run is None:
                out.error(f"no run '{args.run_id}'")
                return Exit.USAGE
            journal = await target.backend.journal(args.run_id)
            out.json({**run, "journal": journal})
            report_run(out, run, journal=journal)
            if run.get("status") == "suspended":
                suspended_hint(out, run)
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow a run until it reaches a terminal state."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            if await target.backend.get(args.run_id) is None:
                out.error(f"no run '{args.run_id}'")
                return Exit.USAGE
            out.line()
            run = await follow(target.backend, args.run_id, out, timeout=args.timeout)
            out.json(run)
            out.line()
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            out.value("error", run.get("error"))
            if run.get("status") == "suspended":
                suspended_hint(out, run)
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


# ---------------------------------------------------------------------------
# Acting on runs
# ---------------------------------------------------------------------------


def cmd_approve(args: argparse.Namespace) -> int:
    """Resolve a pending human approval."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            approved = not args.reject
            await target.backend.send_event(
                args.run_id, f"approval:{args.subject}", {"approved": approved}
            )
            run = await target.backend.get(args.run_id) or {}
            out.json(run)
            verb = "approved" if approved else "rejected"
            out.line(f"  {verb} '{args.subject}'")
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_send(args: argparse.Namespace) -> int:
    """Deliver an event to a parked run."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            await target.backend.send_event(
                args.run_id, args.event, parse_input(args.payload)
            )
            run = await target.backend.get(args.run_id) or {}
            out.json(run)
            out.line(f"  delivered '{args.event}'")
            out.status(run, prefix="  ")
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def _confirm(args: argparse.Namespace, out: Printer, action: str, run_id: str) -> bool:
    """Ask before an irreversible action, unless --yes or not a terminal."""
    if getattr(args, "yes", False) or args.json or not sys.stdin.isatty():
        return True
    answer = input(f"{action} {run_id}? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _act(args: argparse.Namespace, action: str) -> int:
    """Shared body for cancel / retry / replay."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            if await target.backend.get(args.run_id) is None:
                out.error(f"no run '{args.run_id}'")
                return Exit.USAGE
            if action == "cancel" and not _confirm(args, out, "cancel", args.run_id):
                out.line("  aborted")
                return Exit.USAGE

            run = await getattr(target.backend, action)(args.run_id)
            out.json(run)
            out.line()
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            out.value("error", run.get("error"))
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_cancel(args: argparse.Namespace) -> int:
    """Request cancellation of a run."""
    return _act(args, "cancel")


def cmd_retry(args: argparse.Namespace) -> int:
    """Re-run a failed execution from its first failed step."""
    return _act(args, "retry")


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-execute a run from its journal without repeating side effects."""
    return _act(args, "replay")


# ---------------------------------------------------------------------------
# Serving and publishing
# ---------------------------------------------------------------------------


def cmd_workflows(args: argparse.Namespace) -> int:
    """List workflows this process can run, plus published ones."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            entries = await target.backend.workflows()
            out.json(entries)
            out.table(
                ["workflow", "version", "runnable", "description"],
                [
                    [
                        entry.get("name", ""),
                        entry.get("version", ""),
                        "yes" if entry.get("executable", True) else "published only",
                        (entry.get("description") or "")[:48],
                    ]
                    for entry in entries
                ],
            )
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_pending(args: argparse.Namespace) -> int:
    """List the runs parked on a person, and what each is being asked.

    The command that makes a parked run a queue item rather than a mystery.
    Before this, finding one meant already knowing it existed.
    """

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            waiting = await target.backend.pending(getattr(args, "run_id", None))
            out.json(waiting)
            if not waiting:
                out.line("  nothing is waiting on a person")
                return Exit.OK
            out.table(
                ["run", "subject", "asked of", "delivered", "prompt"],
                [
                    [
                        row["run_id"],
                        row["subject"],
                        ", ".join(row["assignees"]) or "-",
                        # A request nobody was told about is the failure this
                        # column exists to surface: the run looks patient.
                        "yes" if row["delivered"] else f"no ({row['channel'] or 'no channel'})",
                        (row["prompt"] or "")[:44],
                    ]
                    for row in waiting
                ],
            )
            for row in waiting:
                out.line(f"  {row['next_action']}")
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_respond(args: argparse.Namespace) -> int:
    """Answer a parked human request with a typed payload.

    ``loom approve`` is the yes/no shortcut; this is what a choice, a form, or
    an edited draft needs.
    """

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            answer = _answer_from(args)
            run = await target.backend.respond(args.run_id, args.subject, answer)
            out.json(run)
            out.line(f"  answered '{args.subject}' with {answer}")
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body())


def _answer_from(args: argparse.Namespace) -> dict[str, Any]:
    """The payload to deliver, from the flags given.

    ``--approve``/``--reject`` and ``--value`` compose, so a rejection can carry
    a comment and a form answer can be approved in one call.
    """
    answer: dict[str, Any] = {}
    if args.payload:
        parsed = parse_input(args.payload)
        if isinstance(parsed, dict):
            answer.update(parsed)
        else:
            answer["value"] = parsed
    if args.approve:
        answer["approved"] = True
    if args.reject:
        answer["approved"] = False
    if args.select:
        answer["selected"] = list(args.select)
    if args.comment:
        answer["comment"] = args.comment
    if args.responder:
        answer["responder"] = args.responder
    return answer


def cmd_nodes(args: argparse.Namespace) -> int:
    """List the catalogued nodes a workflow can call."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            found = await target.backend.nodes(
                args.query or "", category=args.category
            )
            out.json(found)
            if not found:
                out.line("  no nodes matched")
                return Exit.OK
            out.table(
                ["node", "category", "parks", "summary"],
                [
                    [
                        row["id"],
                        row["category"],
                        "yes" if row["suspends"] else "",
                        (row["summary"] or "")[:56],
                    ]
                    for row in sorted(found, key=lambda r: (r["category"], r["id"]))
                ],
            )
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_node(args: argparse.Namespace) -> int:
    """Show one node, including the exact code to call it."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            detail = await target.backend.node(args.node_id)
            out.json(detail)
            # verbatim, not line: this is code, and rich would eat the
            # [category] tag in the header and any bracket in a default value.
            out.verbatim(detail["contract"])
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_artifacts(args: argparse.Namespace) -> int:
    """List, inspect, or download named artifacts."""
    import base64
    from pathlib import Path

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        action = args.action or "list"
        try:
            if action == "list":
                items = await target.backend.list_artifacts()
                out.json(items)
                out.table(
                    ["name", "version", "size", "mime"],
                    [
                        [
                            item.get("name", ""),
                            str(item.get("version", "")),
                            str(item.get("size", "")),
                            item.get("mime", ""),
                        ]
                        for item in items
                    ],
                )
                return Exit.OK
            if not args.name:
                out.error("name is required for show/download")
                return Exit.USAGE
            if action == "show":
                history = await target.backend.artifact_history(args.name)
                out.json(history)
                out.table(
                    ["version", "size", "sha256", "run"],
                    [
                        [
                            str(item.get("version", "")),
                            str(item.get("size", "")),
                            str(item.get("sha256", ""))[:12],
                            str(item.get("created_by_run", ""))[:16],
                        ]
                        for item in history
                    ],
                )
                return Exit.OK
            payload = await target.backend.read_artifact(args.name, args.version)
            data = base64.b64decode(payload["content_b64"])
            dest = Path(args.output) if args.output else Path(payload.get("name") or args.name)
            dest.write_bytes(data)
            out.json({"path": str(dest), "size": len(data), "mime": payload.get("mime")})
            out.line(f"wrote {dest} ({len(data)} bytes)")
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_publish(args: argparse.Namespace) -> int:
    """Record a workflow in the durable catalog."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args, args.target)
        if target.workflow is None:
            out.error("name a workflow: loom publish <name>")
            return Exit.USAGE
        try:
            record = await target.backend.publish(target.workflow)
            out.json(record)
            out.line(f"  published {record.get('name')}@{record.get('version')}")
            out.value("hash", (record.get("code_hash") or "")[:16])
            out.value("source", record.get("source_file"))
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body())


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the HTTP API."""
    out = printer_for(args)
    try:
        import uvicorn
    except ImportError:
        out.error("serving needs the api extra: pip install 'workflow-builder[api]'")
        return Exit.USAGE

    from workflow_builder.identity.config import LOOPBACK_HOSTS, IdentitySettings
    from workflow_builder.server.app import create_app

    try:
        target = resolve(None, modules=getattr(args, "module", None) or None)
    except (ConfigurationError, RegistryError) as exc:
        out.error(str(exc))
        return Exit.USAGE

    identity = IdentitySettings()
    if not identity.is_configured() and args.host not in LOOPBACK_HOSTS:
        out.error(
            f"refusing to bind {args.host}:{args.port} with no identity configured "
            "— that serves every workflow to anyone who can reach this port. Set "
            "LOOM_AUTH_JWKS_URI (or another LOOM_AUTH_* verifier config) or bind "
            "to a loopback host."
        )
        return Exit.USAGE

    runtime = target.backend.runtime  # type: ignore[union-attr]
    registered = sorted(runtime.workflows)
    out.line(f"  serving {len(registered)} workflow(s) on http://{args.host}:{args.port}")
    for name in registered:
        out.line(f"    [dim]{name}[/dim]")
    if not registered:
        out.line(
            "  [yellow]no workflows imported — list them under "
            "[[tool.loom]] modules, or pass --module[/yellow]"
        )

    uvicorn.run(
        create_app(runtime, identity=identity),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return Exit.OK


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve this Runtime over MCP.

    Resolution is the CLI's: ``--module`` or ``[tool.loom] modules`` decides
    which workflows the client sees, and ``--server`` proxies a remote Runtime.
    Without that, a client would connect successfully to an empty server.
    """
    out = printer_for(args)
    try:
        from workflow_builder.mcp_server import serve
    except ImportError as exc:
        out.error(str(exc))
        return Exit.USAGE

    try:
        target = with_backend(args)
    except (ConfigurationError, RegistryError) as exc:
        out.error(str(exc))
        return Exit.USAGE

    runtime = getattr(target.backend, "runtime", None)
    workflows = sorted(runtime.workflows) if runtime is not None else []

    from workflow_builder.mcp_server.authoring_config import AuthoringConfig
    from workflow_builder.toolsets.registry import register_available_toolsets

    toolsets = register_available_toolsets()
    authoring = AuthoringConfig.from_env()
    if args.no_authoring:
        authoring = AuthoringConfig(
            enabled=False,
            smoke_timeout=authoring.smoke_timeout,
            max_code_size=authoring.max_code_size,
        )

    # stdio *is* the protocol channel, so anything written to stdout would
    # corrupt the stream. Status goes to stderr.
    if args.transport == "stdio":
        out.error(
            f"loom mcp: serving {len(workflows)} workflow(s) over stdio"
            + (f" ({', '.join(workflows)})" if workflows else "")
        )
        if toolsets:
            out.error(f"  toolsets: {', '.join(toolsets)}")
        out.error(f"  authoring tools: {'on' if authoring.enabled else 'off'}")
        if not workflows and not args.server:
            out.error(
                "  no workflows imported — pass --module <file.py>, or list "
                "modules under [[tool.loom]] in pyproject.toml"
            )
    else:
        out.line(
            f"  serving {len(workflows)} workflow(s) over {args.transport} "
            f"on {args.host}:{args.port}"
        )
        for name in workflows:
            out.line(f"    [dim]{name}[/dim]")
        out.line(f"  authoring tools: {'on' if authoring.enabled else 'off'}")

    try:
        serve(
            target.backend,
            name=args.name,
            transport=args.transport,
            host=args.host,
            port=args.port,
            scheduler=not args.no_scheduler,
            authoring=authoring,
        )
    except ValueError as exc:
        out.error(str(exc))
        return Exit.USAGE
    return Exit.OK


def cmd_ui(args: argparse.Namespace) -> int:
    """Launch the terminal UI."""
    out = printer_for(args)
    try:
        from workflow_builder.cli.tui import run_tui
    except ImportError:
        out.error("the TUI needs the tui extra: pip install 'workflow-builder[tui]'")
        return Exit.USAGE

    try:
        target = with_backend(args)
    except (ConfigurationError, RegistryError) as exc:
        out.error(str(exc))
        return Exit.USAGE

    return run_tui(target.backend)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_file(path: Path, out: Printer) -> Path | None:
    """Resolve a path, printing a usable error rather than a traceback."""
    if not path.exists():
        out.error(f"no such file: {path}")
        return None
    if path.is_dir():
        out.error(f"expected a file, got a directory: {path}")
        return None
    return path
