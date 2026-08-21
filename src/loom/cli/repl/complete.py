"""Completion for the session prompt.

Three namespaces, told apart by their sigil, because they answer three different
questions and a single flat list would make each one worse:

``/command``  what can I do
``#workflow`` what can I run — read from the project's own registry
``@file``     what can I open or pass as input

The workflow list comes from resolving the project rather than from a cache, so
a workflow added since the session started completes. It is looked up lazily and
at most once every few seconds: a completer runs on every keystroke, and
importing the project's modules on each one would make typing feel broken.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

__all__ = ["LoomCompleter", "workflow_names"]

#: How long a workflow list is reused. Long enough that a burst of keystrokes
#: costs one import, short enough that a file saved in another window shows up
#: without restarting the session.
CACHE_SECONDS = 5.0

#: A directory is never worth completing a workflow file out of.
_SKIP = {".git", ".venv", "__pycache__", ".loom", "node_modules", ".mypy_cache"}


def workflow_names(project: Any) -> list[str]:
    """Every workflow the project declares. Empty on any failure.

    A completer that raises takes the prompt down with it, and a module that
    does not import is ``loom doctor``'s finding to report — not something to
    discover by pressing Tab.
    """
    try:
        from loom.cli.targets import collect_workflows, load_module

        found: list[str] = []
        for spec in getattr(project, "modules", []) or []:
            found += [d.name for d in collect_workflows(load_module(spec))]
        return sorted(set(found))
    except Exception:
        return []


class LoomCompleter:
    """A ``prompt_toolkit`` completer over the three namespaces.

    Constructed lazily by the session so importing this module costs no
    ``prompt_toolkit``, which is an optional extra.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self._workflows: list[str] = []
        self._read_at = 0.0

    # prompt_toolkit calls this on every keystroke.
    def get_completions(self, document: Any, _event: Any) -> Any:
        from prompt_toolkit.completion import Completion

        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("/"):
            yield from self._commands(word, Completion)
        elif word.startswith("#"):
            yield from self._workflow(word, Completion)
        elif word.startswith("@"):
            yield from self._paths(word, Completion)

    # -- namespaces ----------------------------------------------------------

    def _commands(self, word: str, Completion: Any) -> Any:  # noqa: N803
        from loom.cli.repl.commands import known

        stem = word[1:].lower()
        for command in known():
            if command.name.startswith(stem):
                yield Completion(
                    f"/{command.name}",
                    start_position=-len(word),
                    display=f"/{command.name}",
                    display_meta=command.summary,
                )

    def _workflow(self, word: str, Completion: Any) -> Any:  # noqa: N803
        stem = word[1:].lower()
        for name in self._current_workflows():
            if name.lower().startswith(stem):
                yield Completion(
                    f"#{name}", start_position=-len(word), display=f"#{name}"
                )

    def _paths(self, word: str, Completion: Any) -> Any:  # noqa: N803
        stem = word[1:]
        root = getattr(self._session, "root", None) or Path.cwd()
        directory = (root / stem).parent if "/" in stem else root
        prefix = stem.rsplit("/", 1)[-1]
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP:
                continue
            if not entry.name.startswith(prefix):
                continue
            try:
                shown = entry.relative_to(root)
            except ValueError:
                shown = entry
            suffix = "/" if entry.is_dir() else ""
            yield Completion(
                f"@{shown}{suffix}",
                start_position=-len(word),
                display=f"{shown}{suffix}",
            )

    def _current_workflows(self) -> list[str]:
        now = time.monotonic()
        if now - self._read_at > CACHE_SECONDS:
            self._workflows = workflow_names(getattr(self._session, "project", None))
            self._read_at = now
        return self._workflows
