"""``loom doctor`` — what this environment can actually do.

Every failure this reports was previously discovered the same way: run a real
command, get a message about a symptom, and work backwards. No store configured
surfaced as ``loom runs`` printing "none". No provider key surfaced four turns
into an authoring run. A module that does not import surfaced as "no workflow
named x". None of those name the thing that is wrong.

Two rules shape it:

* **Nothing here starts anything.** It reads manifests, imports the modules the
  project declares, and touches the store once to prove it is writable. A
  diagnostic with side effects is one people stop running.
* **Exit 1 when something would fail later.** A checker that always succeeds can
  only be read by a person, and the failures worth catching are the ones nobody
  is looking at. A *warning* is something narrower than the whole tool being
  broken — no OAuth credentials stored is normal for a project that needs none.
"""

from __future__ import annotations

import argparse
import contextlib
import os
from dataclasses import dataclass
from typing import Any

from loom.cli.commands import printer_for, run_async
from loom.cli.output import Exit, Printer, esc

__all__ = ["Check", "cmd_doctor", "collect"]

#: Glyph and style per outcome. Same vocabulary the run statuses use, because a
#: reader should not have to learn a second one.
_MARK = {
    "ok": ("green", "●"),
    "warn": ("yellow", "◐"),
    "fail": ("red", "✗"),
}


@dataclass
class Check:
    """One thing that was looked at, and what was found."""

    name: str
    status: str  # ok | warn | fail
    detail: str
    #: What to do about it. Empty when there is nothing to do.
    fix: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


