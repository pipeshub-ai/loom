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

from loom.cli.changes import (
    Decision,
    FileChange,
    apply,
    propose,
    render,
    session_allowlist,
)
from loom.cli.output import Exit, Printer, esc, exit_for
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
    return Printer(
        as_json=getattr(args, "json", False),
        quiet=getattr(args, "quiet", False),
        debug=getattr(args, "debug", False),
    )


def with_backend(args: argparse.Namespace, target: str | None = None) -> Target:
    """Resolve the backend, or raise a ConfigurationError the caller reports."""
    return resolve(
        target,
        server=getattr(args, "server", None),
        modules=getattr(args, "module", None) or None,
        store=getattr(args, "store", None),
    )


def run_async(
    coro: Awaitable[int], *, debug: bool = False, drives_runs: bool = True
) -> int:
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
        interrupted(stop.exit_code, drives_runs=drives_runs)
        return stop.exit_code
    except KeyboardInterrupt:
        # Reachable where add_signal_handler is not (Windows), and if a signal
        # lands in the window before guarded() has installed anything.
        interrupted(Exit.INTERRUPTED, drives_runs=drives_runs)
        return Exit.INTERRUPTED
    except Exception as exc:
        # Everything the three arms above do not name: a store refusing a
        # connection, a vendor SDK's 401, a rendering fault. Those reached the
        # user as forty frames, which says nothing about which of them is the
        # user's to fix. The traceback is kept behind --debug rather than
        # discarded, because the one case where it is the only useful output is
        # a defect in this repository.
        unexpected(exc, debug=debug)
        return Exit.FAILED


def unexpected(exc: BaseException, *, debug: bool = False) -> None:
    """Report a failure no command anticipated, without a wall of frames."""
    import traceback

    if debug:
        traceback.print_exc()
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    if not debug:
        print("  re-run with --debug for the traceback", file=sys.stderr)


def interrupted(code: int, *, drives_runs: bool = True) -> None:
    """Say what an interrupt left behind, and how to find it.

    A run that was mid-step is still RUNNING in the store with an expired
    lease. That is recoverable rather than lost, but only if whoever pressed
    Ctrl+C knows to go looking — otherwise it reads as a run that vanished.

    *drives_runs* is ``False`` for the commands that start none. ``author`` and
    ``edit`` spend model tokens and write a file; pointing their interrupt at
    ``loom runs --status running`` sent people to look for a run that never
    existed, which is worse than saying nothing.
    """
    print(f"interrupted (exit {code})", file=sys.stderr)
    if drives_runs:
        print(
            "  Any run that was in flight is recoverable: loom runs --status running",
            file=sys.stderr,
        )
    else:
        print("  Nothing was written. Nothing to clean up.", file=sys.stderr)


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


def parse_input(raw: str | None, *, default: Any = None) -> Any:
    """Decode ``--input``: JSON, ``@file.json``, or a bare string.

    Falling back to a bare string matters — most workflows take one, and
    demanding ``'"text"'`` for that case would be hostile.

    *default* is what an **absent** flag means, which is not the same as
    ``--input null``. The engine passes the input positionally, so an absent
    flag used to override a body's own declared default with ``None`` — see
    :attr:`WorkflowDefinition.input_default`, which is what callers pass here.
    """
    if raw is None:
        return default
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


def _declared_default(target: Target) -> Any:
    """The workflow's own default for its input parameter, when it has one.

    Only answerable where the definition is in this process. Against
    ``--server`` the CLI has a name and a schema and no signature, so an absent
    ``--input`` stays ``None`` there — the server is the layer that would have
    to apply it.
    """
    runtime = getattr(target.backend, "runtime", None)
    if runtime is None or not target.workflow:
        return None
    try:
        return runtime.resolve_workflow(target.workflow).input_default
    except Exception:
        # Resolution already failed loudly in `resolve()` if it was going to.
        return None


def _ephemeral_store(target: Target) -> bool:
    """Whether this target's journal dies with the process.

    Read from the project that chose it rather than from the store object: the
    two agree, and the project is also the thing that can say *why*.
    """
    return target.project is not None and target.project.ephemeral


def _warn_if_ephemeral(out: Printer, target: Target) -> None:
    """Say so when a run id names something that will not outlive this command.

    ``--detach`` against an in-memory store prints an identifier for a run that
    ceases to exist the moment the process ends — so ``loom watch`` on it
    reports no such run, which reads as the run having vanished rather than as
    never having been kept.
    """
    if not _ephemeral_store(target):
        return
    out.line(
        "  [yellow]This store is in memory: the run above will not exist once "
        "this command exits.[/yellow]"
    )
    out.hint("run this inside a project (loom init), or pass --store sqlite://runs.db")


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
            f"  [yellow]Waiting for approval '{esc(subject)}'. "
            "This run costs nothing while parked.[/yellow]"
        )
        out.hint(f"loom approve {run_id} {subject}")
    elif awaiting:
        out.line(f"  [yellow]Waiting for event '{esc(awaiting)}'.[/yellow]")
        out.hint(f"loom send {run_id} {awaiting}")
    else:
        out.line("  [yellow]Parked on a timer.[/yellow]")
    out.hint(f"loom watch {run_id}")


