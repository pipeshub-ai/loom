"""What a project tells the CLI about itself.

The CLI used to read one key — ``[tool.loom] modules`` — and take everything
else from the environment. That made the default store ``memory://``, which is
correct for a library and wrong for a command line: every ``loom`` process built
its own in-memory journal, so a run recorded by one invocation did not exist for
the next.

Twelve of the forty commands depend on state outliving a process — ``runs``,
``show``, ``watch``, ``approve``, ``pending``, ``respond``, ``pin``, ``retry``,
``replay``, ``cancel``, ``pause``, ``artifacts`` — and every one of them
answered "none" out of the box. ``--detach`` was worse than useless: it printed
an identifier for a run that ceased to exist the moment the process ended, so
looking it up reported no such run, which reads as the run having vanished
rather than as never having been kept.

**A project is the unit.** Where a *library* keeps its journal is the host's
decision and stays one; where a *command line* keeps it is a property of the
directory you are standing in, the same way ``[tool.loom] modules`` already is.
So a directory with a ``pyproject.toml`` gets a store beside it, and a directory
without one — a scratch shell, a pipe — keeps the ephemeral default, because
there is nowhere to put a file that anybody asked for.

Nothing here overrides an explicit choice. ``$LOOM_STORE`` still wins over the
project's own key, and ``--store`` wins over both, so the environment remains
the way a deployment decides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ProjectConfig", "load_dotenv"]

#: Where a project's own journal goes, relative to the directory holding
#: ``pyproject.toml``. A dot-directory because it is derived state — the same
#: standing as ``.venv`` or ``.pytest_cache`` — and ``loom init`` puts it in
#: ``.gitignore`` for that reason.
LOOM_DIR = ".loom"
DEFAULT_DB = "runs.db"


@dataclass(frozen=True)
class ProjectConfig:
    """The project the CLI is standing in, and what it says.

    ``root`` is ``None`` when there is no ``pyproject.toml`` anywhere above the
    working directory. That is the case the ephemeral default is *for*, and it
    is deliberately narrow: it means the CLI has nowhere it has been invited to
    write, not that the user does not want their runs.
    """

    root: Path | None
    store_url: str
    #: Where ``store_url`` came from, phrased for a person. ``loom doctor``
    #: prints it, because "which store am I using and why" was unanswerable —
    #: and being unanswerable is most of what made the old default painful.
    store_source: str
    modules: list[str] = field(default_factory=list)
    #: The ``.env`` that was loaded, if there was one.
    env_file: Path | None = None

    @property
    def ephemeral(self) -> bool:
        """Whether this store dies with the process."""
        return self.store_url.startswith("memory:")

    @classmethod
    def discover(
        cls,
        start: Path | None = None,
        *,
        store: str | None = None,
        modules: list[str] | None = None,
        load_env: bool = True,
    ) -> ProjectConfig:
        """Read the nearest project, and decide where its journal lives.

        Store resolution, most explicit first:

        1. ``--store``
        2. ``$LOOM_STORE``
        3. ``[tool.loom] store`` in the nearest ``pyproject.toml``
        4. ``.loom/runs.db`` beside that ``pyproject.toml``
        5. ``memory://`` — only when there is no project at all

        *load_env* reads the project's ``.env`` first, so a ``LOOM_STORE`` or a
        provider key written there participates in every decision below rather
        than only in the ones that happen to check for it.
        """
        root = _project_root(start or Path.cwd())
        env_file = load_dotenv(root) if load_env else None
        table = _loom_table(root)

        declared = modules if modules is not None else _declared_modules(table)

        if store:
            return cls(root, store, "--store", declared, env_file)
        from_env = os.environ.get("LOOM_STORE")
        if from_env:
            source = "$LOOM_STORE"
            if env_file is not None and _in_file(env_file, "LOOM_STORE"):
                source = f"$LOOM_STORE (from {env_file.name})"
            return cls(root, from_env, source, declared, env_file)
        configured = table.get("store")
        if isinstance(configured, str) and configured:
            return cls(root, configured, "[tool.loom] store", declared, env_file)
        if root is not None:
            # `sqlite://` takes the path in the authority position, so an
            # absolute path spells itself with the leading slash it already
            # has. See the store table in CLAUDE.md — this is deliberately not
            # SQLAlchemy's convention.
            return cls(
                root,
                f"sqlite://{root / LOOM_DIR / DEFAULT_DB}",
                f"{LOOM_DIR}/{DEFAULT_DB} (project default)",
                declared,
                env_file,
            )
        return cls(None, "memory://", "no project found — nothing persists", declared, env_file)

    def prepare(self) -> None:
        """Make the store reachable, without deciding anything.

        Only ever creates the directory the project default names. A store the
        user asked for by URL is theirs to have pointed somewhere valid, and
        manufacturing directories under an arbitrary path is not this module's
        business.
        """
        if self.root is None or not self.store_url.startswith("sqlite:"):
            return
        expected = f"sqlite://{self.root / LOOM_DIR / DEFAULT_DB}"
        if self.store_url == expected:
            (self.root / LOOM_DIR).mkdir(parents=True, exist_ok=True)


def load_dotenv(root: Path | None) -> Path | None:
    """Load ``<root>/.env`` into the environment. Returns the file, if any.

    A real environment variable always wins, so exporting a key for one command
    still overrides the file.

    The cookbooks have read ``.env`` since they existed and the CLI did not —
    only ``auth_commands``, only for one OAuth port — so a project with
    ``ANTHROPIC_API_KEY`` in ``.env`` ran under ``python examples/…`` and failed
    under ``loom author``. ``coding_agent`` even ships an error string
    acknowledging it: *"a shell does not read .env the way the cookbooks do."*
    """
    if root is None:
        return None
    path = root / ".env"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # An unreadable .env is not grounds for refusing to run a workflow.
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")
    return path


def _project_root(start: Path) -> Path | None:
    """The nearest directory at or above *start* holding a ``pyproject.toml``."""
    for directory in [start, *start.parents]:
        if (directory / "pyproject.toml").exists():
            return directory
    return None


def _loom_table(root: Path | None) -> dict[str, Any]:
    """``[tool.loom]`` from the project, or an empty table."""
    if root is None:
        return {}
    try:
        import tomllib

        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        # A malformed pyproject is the project's problem to report, not this
        # module's to raise from — every command would then fail on it,
        # including the ones that never needed the file.
        return {}
    table = data.get("tool", {}).get("loom", {})
    return table if isinstance(table, dict) else {}


def _declared_modules(table: dict[str, Any]) -> list[str]:
    modules = table.get("modules", [])
    return [str(m) for m in modules] if isinstance(modules, list) else []


def _in_file(path: Path, key: str) -> bool:
    """Whether *key* is assigned in *path*, for attributing a value to it."""
    try:
        return any(
            line.strip().startswith(f"{key}=")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return False
