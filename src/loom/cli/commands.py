"""Command handlers.

Each returns a process exit code. Every one that touches a run goes through
:class:`CliBackend`, so the same handler serves both a local Runtime and a
remote server — the only difference is which backend ``resolve()`` built.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from loom.cli.output import Exit, Printer, exit_for
from loom.cli.targets import CliBackend, LocalBackend, Target, resolve
from loom.core.exceptions import (
    ConfigurationError,
    InputMismatch,
    RegistryError,
)
from loom.facade import VersionSurface
from loom.runtime.shutdown import Interrupted

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


def run_async(coro: Awaitable[int]) -> int:
    """Drive a command coroutine, turning known errors into exit codes.

    ``guarded`` is what makes Ctrl+C and ``docker stop`` behave the same: both
    cancel the command, its ``finally`` closes the backend, and the Runtime
    settles the lease on anything it was driving — so an interrupted run is
    picked up by the next ``reclaim_orphans`` instead of being stranded.
    """
    from loom.runtime.shutdown import guarded, report

    try:
        return asyncio.run(guarded(coro, notify=report))
    except (ConfigurationError, InputMismatch, RegistryError) as exc:
        # InputMismatch is USAGE rather than FAILED on purpose: the payload was
        # refused at the door, so there is no run to have failed. A script that
        # branches on the exit code has to be able to tell "fix your input"
        # from "the workflow broke".
        print(str(exc), file=sys.stderr)
        return Exit.USAGE
    except Interrupted as stop:
        interrupted(stop.exit_code)
        return stop.exit_code
    except KeyboardInterrupt:
        # Reachable where add_signal_handler is not (Windows), and if a signal
        # lands in the window before guarded() has installed anything.
        interrupted(Exit.INTERRUPTED)
        return Exit.INTERRUPTED


def interrupted(code: int) -> None:
    """Say what an interrupt left behind, and how to find it.

    A run that was mid-step is still RUNNING in the store with an expired lease.
    That is recoverable rather than lost, but only if whoever pressed Ctrl+C
    knows to go looking — otherwise it reads as a run that vanished.
    """
    print(f"interrupted (exit {code})", file=sys.stderr)
    print(
        "  Any run that was in flight is recoverable: loom runs --status running",
        file=sys.stderr,
    )


def missing_run(run_id: str, out: Printer) -> Exit:
    """Report a run id that names nothing, having done nothing.

    Every command that acts *on* a run checks this before it acts. Delivering
    first and looking the run up afterwards reads as equivalent and is not: an
    event addressed to a run that does not exist is still written to the store,
    and one addressed to the empty string is written as a *broadcast* --
    ``Runtime.send_event`` reserves a falsy target for "every run awaiting this
    name", and the stores match a broadcast row against any run that asks. So a
    mistyped ``loom approve '' publish`` exits 2 saying it found no such run,
    having left an approval in the queue for the next run to reach that gate to
    consume. A human gate satisfied by a stale broadcast is the one failure this
    subsystem exists to prevent.

    ``mcp_server/tools.py`` has always checked in this order; this is the CLI
    catching up to it.
    """
    out.error(f"no run with id {run_id!r}")
    out.hint("loom runs --status suspended")
    return Exit.USAGE


def close_backend(backend: CliBackend) -> None:
    """Close a backend from synchronous code, best-effort.

    For the two commands that hand control to a server owning its own event
    loop. By the time it returns there is no loop left to await on, so this
    opens one — and swallows what it finds, because a failure to close on the
    way out must not turn a clean shutdown into a non-zero exit.
    """
    with contextlib.suppress(Exception):
        asyncio.run(backend.close())


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


def report_run(
    out: Printer,
    run: dict[str, Any],
    *,
    journal: list[dict[str, Any]] | None = None,
) -> None:
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


def cmd_author(args: argparse.Namespace) -> int:
    """Write a workflow from a description.

    The coding agent's first CLI surface. It had none, so every capability it
    grew was reachable only by writing a Python driver — which is a large part
    of why its loop went so long without pressure on it.

    Thin over ``RuntimeFacade.author``: this function reads flags and renders a
    result, and decides nothing. The MCP server calls the same method, and
    ``test_surface_parity`` fails the build if the two drift.
    """

    async def body() -> int:
        out = printer_for(args)
        spec = _spec_text(args.spec)
        target = with_backend(args)
        try:
            result = await target.backend.author(
                spec,
                packages=args.package or None,
                smoke_input=parse_input(args.input),
                observe=not args.no_observe,
            )
        finally:
            await target.backend.close()

        out.json(result)

        if args.output and result["code"]:
            args.output.write_text(result["code"], encoding="utf-8")
        elif result["code"] and not args.json:
            out.verbatim(result["code"])

        _report_authoring(out, result, args)
        # Not clean is not a crash: the code is on disk and the issues say what
        # is unresolved, which is a review, not a failure to produce anything.
        return Exit.OK if result["code"] else Exit.FAILED

    return run_async(body())


def _spec_text(spec: str) -> str:
    """The spec itself, or the contents of ``@file``.

    A useful spec outgrows a shell argument quickly — it names entities, states
    what completeness means, and pins the shape of a right answer.
    """
    if not spec.startswith("@"):
        return spec
    path = Path(spec[1:])
    if not path.exists():
        raise ConfigurationError(f"no such spec file: {path}")
    return path.read_text(encoding="utf-8")


def _report_authoring(
    out: Printer, result: dict[str, Any], args: argparse.Namespace
) -> None:
    """What it did, on stderr-ish lines beside the code on stdout.

    The plan is printed because a rule the model was asked to follow silently
    is a rule nobody can check it followed: each node, and whether it was
    settled by code or handed to judgement.
    """
    if args.json:
        return

    out.line()
    if args.output:
        out.line(f"  wrote {args.output}")

    smoke = result.get("smoke") or {}
    out.line(
        f"  {result['model']}  "
        f"{result['input_tokens']}+{result['output_tokens']} tokens  "
        f"repairs={result['repairs']}  "
        f"ran={'yes' if smoke.get('ok') else 'no'}"
    )
    if result.get("tools_used"):
        out.line(f"  looked at: {', '.join(result['tools_used'])}")

    for node in result.get("plan", []):
        # Columns, not `[kind]`: `line` renders through rich, which reads a
        # bracketed word as a style tag and removes it. `verbatim` says so in
        # its own docstring, and this printed a bare list of nodes until it
        # was read.
        out.line(f"  {node['kind']:<9} {node['node']}")

    for issue in result.get("issues", []):
        line = f"{issue['category']}: {issue['message']}"
        if issue["severity"] == "error":
            out.error(f"  {line}")
        else:
            out.line(f"  warning: {line}")

    if result["clean"]:
        out.hint("loom check <file> && loom run <workflow>")


def cmd_check(args: argparse.Namespace) -> int:
    """Emit ``<flow>.graph.json`` and ``<flow>.description.md`` beside the source."""
    from loom.graph.pipeline import check_module

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    reports = check_module(source, write=not args.no_write)
    problems = [problem for report in reports for problem in report.problems]
    changed = any(report.graph_changed for report in reports)

    if args.json:
        # The top level keeps the shape a single-workflow file has always
        # emitted, aggregated over the module; `flows` is where a file with
        # more than one is actually readable.
        out.json(
            {
                "flow_id": reports[0].flow_id,
                "nodes": sum(report.node_count for report in reports),
                "edges": sum(report.edge_count for report in reports),
                "changed": changed,
                "written": [str(p) for r in reports for p in r.written],
                "problems": problems,
                "flows": [
                    {
                        "flow_id": report.flow_id,
                        "nodes": report.node_count,
                        "edges": report.edge_count,
                        "changed": report.graph_changed,
                        "written": [str(p) for p in report.written],
                        "problems": report.problems,
                    }
                    for report in reports
                ],
            }
        )
    else:
        for report in reports:
            out.line(
                f"{report.flow_id}: {report.node_count} nodes, {report.edge_count} edges"
            )
            for path in report.written:
                out.line(f"  wrote {path}")
            for path in report.unchanged:
                out.line(f"  [dim]unchanged {path}[/dim]")
    for problem in problems:
        out.error(f"warning: {problem}")

    if args.fail_on_change and changed:
        out.error("graph differs from the committed version; run 'loom check' and commit")
        return Exit.FAILED
    return Exit.FAILED if problems else Exit.OK


def cmd_graph(args: argparse.Namespace) -> int:
    """Print the extracted graph in the requested format."""
    from loom.graph.export import to_mermaid
    from loom.graph.pipeline import build_graph
    from loom.graph.reactflow import to_react_flow

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    graph = build_graph(source, flow_id=getattr(args, "workflow", "") or "")
    if args.format == "mermaid":
        print(to_mermaid(graph))
    elif args.format == "json":
        print(graph.model_dump_json(indent=2))
    else:
        print(json.dumps(to_react_flow(graph), indent=2))
    return Exit.OK


def cmd_describe(args: argparse.Namespace) -> int:
    """Print the narration without writing it to disk."""
    from loom.graph.explainer import SkeletonExplainer
    from loom.graph.pipeline import build_graph

    out = printer_for(args)
    source = _require_file(args.path, out)
    if source is None:
        return Exit.USAGE

    graph = build_graph(source, flow_id=getattr(args, "workflow", "") or "")
    narration = asyncio.run(SkeletonExplainer().narrate(graph))
    print(narration.full_text)
    return Exit.OK


def cmd_init(args: argparse.Namespace) -> int:
    """Create a runnable project skeleton."""
    from loom.cli.scaffold import scaffold_project, write_project

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
            if not args.run_id.strip() or await target.backend.get(args.run_id) is None:
                return missing_run(args.run_id, out)
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
            if not args.run_id.strip() or await target.backend.get(args.run_id) is None:
                return missing_run(args.run_id, out)
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


def cmd_versions(args: argparse.Namespace) -> int:
    """List a workflow's committed versions, or activate one.

    Activation is a pointer move, never a commit. Without it, rolling back means
    re-publishing old source as a *new, higher* version — which loses the fact of
    which version is actually being served, and grows the chain with duplicates
    of code that already exists.
    """

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            backend = target.backend
            if not isinstance(backend, VersionSurface):
                out.error(
                    "this backend does not expose workflow versions. "
                    "VersionSurface is optional; a host facade need not have it."
                )
                return Exit.USAGE

            if args.activate is not None:
                served = await backend.activate_version(args.workflow, args.activate)
                out.json(served)
                out.line(
                    f"  {args.workflow} now serving version {served['version']}"
                )
                return Exit.OK

            chain = await backend.versions(args.workflow)
            if not chain:
                out.line(f"  no versions recorded for {args.workflow}")
                return Exit.OK
            out.json(chain)
            out.table(
                ["version", "active", "content hash", "created"],
                [
                    [
                        str(entry.get("version", "")),
                        "*" if entry.get("active") else "",
                        str(entry.get("content_hash", ""))[:12],
                        (entry.get("created_at") or "")[:19],
                    ]
                    for entry in chain
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
            if not args.run_id.strip() or await target.backend.get(args.run_id) is None:
                return missing_run(args.run_id, out)
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


def cmd_toolsets(args: argparse.Namespace) -> int:
    """List the integrations a workflow (or an agent) can call.

    The node catalog has had ``loom nodes`` since it existed; toolsets had no
    CLI surface at all, so the only way to see which integrations a process
    could reach was to start an MCP server and ask it. That is a strange place
    to have to go to answer "is Salesforce wired up here?".

    Reads manifests only — Layer 1 — so listing costs no imports of httpx, no
    vendor SDKs, and no credentials.
    """
    out = printer_for(args)
    from loom.toolsets.registry import get_catalog, register_available_toolsets

    register_available_toolsets()
    catalog = get_catalog()

    # Both shapes are built in the one pass that still holds the manifest. A
    # JSON row is a `dict[str, Any]` by nature, so reading the table's cells
    # back out of it loses every type the manifest had — and a cell rendered
    # from a value that could be anything is a cell nothing checks.
    rows: list[dict[str, Any]] = []
    table: list[list[str]] = []
    for toolset_id in sorted(catalog.toolset_ids):
        manifest = catalog.get(toolset_id)
        if manifest is None:
            continue
        operations = list(manifest.all_operations())
        if args.query and args.query.lower() not in (
            f"{manifest.id} {manifest.summary} {manifest.description}".lower()
        ):
            continue
        groups = sorted(manifest.groups)
        summary = manifest.summary or ""
        rows.append(
            {
                "id": manifest.id,
                "version": manifest.version,
                "operations": len(operations),
                "groups": groups,
                "summary": manifest.summary,
                "auth": sorted((manifest.auth or {}).get("fields", [])),
            }
        )
        table.append(
            [
                manifest.id,
                str(len(operations)),
                ",".join(groups)[:28],
                summary[:52],
            ]
        )

    out.json(rows)
    if not rows:
        out.line("  no toolsets matched")
        return Exit.OK
    out.table(["toolset", "ops", "groups", "summary"], table)
    return Exit.OK


def cmd_toolset(args: argparse.Namespace) -> int:
    """Show one toolset: its operations, effects, and how to import them."""
    out = printer_for(args)
    from loom.toolsets.registry import get_catalog, register_available_toolsets

    register_available_toolsets()
    manifest = get_catalog().get(args.toolset_id)
    if manifest is None:
        known = ", ".join(sorted(get_catalog().toolset_ids)) or "none"
        out.error(f"unknown toolset '{args.toolset_id}' (known: {known})")
        return Exit.USAGE

    # Same reason as `cmd_toolsets`: the table is rendered from each
    # OperationSpec's own attributes, not read back out of the JSON row, where
    # `summary` and `paginated` have already collapsed into one union.
    operations: list[dict[str, Any]] = []
    table: list[list[str]] = []
    for op in manifest.all_operations():
        operations.append(
            {
                "id": op.id,
                "function": op.function,
                "effect": op.effect.value,
                "paginated": op.pagination,
                "resolves": op.resolves,
                "summary": op.summary,
            }
        )
        table.append(
            [
                op.id,
                op.effect.value,
                "yes" if op.pagination else "",
                op.resolves or "",
                (op.summary or "")[:44],
            ]
        )
    out.json(
        {
            "id": manifest.id,
            "version": manifest.version,
            "summary": manifest.summary,
            "description": manifest.description,
            "base_url": manifest.base_url,
            "auth": manifest.auth,
            "import_line": manifest.import_line(),
            "operations": operations,
        }
    )
    out.line(f"{manifest.id} v{manifest.version} — {manifest.summary}")
    if manifest.auth:
        fields = ", ".join((manifest.auth or {}).get("fields", []))
        out.line(f"  auth: {manifest.auth.get('type', '')} ({fields})")
    # The one line that stops a generated import being invented.
    if manifest.import_line():
        out.line(f"  {manifest.import_line()}")
    out.table(["operation", "effect", "pages", "resolves", "summary"], table)
    return Exit.OK


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
        out.error("serving needs the api extra: pip install 'loomsdk[api]'")
        return Exit.USAGE

    from loom.identity.config import LOOPBACK_HOSTS, IdentitySettings
    from loom.server.app import create_app

    # `serve` takes the shared backend flags, but --server is the one it cannot
    # honour: this command *is* the HTTP surface, and `create_app` needs a
    # Runtime in this process to serve. Accepting the flag and quietly ignoring
    # it is the worse failure — it starts a server over the local Runtime and
    # reports success, so a caller who meant to reach another machine gets an
    # empty workflow list and no reason for it.
    if getattr(args, "server", None):
        out.error(
            "serve cannot operate against --server: it is itself the server, and "
            "it needs a Runtime in this process. Point clients at that URL "
            "directly, or drop --server to serve the workflows imported here."
        )
        return Exit.USAGE

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

    # `resolve` with no `server=` only ever builds a LocalBackend, and only that
    # one owns a Runtime. Narrowing on the class rather than asserting it keeps
    # the guarantee checked where it is relied on instead of stated in a comment.
    backend = target.backend
    if not isinstance(backend, LocalBackend):
        out.error("serve needs an in-process Runtime, and this target has none")
        close_backend(backend)
        return Exit.USAGE

    runtime = backend.runtime
    registered = sorted(runtime.workflows)
    out.line(f"  serving {len(registered)} workflow(s) on http://{args.host}:{args.port}")
    for name in registered:
        out.line(f"    [dim]{name}[/dim]")
    if not registered:
        out.line(
            "  [yellow]no workflows imported — list them under "
            "[[tool.loom]] modules, or pass --module[/yellow]"
        )

    # uvicorn installs its own SIGINT/SIGTERM handlers and unwinds cleanly, so
    # what is missing on the way out is only ours: the Runtime's schedulers and
    # the store's connections, neither of which uvicorn knows about.
    #
    # It then re-raises the signal it caught, so the process ends the way it
    # would have unhandled. That is right for a program uvicorn owns and wrong
    # here: the server was *asked* to stop and did, which is a success, and
    # reporting it as an interrupt would print a recovery hint for runs nobody
    # cut short. uvicorn already swallows the KeyboardInterrupt for exactly this
    # reason; SIGTERM only reaches us because we are the ones who handle it.
    try:
        uvicorn.run(
            create_app(runtime, identity=identity),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    except (Interrupted, KeyboardInterrupt):
        pass
    finally:
        close_backend(target.backend)
    return Exit.OK


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve this Runtime over MCP.

    Resolution is the CLI's: ``--module`` or ``[tool.loom] modules`` decides
    which workflows the client sees, and ``--server`` proxies a remote Runtime.
    Without that, a client would connect successfully to an empty server.
    """
    out = printer_for(args)
    try:
        from loom.mcp_server import serve
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

    from loom.mcp_server.authoring_config import AuthoringConfig
    from loom.toolsets.registry import register_available_toolsets

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
    except (Interrupted, KeyboardInterrupt):
        # Same as serve: a server told to stop, which stopped, succeeded.
        pass
    finally:
        # The scheduler lifespan already calls runtime.shutdown(); this is what
        # closes the store behind it.
        close_backend(target.backend)
    return Exit.OK


def cmd_ui(args: argparse.Namespace) -> int:
    """Launch the terminal UI."""
    out = printer_for(args)
    try:
        from loom.cli.tui import run_tui
    except ImportError:
        out.error("the TUI needs the tui extra: pip install 'loomsdk[tui]'")
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