#: Statuses at which a run has stopped moving and there is nothing left to
#: follow. ``suspended`` belongs here: a parked run costs nothing and may sit
#: for weeks, so waiting on it is a different command.
SETTLED = ("completed", "failed", "cancelled", "suspended")


async def follow(
    backend: CliBackend, run_id: str, out: Printer, *, timeout: float = 3600.0
) -> tuple[dict[str, Any], bool]:
    """Stream journal entries until the run stops moving.

    Polls rather than subscribes: it is the one approach that works identically
    against an in-process Runtime and a remote server, and a durable run's
    granularity is steps rather than tokens.

    Returns the run and whether it **settled**. A caller cannot infer that from
    the status alone — a run still ``running`` when the deadline expired looks
    exactly like one being reported mid-flight — and conflating the two is how
    ``loom watch --timeout`` exited 0 on a run that was still going.
    """
    seen = 0
    said = 0
    waited = 0.0
    run: dict[str, Any] = {}

    while True:
        run = await backend.get(run_id) or {}
        # Only what has landed since the last poll. Refetching the whole
        # journal each tick is quadratic in the length of the run, and pays for
        # it twice against a remote Runtime.
        entries = await backend.journal(run_id, seen)
        for entry in entries:
            status = esc(entry.get("status", ""))
            name = esc(entry.get("step_id", ""))
            out.line(f"  [dim]{entry.get('seq', ''):>3}[/dim]  {name:<24} {status}")
        seen += len(entries)

        # Anything the run narrated since the last poll. A step that takes four
        # minutes is one journal line and no news; this is where it says what it
        # is actually doing.
        fresh = await backend.reports(run_id, said)
        for report in fresh:
            out.line(f"       [dim]{esc(report.get('message', ''))}[/dim]")
        said += len(fresh)

        if str(run.get("status", "")) in SETTLED:
            return run, True
        if waited >= timeout:
            return run, False
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


def _watching(args: argparse.Namespace, target: Target) -> Any:
    """Attach a progress renderer to the backend, and return it to be closed.

    Set on the backend for the reason ``_asking`` sets the interaction there: a
    renderer is an object rather than a payload, so it cannot cross
    ``RemoteFacade`` — which refuses to author anyway.

    Silent under ``--json`` and ``--quiet``, and reduced to one line per event
    when stderr is not a terminal, so a redirected authoring run produces a log
    rather than a file of escape codes.
    """
    from loom.cli.progress import ProgressRenderer

    backend = target.backend
    # Both attributes are `LocalFacade`'s; the port carries neither, for the
    # reason `user_interaction` is not on it either. A remote backend simply
    # gets no renderer.
    if not (hasattr(backend, "hooks") and hasattr(backend, "on_stage")):
        return None

    quiet = getattr(args, "json", False) or getattr(args, "quiet", False)
    renderer = ProgressRenderer.for_terminal(enabled=not quiet)
    if not renderer.enabled:
        return renderer

    from loom.runtime.hooks import HookRegistry

    # Its own registry, not the Runtime's. `HookRegistry` is per-Runtime
    # precisely so one caller's middleware is not another's, and progress
    # rendering is this command's business rather than the deployment's.
    hooks = HookRegistry()
    renderer.install(hooks)
    backend.hooks = hooks
    backend.on_stage = renderer
    return renderer


def _asking(args: argparse.Namespace, target: Target) -> None:
    """Apply ``--no-ask`` / ``--answers`` to the backend before it authors.

    Set on the backend rather than passed through ``author()``, because an
    interaction is an object and not a payload: it could not cross
    ``RemoteFacade``, which refuses to author anyway. What crosses the port is
    the *record*, which is JSON.
    """
    backend = target.backend
    if not hasattr(backend, "user_interaction"):
        return
    if getattr(args, "no_ask", False):
        backend.user_interaction = None
        return
    answers = getattr(args, "answers", None)
    if answers is not None:
        from loom.agents.interaction import RecordedUserInteraction

        # The recording answers what it knows and the terminal answers the
        # rest, so a spec that grew a new ambiguity asks about that one alone
        # rather than re-asking everything or refusing to build.
        backend.user_interaction = RecordedUserInteraction.from_file(
            answers, fallback=backend.user_interaction
        )


def _save_answers(args: argparse.Namespace, result: dict[str, object]) -> None:
    """Write the questions and answers, when asked for and when there were any.

    An empty file is not written: it reads as "this run asked nothing" only if
    you know the flag was passed, and as "the flag did not work" otherwise.
    """
    path = getattr(args, "save_answers", None)
    asked = result.get("questions") or []
    if path is None or not asked:
        return
    path.write_text(json.dumps(asked, indent=2) + "\n", encoding="utf-8")


