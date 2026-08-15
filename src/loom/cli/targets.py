"""Finding a workflow, and reaching a Runtime.

Workflows live in code — the file on disk is the source of truth — so a command
naming one has to import something first. Three resolution paths, tried in
order:

``flows/orders.py::fulfil``
    Explicit path. Always works, needs no configuration.
``fulfil``
    Imports the modules listed under ``[tool.loom] modules`` in
    ``pyproject.toml``, then looks up by name.
``--server URL``
    Imports nothing and asks a running server. The only mode that works when
    the workflow lives on another machine.

Local and remote expose the *same* operations through :class:`CliBackend`, so
every command is written once. The difference is only where the Runtime is.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core.exceptions import ConfigurationError, RegistryError
from loom.facade import LocalFacade, RemoteFacade, RuntimeFacade

# The port and its implementations live in loom.facade, shared with
# the MCP server. These aliases keep the CLI's own vocabulary.
CliBackend = RuntimeFacade
LocalBackend = LocalFacade
RemoteBackend = RemoteFacade

__all__ = [
    "CliBackend",
    "LocalBackend",
    "RemoteBackend",
    "Target",
    "collect_workflows",
    "configured_modules",
    "load_module",
    "resolve",
]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def configured_modules(start: Path | None = None) -> list[str]:
    """Modules listed under ``[tool.loom] modules`` in the nearest pyproject."""
    root = _find_pyproject(start or Path.cwd())
    if root is None:
        return []
    try:
        import tomllib

        data = tomllib.loads(root.read_text(encoding="utf-8"))
    except Exception:
        return []
    modules = data.get("tool", {}).get("loom", {}).get("modules", [])
    return [str(m) for m in modules] if isinstance(modules, list) else []


def _find_pyproject(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def load_module(spec: str) -> Any:
    """Import a module by dotted name or file path."""
    path = Path(spec)
    if path.suffix == ".py" and path.exists():
        # Put the file's own directory on the path first. A workflow file that
        # imports a sibling — helpers, shared steps, anything a project outgrows
        # one file for — otherwise fails with ModuleNotFoundError, which reads
        # as a broken workflow rather than a missing search path. Running the
        # same file with `python flows.py` would have worked, so this matches.
        folder = str(path.resolve().parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)

        name = f"_loom_cli_{path.stem}"
        module_spec = importlib.util.spec_from_file_location(name, path)
        if module_spec is None or module_spec.loader is None:
            raise ConfigurationError(f"cannot import {spec}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
        return module

    # A package directory needs its parent importable.
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    return importlib.import_module(spec)


def collect_workflows(module: Any) -> list[Any]:
    """Every ``WorkflowDefinition`` a module declares."""
    from loom.runtime.workflow import WorkflowDefinition

    return [
        value for value in vars(module).values() if isinstance(value, WorkflowDefinition)
    ]


@dataclass
class Target:
    """A resolved command target: a backend, and the workflow name if given."""

    backend: RuntimeFacade
    workflow: str | None = None


def resolve(
    target: str | None,
    *,
    server: str | None = None,
    modules: list[str] | None = None,
) -> Target:
    """Build the backend a command should use, and resolve a workflow name.

    Raises :class:`ConfigurationError` with a message naming the fix, rather
    than a traceback, whenever resolution fails.
    """
    if server:
        from loom.cli.auth_commands import server_token_provider
        from loom.server.client import LoomClient

        client = LoomClient(base_url=server, token_provider=server_token_provider(server))
        return Target(RemoteFacade(client), target)

    from loom.nodes.human import LogChannel
    from loom.runtime.engine import Runtime

    # The CLI is a *host*, and a host chooses where human requests go — the same
    # reason a workflow does not choose its store. A bare ``Runtime()`` still
    # requires one, because a library that silently swallowed approval requests
    # would be worse than one that refuses; but a CLI with no channel could not
    # run a workflow containing an approval at all.
    #
    # LogChannel is the honest default: it records the request and reports
    # ``delivered=False``, so `loom pending` finds it and the table says plainly
    # that nobody was notified. Configure a real one in the host process for
    # anything that should reach a person.
    runtime = Runtime.from_env(human=LogChannel())
    loaded: list[str] = []

    explicit_name: str | None = None
    to_import = list(modules or configured_modules())

    if target and "::" in target:
        module_spec, _, explicit_name = target.partition("::")
        to_import.insert(0, module_spec)
    elif target and target.endswith(".py"):
        to_import.insert(0, target)
    else:
        explicit_name = target

    for spec in dict.fromkeys(to_import):
        try:
            module = load_module(spec)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(f"could not import {spec!r}: {exc}") from exc
        loaded.append(spec)
        for definition in collect_workflows(module):
            runtime.register(definition)

    backend = LocalFacade(runtime, loaded)

    if explicit_name and explicit_name not in runtime.workflows:
        known = ", ".join(sorted(runtime.workflows)) or "none"
        where = f" (imported: {', '.join(loaded)})" if loaded else ""
        raise RegistryError(
            f"no workflow named {explicit_name!r}{where}. Known: {known}. "
            "Name it as path.py::workflow, or list its module under "
            "[tool.loom] modules in pyproject.toml."
        )

    return Target(backend, explicit_name)