async def collect(args: argparse.Namespace) -> list[Check]:
    """Run every check. Never raises: a check that cannot run is a finding."""
    checks: list[Check] = []
    checks.append(_python())
    project, project_check = _project(args)
    checks.append(project_check)
    checks.extend(await _store(args))
    checks.append(_model())
    checks.extend(_workflows(args))
    checks.append(_toolsets())
    checks.append(_optionals())
    if project is not None and project.root is not None:
        checks.append(await _credentials())
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what is configured, and exit 1 on anything that would fail later."""

    async def body() -> int:
        out = printer_for(args)
        checks = await collect(args)
        out.json(
            {
                "ok": not any(c.status == "fail" for c in checks),
                "checks": [c.as_json() for c in checks],
            }
        )
        _render(out, checks)
        return Exit.FAILED if any(c.status == "fail" for c in checks) else Exit.OK

    return run_async(body(), debug=getattr(args, "debug", False))


def _render(out: Printer, checks: list[Check]) -> None:
    out.line()
    for check in checks:
        style, glyph = _MARK.get(check.status, ("", "·"))
        out.line(
            f"  [{style}]{glyph}[/{style}] [bold]{esc(check.name):<12}[/bold] "
            f"{esc(check.detail)}"
        )
        if check.fix:
            out.hint(check.fix)
    out.line()
    failed = [c for c in checks if c.status == "fail"]
    if failed:
        out.line(
            f"  [red]{len(failed)} problem(s) — commands depending on these will "
            "fail.[/red]"
        )
    else:
        out.line("  [green]ready[/green]")


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _python() -> Check:
    import sys

    version = ".".join(str(n) for n in sys.version_info[:3])
    from loom import __version__

    return Check("loom", "ok", f"{__version__} on python {version}")


def _project(args: argparse.Namespace) -> tuple[Any, Check]:
    from loom.cli.config import ProjectConfig

    project = ProjectConfig.discover(store=getattr(args, "store", None))
    if project.root is None:
        return project, Check(
            "project",
            "warn",
            "no pyproject.toml above this directory",
            "loom init . — without one, runs are kept in memory and lost on exit",
        )
    env = ", .env loaded" if project.env_file else ""
    return project, Check("project", "ok", f"{project.root}{env}")


async def _store(args: argparse.Namespace) -> list[Check]:
    """Where the journal lives, and whether it can actually be written.

    Reachability is the half that matters: a URL that parses and a store that
    accepts a write are different claims, and only the second one is the one
    every other command depends on.
    """
    from loom.cli.config import ProjectConfig

    project = ProjectConfig.discover(store=getattr(args, "store", None))
    if getattr(args, "server", None):
        return [
            Check("store", "ok", f"remote — {args.server} keeps the journal"),
        ]

    where = Check("store", "ok", f"{project.store_url}  [{project.store_source}]")
    if project.ephemeral:
        where = Check(
            "store",
            "warn",
            f"{project.store_url} — runs do not survive this command",
            "loom init . to get a project store, or pass --store sqlite://runs.db",
        )

    try:
        from loom.stores import from_url

        project.prepare()
        store = from_url(project.store_url)
        # Connect explicitly where the backend offers it. Postgres builds its
        # pool lazily, so without this the first *use* fails deep inside the
        # driver with "'NoneType' object has no attribute 'acquire'" — a
        # message about our call rather than about the server being
        # unreachable, which is the finding.
        connect = getattr(store, "connect", None)
        if callable(connect):
            await connect()
        # Then one real round trip. Listing is the cheapest operation every
        # store implements, and it exercises connection, schema and
        # permissions — which is the whole question.
        await store.list_executions(limit=1)
        with contextlib.suppress(Exception):
            await store.close()
    except Exception as exc:
        return [
            where,
            Check(
                "store-write",
                "fail",
                f"cannot reach it: {exc}",
                "check the URL, the directory's permissions, and that the server is up",
            ),
        ]
    return [where]


def _model() -> Check:
    """Whether authoring can run at all.

    ``loom author`` without a key used to fail after argument parsing with a
    message naming three environment variables — which is the right message,
    arriving at the wrong time for anyone setting a project up.
    """
    from loom.agents import providers

    keys = providers.env_keys()
    present = [key for key in keys if os.environ.get(key)]
    if not present:
        return Check(
            "model",
            "warn",
            "no provider key set — author, edit and ctx.agent() cannot run",
            f"set one of {', '.join(keys)} (a project .env is read)",
        )
    model = providers.from_env()
    name = getattr(model, "model_name", "") if model is not None else ""
    return Check("model", "ok", f"{name or 'configured'} via {present[0]}")


def _workflows(args: argparse.Namespace) -> list[Check]:
    """The modules the project declares, and whether they import.

    A module that raises on import is the failure most often mistaken for a
    missing workflow: ``resolve`` reports "no workflow named x", which sends
    someone to check the name.
    """
    from loom.cli.config import ProjectConfig
    from loom.cli.targets import collect_workflows, load_module

    project = ProjectConfig.discover()
    declared = list(getattr(args, "module", None) or project.modules)
    if not declared:
        return [
            Check(
                "workflows",
                "warn",
                "no modules declared",
                "list them under [tool.loom] modules in pyproject.toml, "
                "or pass --module flows.py",
            )
        ]

    checks: list[Check] = []
    names: list[str] = []
    for spec in declared:
        try:
            module = load_module(spec)
        except Exception as exc:
            checks.append(
                Check(
                    "workflows",
                    "fail",
                    f"{spec} does not import: {type(exc).__name__}: {exc}",
                    "fix the import; until then every command naming a workflow "
                    "reports it as unknown",
                )
            )
            continue
        names += [definition.name for definition in collect_workflows(module)]

    if not checks:
        checks.append(
            Check(
                "workflows",
                "ok" if names else "warn",
                f"{len(names)} registered from {len(declared)} module(s)"
                + (f": {', '.join(sorted(names)[:6])}" if names else ""),
                "" if names else "a declared module that defines no @workflow",
            )
        )
    return checks


def _toolsets() -> Check:
    """Layer 1 only — manifests, no vendor imports, no credentials."""
    try:
        from loom.toolsets.registry import register_available_toolsets

        found = register_available_toolsets()
    except Exception as exc:
        return Check("toolsets", "warn", f"catalogue did not load: {exc}")
    return Check("toolsets", "ok", f"{len(found)} reachable from this process")


def _optionals() -> Check:
    """The extras that decide which surfaces exist at all."""
    import importlib.util

    wanted = {
        "rich": "cli",
        "textual": "tui",
        "fastapi": "api",
        "mcp": "mcp",
        "prompt_toolkit": "cli",
    }
    missing = [
        f"{module} ([{extra}])"
        for module, extra in wanted.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return Check("extras", "ok", "cli, tui, api and mcp all installed")
    return Check(
        "extras",
        "warn",
        "not installed: " + ", ".join(missing),
        "pip install 'loomsdk[cli,tui,api,mcp]' for every surface",
    )


async def _credentials() -> Check:
    """What ``loom connect`` has stored, and whether any of it has expired."""
    try:
        from loom.cli.auth_commands import credential_store_for

        names = list(await credential_store_for(None).names())
    except Exception:
        # A credential file that cannot be opened is `loom whoami`'s finding to
        # report in detail; here it only needs to not be a failure.
        return Check("credentials", "warn", "credential store unreadable")
    if not names:
        return Check("credentials", "ok", "none stored")
    return Check("credentials", "ok", f"{len(names)} stored: {', '.join(sorted(names))}")