#: Triggers that mean "not now, later" — something else has to be running for
#: them to fire, and saying which is the difference between a workflow that is
#: scheduled and one that merely says it is.
_WIRED_BY = {
    "Schedule": "loom serve  (or rt.start_scheduler())",
    "After": "loom serve  (or rt.start_scheduler())",
    "Interval": "loom serve  (or rt.start_scheduler())",
    "Poll": "loom serve  (or rt.start_scheduler())",
    "OnAppEvent": "loom serve  (an EventDispatcher must be draining the topic)",
    "OnEvent": "loom serve  (a QueueConsumer must be draining the queue)",
    "Webhook": "loom serve  (the endpoint is published by the HTTP surface)",
    "EmailInbox": "loom serve",
    "Chat": "loom serve",
}


async def _act_on(
    out: Printer, target: Target, result: dict[str, Any], args: Any
) -> int:
    """Do the thing that was asked for, rather than describing where it lives.

    `loom author` wrote a file, registered it, printed a summary and stopped —
    so "find my jira tickets", which is a *task*, produced Python and the
    sentence `loom run my_jira_tickets`. The task was the query and the answer
    is the tickets.

    Two independent decisions, and keeping them apart is what makes this
    predictable:

    * **what gets wired** comes from the triggers the workflow declares. A
      question declares none and runs once, now; "every weekday at 9" declares
      a `Schedule` and is registered.
    * **whether the immediate run needs asking** comes from the effect classes
      its manifests declare — never from the model's account of its own code.
      Every call a declared read, and it runs; anything that writes, deletes,
      or is unclassified is named and asked about first.

    A declared schedule does not suppress the first run: seeing it work once is
    most of the reason to have asked for it, and a workflow whose first
    execution is at 9am tomorrow is one nobody has tested.
    """
    triggers = result.get("triggers") or []
    # A one-off delay this command is about to sit through needs no advice
    # about `loom serve`: `_run_when_due` says what it is doing, and telling
    # somebody to start a server for a trigger that is about to fire in front
    # of them is the advice-with-no-move-behind-it failure again.
    waiting = _one_shot_delay(triggers) is not None and bool(getattr(args, "run", False))
    for trigger in triggers:
        kind = str(trigger.get("kind", ""))
        detail = _trigger_detail(trigger)
        out.line(f"  [dim]trigger[/dim] {esc(kind)}{esc(detail)}")
        needs = _WIRED_BY.get(kind)
        if needs and not (waiting and kind == "After"):
            out.line(f"    [dim]fires while {esc(needs)} is running[/dim]")

    name = _authored_name(result)
    if name is None:
        return Exit.OK

    decision = await _may_run(out, result, args)
    if decision is not True:
        if decision is False:
            out.line(f"  [dim]not run. loom run {esc(name)}[/dim]")
        return Exit.OK

    # A one-off delay is the one trigger an immediate run does not rehearse.
    # A cron's first run is a preview of something that happens again, so
    # running it now is worth a duplicate; `After(minutes=2)` fires exactly
    # once, and running it now is that one firing, at the wrong time — which
    # is precisely what the request asked not to happen.
    delay = _one_shot_delay(triggers)
    if delay is not None:
        return await _run_when_due(out, args, name, delay)
    return await _run_now(out, args, name)


def _one_shot_delay(triggers: list[dict[str, Any]]) -> float | None:
    """The delay of a declared one-shot trigger, if there is one.

    Read from ``after_seconds`` rather than from the class name, so a spec that
    renders differently is still recognised by the field that decides when it
    fires — the same allowlist ``_trigger_id`` hashes.
    """
    for trigger in triggers:
        fields = {**(trigger.get("fields") or {})}
        if str(trigger.get("kind", "")) != "After":
            continue
        seconds = fields.get("seconds", 0) or 0
        minutes = fields.get("minutes", 0) or 0
        hours = fields.get("hours", 0) or 0
        days = fields.get("days", 0) or 0
        total = (
            float(seconds) + float(minutes) * 60
            + float(hours) * 3600 + float(days) * 86400
        )
        if total > 0:
            return total
    return None


def _trigger_detail(trigger: dict[str, Any]) -> str:
    fields = {**(trigger.get("fields") or {})}
    positional = trigger.get("args") or []
    parts = [str(value) for value in positional]
    parts += [f"{key}={value}" for key, value in fields.items()]
    return f" ({', '.join(parts)})" if parts else ""


def _authored_name(result: dict[str, Any]) -> str | None:
    """The workflow to run, read from the source rather than from an import.

    Deciding what to do with a freshly written file happens before anything has
    imported it, and importing model-written code to find out whether it is
    safe to run has the order backwards.
    """
    from loom.graph.extractor import flow_names

    try:
        found = flow_names(result.get("code") or "")
    except Exception:
        return None
    return found[0] if found else None


