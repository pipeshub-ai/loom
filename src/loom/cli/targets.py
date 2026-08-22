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

from loom.agents.interaction import CLIUserInteraction
from loom.cli.config import ProjectConfig
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
    """Modules listed under ``[tool.loom] modules`` in the nearest pyproject.

    Kept as its own name because that is what it answers; the rest of what the
    project says now lives in :class:`~loom.cli.config.ProjectConfig`, which
    also reads this key.
    """
    return ProjectConfig.discover(start, load_env=False).modules


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


def _is_a_missing_file(spec: str) -> bool:
    """Whether *spec* names a file that is not there.

    Only a path is judged. A dotted name is a module the import system
    resolves however it likes — against site-packages, a namespace package, an
    editable install — and second-guessing that here would refuse imports that
    work.
    """
    path = Path(spec)
    return path.suffix == ".py" and not path.exists()


def collect_workflows(module: Any) -> list[Any]:
    """Every ``WorkflowDefinition`` a module declares."""
    from loom.runtime.workflow import WorkflowDefinition

    return [
        value for value in vars(module).values() if isinstance(value, WorkflowDefinition)
    ]


@dataclass
class Target:
    """A resolved command target: a backend, and the workflow name if given.

    ``project`` is what the local backend was built from — ``None`` against
    ``--server``, where the store is the server's business. Carried so a
    command can say *which* store it used without re-deriving it, which is
    what made the old ephemeral default so hard to notice.
    """

    backend: RuntimeFacade
    workflow: str | None = None
    project: ProjectConfig | None = None


def _interaction() -> Any:
    """The best question renderer this terminal supports."""
    from loom.cli.repl.interaction import PromptUserInteraction

    picker = PromptUserInteraction()
    return picker if picker.available() else CLIUserInteraction()


def _connect_renderer() -> Any:
    """Draw a :class:`~loom.connectors.flows.ConnectEvent` on stderr.

    Stderr, because ``loom connect x --json`` must keep JSON on stdout, and
    because a connection started by the coding agent interleaves with the
    progress region and the questions, which are written there too.

    Every line goes out with the live region **suspended**. Without that the
    progress spinner keeps redrawing on the same stderr on its own timer and
    lands in the middle of what this is printing — the same two-writers-one-
    terminal fault `ask_user`'s dialog had, one subsystem over.
    """
    import sys

    from loom.cli.progress import suspend

    def say(line: str = "") -> None:
        print(line, file=sys.stderr)

    def draw(event: Any) -> None:
        with suspend():
            _draw(event, say)

    return draw


def _draw(event: Any, say: Any) -> None:
    if event.kind == "needs_app":
        # The redirect URI resolved the way the listener will actually resolve
        # it — `--redirect-port`, then $LOOM_OAUTH_REDIRECT_PORT, then the
        # project's .env. Printing a fixed 8931 here told anyone who had set
        # that variable to register a URI the flow was never going to use.
        from loom.cli.auth_commands import resolve_redirect_port

        redirect = f"http://127.0.0.1:{resolve_redirect_port(None)}/callback"
        say()
        say(f"  {event.detail or 'No OAuth app is registered yet.'}")
        if event.setup_url:
            say(f"  Create one at: {event.setup_url}")
        say(f"  Register this redirect URI on it: {redirect}")
        say("  Then paste the two values below.")
        say()
    elif event.kind == "redirect_ready":
        say()
        say(f"  Redirect URI: {event.redirect_uri}")
        if event.scopes:
            say("  Requesting scopes: " + " ".join(event.scopes))
    elif event.kind == "opening_browser":
        say("  Opening your browser to continue.")
        say(f"  If it does not open, visit: {event.authorization_url}")
    elif event.kind == "waiting" and event.verification_uri:
        say(f"  Go to {event.verification_uri} and enter: {event.user_code}")
    elif event.kind == "exchanged":
        say("  Connected.")


def _secret_prompt() -> Any:
    """`ConsoleSecretPrompt`, with the live region held while it asks.

    The suspension belongs here rather than in the prompt itself:
    `loom.connectors.flows` is library tier and `ProgressRenderer` is the
    CLI's, so the library asking the CLI to stop drawing would be the import
    edge `tests/test_layering.py` exists to prevent. The CLI composes both, so
    the CLI is where they are made to take turns.
    """
    from loom.cli.progress import suspend
    from loom.connectors.flows import ConsoleSecretPrompt
    from loom.toolsets.manifest import AuthField

    class _Held(ConsoleSecretPrompt):
        async def read(self, field: AuthField) -> str:
            with suspend():
                return await super().read(field)

    return _Held()