async def _may_run(out: Printer, result: dict[str, Any], args: Any) -> bool | None:
    """True to run, False to decline, None when running was never asked for."""
    if not getattr(args, "run", False):
        return None
    if not result.get("clean"):
        # Unresolved findings are a review, not a failure — but they are also
        # not a reason to reach a real API on somebody's behalf.
        out.line("  [dim]not run: the verification pipeline still has findings[/dim]")
        return False

    writes = result.get("writes") or []
    if result.get("impact") == "read_only" and not writes:
        return True

    out.line()
    out.line("  [yellow]This does more than read:[/yellow]")
    for call in writes:
        effect = str(call.get("effect", "")) or "unclassified"
        out.line(f"    [yellow]{esc(effect):<13}[/yellow] {esc(str(call.get('target')))}")
    if getattr(args, "yes", False):
        return True

    import sys

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # The rule `before` hooks and `propose` already follow: a gate that
        # could not run has not passed. `--yes` is the override, and saying so
        # beats a CI job that silently did nothing.
        out.line("  [dim]not run: nothing here can answer. Pass --yes to run it.[/dim]")
        return False
    out.line()
    out.line("  [bold]Run it?[/bold] [dim](y/N)[/dim]")
    try:
        # A thread, because this is inside the command's own event loop:
        # `input()` on the loop blocks every timer and heartbeat the Runtime
        # has running behind it. `CLIUserInteraction` reads stdin the same way.
        answer = (await asyncio.to_thread(input, "  > ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        out.line()
        return False
    return answer in ("y", "yes")


#: Longest one-off delay this command will sit through. Beyond it the trigger
#: is wired and reported, because a terminal held open for a week is not a
#: scheduler — `loom serve` is, and it survives the laptop closing.
_MAX_WAIT_SECONDS = 15 * 60


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


async def _run_when_due(out: Printer, args: Any, name: str, delay: float) -> int:
    """Wire the one-shot trigger, and stay until it fires.

    The half that was missing. A declared trigger is a statement, and until
    something drives a dispatcher it is a statement nobody has heard — so
    ``loom run`` on a workflow whose whole point was "in two minutes" reported
    a scheduled trigger and then nothing ever happened, which reads exactly
    like a broken schedule.

    Driven through the port rather than by sleeping and then calling
    ``_run_now``: waiting out the delay and running it by hand would produce
    the right joke at the right time while leaving the trigger untested, so
    the one thing this exists to prove would be the one thing unproven.
    ``tick_schedules`` is one turn of the loop ``rt.start_scheduler()`` runs,
    and it advances the engine's own timers on the way through — so a body
    that also parks on ``ctx.sleep`` gets woken here too.

    A trigger outlives the command. Ctrl+C, or a delay too long to sit
    through, leaves it wired and says which command picks it up: the record is
    in the store, and its fire time was fixed when it was registered rather
    than being pushed forward by the next process to look at it.
    """
    fresh = with_backend(args, name)
    out.line()
    try:
        try:
            wired = await fresh.backend.wire_triggers(name)
        except Exception as exc:  # pragma: no cover - remote or storeless
            out.line(f"  [dim]not scheduled: {esc(str(exc))}[/dim]")
            return Exit.OK

        when = next(
            (t.get("next_fire_at") for t in wired if t.get("next_fire_at")), None
        )
        out.line(f"  [dim]scheduled[/dim] {esc(name)} fires in {_duration(delay)}")

        if delay > _MAX_WAIT_SECONDS:
            out.line(f"  [dim]too far out to wait here: {esc(str(when))}[/dim]")
            out.hint("loom serve")
            return Exit.OK

        out.line("  [dim]waiting — Ctrl+C leaves it scheduled[/dim]")
        started = await _tick_until_fired(fresh, delay)
        if not started:
            out.line("  [dim]nothing fired; still scheduled[/dim]")
            out.hint("loom serve")
            return Exit.OK

        run_id = started[0]["run_id"]
        record, settled = await follow(fresh.backend, run_id, out)
        out.json(record)
        out.status(record)
        if record.get("output") is not None:
            out.value("output", record["output"])
        return int(exit_for(record, settled=settled))
    finally:
        await fresh.backend.close()


async def _tick_until_fired(target: Target, delay: float) -> list[dict[str, Any]]:
    """Turn the scheduler's loop until the trigger fires, or the window shuts.

    Polled rather than slept-through-once, because the fire time belongs to the
    record and not to this command: a trigger registered by an earlier
    invocation is already part-way through its delay, and one whose clock is a
    ``ManualClock`` is not on this loop's timeline at all.
    """
    import asyncio

    # A grace period past the delay, since a tick lands on the poll boundary
    # rather than on the fire time.
    deadline = delay + _POLL_SECONDS * 2
    waited = 0.0
    while waited <= deadline:
        started = await target.backend.tick_schedules()
        if started:
            return started
        await asyncio.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS
    return []


#: How often the wait above turns the loop. Short enough that a two-minute
#: delay is not reported half a minute late, long enough not to hammer a store.
_POLL_SECONDS = 1.0


async def _run_now(out: Printer, args: Any, name: str) -> int:
    """Start it and show what it answered.

    Awaited rather than driven: this is already inside the event loop
    ``run_async`` opened for ``cmd_author``, so opening a second one raised
    ``asyncio.run() cannot be called from a running event loop`` — after the
    file had been written and the summary printed, which is the worst place for
    it. Awaiting here also keeps the run inside the same ``guarded`` scope, so
    Ctrl+C settles its lease exactly as it does for ``loom run``.
    """
    out.line()
    # Re-resolved, because the file was written after this command's Runtime
    # imported its modules — the registry does not have the new workflow yet.
    fresh = with_backend(args, name)
    try:
        record = await fresh.backend.start(name, None, wait=True)
    finally:
        await fresh.backend.close()
    out.json(record)
    out.status(record)
    if record.get("output") is not None:
        out.value("output", record["output"])
    return int(exit_for(record, settled=True))


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
        resume = str(getattr(args, "resume", "") or "")
        target = with_backend(args)
        if resume == "list":
            return await _list_sessions(out, target)
        spec = _spec_text(args.spec)
        _asking(args, target)
        watching = _watching(args, target)
        try:
            result = await target.backend.author(
                spec,
                packages=args.package or None,
                smoke_input=parse_input(args.input),
                observe=not args.no_observe,
                turns=args.turns,
                max_tokens=getattr(args, "max_tokens", None),
                max_cost=getattr(args, "max_cost", None),
                resume=resume,
            )
        except BaseException:
            # Includes the Ctrl+C that `run_async` turns into an exit code: the
            # id is only useful to somebody who has just lost four minutes, and
            # that is the one moment they do not have it.
            _say_resume(out, target)
            raise
        finally:
            if watching is not None:
                # Before closing the backend, so the live region is gone before
                # anything else writes — otherwise the summary is drawn under a
                # spinner that is still redrawing over it.
                watching.flush_stages()
                watching.close()
            await target.backend.close()

        out.json(result)
        _save_answers(args, result)

        if args.output and result["code"]:
            change = FileChange(path=args.output, after=result["code"])
            # `-o existing.py` clobbered silently, for the same reason `edit`
            # did: the write was the first thing either did with the path.
            # Creating a file nothing is losing needs no ceremony, so the
            # question is only asked when there is something to overwrite.
            decision = (
                Decision.APPLY
                if change.creating
                else propose(
                    change,
                    out,
                    assume_yes=getattr(args, "yes", False),
                    allowlist=session_allowlist(),
                )
            )
            if decision is Decision.REFUSE:
                out.line("  [dim]not written[/dim]")
                _report_authoring(out, result, args)
                return Exit.USAGE
            apply(change, out)
            _register(out, target, args.output)
        elif result["code"] and not args.json:
            out.verbatim(result["code"])

        _report_authoring(out, result, args)
        if args.output and result["code"]:
            return await _act_on(out, target, result, args)
        # Not clean is not a crash: the code is on disk and the issues say what
        # is unresolved, which is a review, not a failure to produce anything.
        return Exit.OK if result["code"] else Exit.FAILED

    return run_async(
        body(), debug=getattr(args, "debug", False), drives_runs=False
    )


def cmd_pause(args: argparse.Namespace) -> int:
    """Hold a run at its next durable boundary.

    Between steps, never inside one — so nothing is half-done and the run
    resumes exactly where a crash would have.
    """

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            record = await target.backend.pause(args.run)
        finally:
            await target.backend.close()
        out.json(record)
        if not args.json:
            out.line(f"{args.run} will hold at its next durable step")
            out.line(f"release it with: loom unpause {args.run}")
        return Exit.OK

    return run_async(body(), debug=getattr(args, "debug", False))


def cmd_unpause(args: argparse.Namespace) -> int:
    """Release a held run and let it continue."""

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            record = await target.backend.unpause(args.run)
        finally:
            await target.backend.close()
        out.json(record)
        if not args.json:
            out.line(f"{args.run} released")
        return Exit.OK

    return run_async(body(), debug=getattr(args, "debug", False))


def cmd_pin(args: argparse.Namespace) -> int:
    """Turn a run into a regression test.

    The loop durable execution exists for: a production failure becomes a
    committed test that fails for the same reason and passes when it is fixed.
    """

    async def body() -> int:
        out = printer_for(args)
        target = with_backend(args)
        try:
            # `--module` is a repeatable "where the workflows live" flag; the
            # generated test needs one import path, so the first is used and
            # the rest are the resolver's business.
            modules = args.module or []
            pinned = await target.backend.pin(
                args.run, module=modules[0] if modules else ""
            )
        finally:
            await target.backend.close()

        out.json(pinned)
        destination = args.output
        if destination is None and not args.json:
            out.verbatim(pinned["source"])
        elif destination is not None:
            destination.write_text(pinned["source"], encoding="utf-8")
            if not args.json:
                out.line(f"wrote {esc(destination)} ({pinned['seeded']} entries seeded)")

        if not args.json:
            for note in pinned["notes"]:
                out.line(f"note: {esc(note)}")
        return Exit.OK

    return run_async(body(), debug=getattr(args, "debug", False))


def cmd_edit(args: argparse.Namespace) -> int:
    """Change a workflow that already exists, by describing the change.

    Thin over ``RuntimeFacade.edit``, exactly as ``cmd_author`` is over
    ``author``: reads flags, renders a result, decides nothing.

    Writes in place by default — an edit whose result you have to copy
    somewhere is not an edit — and ``--output`` sends it elsewhere for a
    reviewer who wants both versions. ``--dry-run`` prints the diff and writes
    nothing, which is what you want the first time you try an instruction.
    """

    async def body() -> int:
        out = printer_for(args)
        try:
            source = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            out.error(f"cannot read {args.file}: {exc}")
            return Exit.USAGE

        target = with_backend(args)
        _asking(args, target)
        watching = _watching(args, target)
        try:
            result = await target.backend.edit(
                source,
                _spec_text(args.instruction),
                packages=args.package or None,
                smoke_input=parse_input(args.input),
                observe=not args.no_observe,
            )
        finally:
            if watching is not None:
                watching.flush_stages()
                watching.close()
            await target.backend.close()

        out.json(result)
        _save_answers(args, result)

        if not result["changed"]:
            if not args.json:
                out.line("unchanged")
                if result.get("explanation"):
                    out.line(result["explanation"])
            # Not a failure. The instructions tell the model to decline rather
            # than guess, so declining is the requested behaviour and the file
            # on disk is still the one that works.
            return Exit.OK

        destination = args.output or args.file
        change = FileChange(
            path=destination,
            after=result["code"],
            # The model's own diff, computed against the source it was handed.
            # Preferred over one recomputed from disk, because that is the diff
            # the *edit* is: a file changed in another window since would
            # otherwise be described as part of this change.
            diff=str(result.get("diff") or ""),
            graph_changes=tuple(result.get("graph_changes") or ()),
            explanation=str(result.get("explanation") or ""),
        )

        if args.dry_run:
            render(change, out)
            _report_authoring(out, result, args)
            out.line(f"  [dim]would write {esc(destination)}[/dim]")
            return Exit.OK

        # Shown, then asked, then written. The write used to come first and the
        # diff after it, so the first answer to "what did that do to my
        # workflow?" arrived when the answer was already the only copy.
        decision = propose(
            change,
            out,
            assume_yes=getattr(args, "yes", False),
            allowlist=session_allowlist(),
        )
        if decision is Decision.REFUSE:
            out.line("  [dim]left unchanged[/dim]")
            _report_authoring(out, result, args)
            # Usage, not failure: nothing broke, a person declined. The file on
            # disk is the one that still works.
            return Exit.USAGE
        written = apply(change, out)
        _report_authoring(out, result, args)
        return written if written != Exit.OK else Exit.OK

    return run_async(
        body(), debug=getattr(args, "debug", False), drives_runs=False
    )


def _say_resume(out: Printer, target: Target) -> None:
    """Name the job that was interrupted, so it can be picked up.

    Read off the facade, which records the id as the agent is built. A caller
    four minutes into a generation has lost the transcript, the resolved
    entities and the tokens along with it — the id is the only thing that gets
    any of it back, and it is the one thing they never saw.
    """
    session_id = str(getattr(target.backend, "last_session_id", "") or "")
    if not session_id or not getattr(target.backend, "resumable", False):
        # Nothing completed, so there is nothing to come back to. Naming an id
        # that resolves to nothing is the advice-that-cannot-help failure this
        # file has already fixed twice.
        return
    out.error(f"  pick it up with: loom author --resume {session_id}")


async def _list_sessions(out: Printer, target: Target) -> int:
    """``--resume list``: authoring jobs that can still be picked up.

    Its own listing rather than a `loom sessions` command, because it answers
    one question — "what can I pass to --resume?" — and belongs beside the flag
    that takes the answer.
    """
    runtime = getattr(target.backend, "runtime", None)
    if runtime is None:
        out.error("authoring sessions are local; --server keeps none")
        return Exit.USAGE

    from loom.agents.session_store import StoreBackedSessionStore

    found = await StoreBackedSessionStore(runtime.store).recent()
    out.json([snapshot.as_json() for snapshot in found])
    if not found:
        out.line("  no authoring sessions to resume")
        out.hint("they are kept for a week after the last turn")
        return Exit.OK
    out.table(
        ["session", "turns", "tokens", "spec"],
        [
            [
                snapshot.session_id,
                str(snapshot.turns_used),
                str(snapshot.spent.total_tokens),
                snapshot.spec.replace("\n", " ")[:48],
            ]
            for snapshot in found
        ],
    )
    return Exit.OK


def _register(out: Printer, target: Target, module: Path) -> None:
    """Make the workflow just written findable by name.

    Authoring a file and then being told the workflow does not exist is the
    last step of the loop failing: a name resolves through ``[tool.loom]
    modules``, and nothing added the file to it — so ``loom run digest`` after
    ``loom author -o flows/digest.py`` reported an unknown workflow, which
    reads as the authoring having failed.

    Additive, verified, and *said out loud*: this edits the project's own
    ``pyproject.toml``, so it is not something to do quietly. When the edit
    cannot be made safely the line to add is printed instead.
    """
    from loom.cli.config import register_module

    project = target.project
    if project is None or project.root is None:
        return
    outcome = register_module(project.root, module)
    if outcome == "added":
        try:
            shown = module.resolve().relative_to(project.root.resolve()).as_posix()
        except ValueError:  # pragma: no cover - register_module already refused
            return
        out.line(f"  [dim]registered {esc(shown)} in [[dim]tool.loom[/dim]] modules[/dim]")
    elif outcome == "unchanged":
        out.line(
            "  [yellow]could not add it to [[dim]tool.loom[/dim]] modules — "
            "add it by hand so 'loom run' can find it by name:[/yellow]"
        )
        out.hint(f'modules = [..., "{module.name}"]')


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
    smoke = result.get("smoke") or {}
    out.line(
        f"  {result['model']}  "
        f"{result['input_tokens']}+{result['output_tokens']} tokens  "
        f"repairs={result['repairs']}  "
        f"ran={'yes' if smoke.get('ok') else 'no'}"
    )
    if result.get("tools_used"):
        out.line(f"  looked at: {esc(', '.join(result['tools_used']))}")

    for node in result.get("plan", []):
        # Columns, not `[kind]`: `line` renders through rich, which reads a
        # bracketed word as a style tag and removes it. `verbatim` says so in
        # its own docstring, and this printed a bare list of nodes until it
        # was read.
        out.line(f"  {esc(node['kind']):<9} {esc(node['node'])}")

    for issue in result.get("issues", []):
        line = f"{issue['category']}: {issue['message']}"
        if issue["severity"] == "error":
            out.error(f"  {line}")
        else:
            out.line(f"  warning: {esc(line)}")

    if not result["clean"]:
        return
    # The exact command, not a shape to fill in. A workflow's name comes from
    # `@workflow(name=...)` and is routinely not the filename — the hint used
    # to read `loom run <workflow>` literally, leaving the one thing the reader
    # needs as the one thing it did not say.
    written = getattr(args, "output", None)
    names = declared_workflows(written) if written else []
    if names:
        for name in names:
            out.hint(f"loom run {name}")
    else:
        out.hint("loom check <file> && loom run <workflow>")


def declared_workflows(path: Path) -> list[str]:
    """Workflow names declared in *path*, read without importing it.

    An AST walk rather than an import: this is a *reporting* path, and running
    a freshly generated module's top level to find out what to call it is a
    side effect nobody asked for. The name is whatever ``@workflow(name=...)``
    says, falling back to the function's own name, which is the rule
    ``WorkflowDefinition`` applies.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            if getattr(target, "id", getattr(target, "attr", "")) != "workflow":
                continue
            named = next(
                (
                    kw.value.value
                    for kw in (call.keywords if call else [])
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                ),
                None,
            )
            found.append(str(named) if named else node.name)
    return found


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
                out.line(f"  wrote {esc(path)}")
            for path in report.unchanged:
                out.line(f"  [dim]unchanged {esc(path)}[/dim]")
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
        out.line(f"created {esc(path)}")
    out.line()
    # `verbatim`, not `line`: this is a command to copy, and rich reads the
    # `[dev]` extra as a style tag and deletes it — handing a new user an
    # install that leaves them without the pytest the same line tells them to
    # run. `cd .` is skipped when the target is already where they are.
    prefix = "" if str(args.directory) in (".", "") else f"cd {args.directory} && "
    out.verbatim(f"Next: {prefix}pip install -e '.[dev]' && pytest")
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
            payload = parse_input(args.input, default=_declared_default(target))
            env = parse_env(getattr(args, "env", None), getattr(args, "env_file", None))
            # `--follow` has to *not* wait. It did, so `start` drove the run to
            # completion and `follow` then polled a run that had already
            # finished — every journal line arriving at once, after the fact,
            # from the one flag whose whole purpose is that they do not.
            # `submit` spawns the drive on this same loop, so the poll below
            # and the run make progress together.
            streaming = args.follow and not args.detach
            run = await target.backend.start(
                target.workflow,
                payload,
                idempotency_key=args.idempotency_key,
                wait=not (args.detach or streaming),
                env=env or None,
            )
            run_id = run["run_id"]

            settled = True
            if streaming:
                out.line()
                run, settled = await follow(target.backend, run_id, out)
            elif args.detach:
                out.json(run)
                out.status(run, prefix="  ")
                if not args.json:
                    _warn_if_ephemeral(out, target)
                    out.hint(f"loom watch {run_id}")
                return Exit.OK

            journal = None if streaming else await target.backend.journal(run_id)
            out.json(run)
            report_run(out, run, journal=journal)
            if run.get("status") == "suspended":
                suspended_hint(out, run)
            return exit_for(run, settled=settled)
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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
            run, settled = await follow(
                target.backend, args.run_id, out, timeout=args.timeout
            )
            out.json(run)
            out.line()
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            out.value("error", run.get("error"))
            if run.get("status") == "suspended":
                suspended_hint(out, run)
            elif not settled:
                # Still going when we stopped looking. Exit 3, not 0: this
                # command reported nothing about whether the run succeeds.
                out.line(
                    f"  [yellow]still running after {args.timeout:g}s — "
                    "stopped watching, not stopped.[/yellow]"
                )
                out.hint(f"loom watch {args.run_id}")
            return exit_for(run, settled=settled)
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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
            out.line(f"  {verb} '{esc(args.subject)}'")
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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
            out.line(f"  delivered '{esc(args.event)}'")
            out.status(run, prefix="  ")
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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
                out.line(f"  {esc(row['next_action'])}")
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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
            out.line(f"  answered '{esc(args.subject)}' with {esc(answer)}")
            out.status(run, prefix="  ")
            out.value("output", run.get("output"))
            return exit_for(run)
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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


def cmd_connections(args: argparse.Namespace) -> int:
    """Which integrations are configured here, and what the rest are short of.

    `loom toolsets` says what this process can *reach*; `loom whoami` says what
    it has *stored*. Neither could put the two together, so "is Jira usable
    here?" had no answer — the credential name a client reads was a default
    argument inside one file, and nothing outside it could learn the name, let
    alone check for it.

    Reads manifests, peeks at the credential store, and looks at the
    environment. No toolset is imported, nothing is minted, and no token is
    printed.
    """
    out = printer_for(args)

    async def body() -> int:
        target = with_backend(args)
        try:
            rows = await target.backend.connections(args.toolset or "")
        finally:
            await target.backend.close()

        if getattr(args, "missing", False):
            rows = [r for r in rows if r["state"] in ("missing", "expired")]

        out.json(rows)
        if not rows:
            out.line("Nothing to report.")
            return int(Exit.OK)

        out.table(
            ["toolset", "state", "auth", "needs", "how"],
            [
                [
                    row["toolset"],
                    row["state"],
                    row["method"],
                    ", ".join(row["missing_fields"])[:38] or "-",
                    row["how"] or "-",
                ]
                for row in rows
            ],
            status_column=1,
        )
        # Never non-zero. `loom doctor` is the command that fails a build, and
        # an unconfigured integration is a normal state for a project that does
        # not use it — exiting 1 here would make `loom connections` unusable in
        # every script that runs it to find out.
        return int(Exit.OK)

    return run_async(body(), debug=getattr(args, "debug", False), drives_runs=False)


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
                "auth": sorted(manifest.auth.field_names),
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
            "auth": manifest.auth.model_dump(mode="json"),
            "import_line": manifest.import_line(),
            "operations": operations,
        }
    )
    out.line(f"{esc(manifest.id)} v{esc(manifest.version)} — {esc(manifest.summary)}")
    auth = manifest.auth
    if auth.kind != "none":
        fields = ", ".join(auth.field_names)
        out.line(f"  auth: {esc(auth.kind)} ({esc(fields)})")
        # The two facts `loom connect` needs and nothing could previously
        # answer: which provider serves this toolset, and which credential name
        # its client reads.
        if auth.credential:
            connect = f"loom connect {auth.credential}"
            via = f" via the '{auth.provider}' provider" if auth.provider else ""
            out.line(f"  connect: {esc(connect)}{esc(via)}")
        if auth.setup_url:
            out.line(f"  create an app: {esc(auth.setup_url)}")
    # The one line that stops a generated import being invented.
    if manifest.import_line():
        out.line(f"  {esc(manifest.import_line())}")
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

    return run_async(body(), debug=getattr(args, "debug", False))


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

    return run_async(body(), debug=getattr(args, "debug", False))


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
            out.line(f"wrote {esc(dest)} ({len(data)} bytes)")
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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
            out.line(f"  published {esc(record.get('name'))}@{esc(record.get('version'))}")
            out.value("hash", (record.get("code_hash") or "")[:16])
            out.value("source", record.get("source_file"))
            return Exit.OK
        finally:
            await target.backend.close()

    return run_async(body(), debug=getattr(args, "debug", False))


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
        out.line(f"    [dim]{esc(name)}[/dim]")
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
            out.line(f"    [dim]{esc(name)}[/dim]")
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