def resolve(
    target: str | None,
    *,
    server: str | None = None,
    modules: list[str] | None = None,
    store: str | None = None,
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

    from loom.cli.auth_commands import credential_store_for
    from loom.nodes.human import LogChannel
    from loom.runtime.engine import Runtime
    from loom.stores import from_url

    # The project decides where the journal lives, and says why — see
    # `loom.cli.config`. `Runtime.from_env()` would answer `memory://` for a
    # directory that plainly is a project, which is what made twelve commands
    # report "none" out of the box.
    project = ProjectConfig.discover(store=store, modules=modules)
    project.prepare()

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
    runtime = Runtime.from_env(human=LogChannel(), store=from_url(project.store_url))

    # What `loom connect <name>` stored is what a workflow's toolsets read via
    # ctx.credential(name) — without this the CLI could authenticate a
    # credential it then could not use, which is indistinguishable from the
    # connect having silently failed.
    #
    # Attached as Runtime-level credentials, which `Runtime._credentials_for`
    # treats as *ambient*: a per-run `credentials=` still wins, and a name the
    # run declared is never satisfied from here. So this widens what an
    # unspecified run can reach, and never swaps an identity a caller supplied.
    # Built after the Runtime so the store's LockProvider serializes refreshes
    # across processes sharing this credential file.
    runtime.credentials = credential_store_for(runtime)
    loaded: list[str] = []

    explicit_name: str | None = None
    to_import = list(project.modules)

    if target and "::" in target:
        module_spec, _, explicit_name = target.partition("::")
        to_import.insert(0, module_spec)
    elif target and target.endswith(".py"):
        to_import.insert(0, target)
    else:
        explicit_name = target

    missing: list[str] = []
    for spec in dict.fromkeys(to_import):
        if _is_a_missing_file(spec):
            # A *declared* file that is not there is a stale declaration, not
            # a broken workflow — somebody moved or deleted it and did not
            # update `[tool.loom] modules`. Aborting on it took down every
            # command in the project, including the ones that never needed a
            # module: `loom runs` and `loom pending` read the store and import
            # nothing. And the message was actively misleading, reporting "No
            # module named 'flows/x.py'" for something that is a path.
            #
            # A file that *does* exist and raises on import is a different
            # thing and still aborts: that is a real error in code somebody is
            # working on, and running against half a registry would hide it.
            missing.append(spec)
            continue
        try:
            module = load_module(spec)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(f"could not import {spec!r}: {exc}") from exc
        loaded.append(spec)
        for definition in collect_workflows(module):
            runtime.register(definition)

    if missing:
        # stderr, so it cannot corrupt `--json` on stdout.
        print(
            f"warning: [tool.loom] modules names {', '.join(missing)}, which "
            "does not exist — skipping. Remove it from pyproject.toml, or run "
            "`loom doctor`.",
            file=sys.stderr,
        )

    # The CLI is the one surface with a person at the other end of stdin, so
    # it is the one that composes an interaction in. Both implementations
    # return a *skipped* answer when stdin is not a TTY, so a piped or CI
    # invocation degrades to the non-interactive behaviour instead of blocking
    # on a prompt nobody can see.
    #
    # The picker is preferred where it can run because the recommended option
    # is preselected there, which makes Enter a usable "you decide" — the
    # outcome the three-outcome protocol exists for, and the one that costs the
    # same as any other answer when the question is a numbered list. It adds a
    # rendering and no rules: everything it cannot draw is handed to
    # `CLIUserInteraction`, which is where the rules already are.
    #
    # The connect flow and the secret prompt are composed here for the same
    # reason and in the same place. Both default to `None` on `LocalFacade`, so
    # a host embedding it gets no browser and no `getpass` unless it asks: "a
    # library that reads stdin because it was imported is the ambient behaviour
    # `Runtime` avoids everywhere else — the CLI opts in, a server does not."
    from loom.connectors.flows import OAuthBrowserFlow

    backend = LocalFacade(
        runtime,
        loaded,
        user_interaction=_interaction(),
        connect_flow=OAuthBrowserFlow(),
        secret_prompt=_secret_prompt(),
        # Without a renderer a connection that stops at "register an app
        # first" returns in a tenth of a second having printed nothing, which
        # reads as broken rather than as a step you have not done yet.
        on_connect_event=_connect_renderer(),
    )

    if explicit_name and explicit_name not in runtime.workflows:
        known = ", ".join(sorted(runtime.workflows)) or "none"
        where = f" (imported: {', '.join(loaded)})" if loaded else ""
        raise RegistryError(
            f"no workflow named {explicit_name!r}{where}. Known: {known}. "
            "Name it as path.py::workflow, or list its module under "
            "[tool.loom] modules in pyproject.toml."
        )

    return Target(backend, explicit_name, project)
