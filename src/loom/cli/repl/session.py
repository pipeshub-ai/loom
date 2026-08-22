"""The interactive session ``loom`` opens with no subcommand.

Everything the CLI could do, it could only do one command at a time: describe a
workflow, wait, read a summary, type the next command. The loop that authoring
actually is — describe, look, change, run, change again — had no surface at all,
and each round trip paid the resolve cost and lost every piece of context the
previous one had built.

**The subcommands are not replaced.** The exit codes, ``--json`` and the
local/remote symmetry exist for scripts and CI, and a session that became the
only way in would take that away. Every capability here is a subcommand too, and
:mod:`loom.cli.repl.commands` reaches them through the *same parser and the same
handlers* rather than reimplementing any of them.

Two things the session owns that a command cannot:

**Focus.** A file being worked on. Free text with a file in focus is an *edit*
of it; free text with none is a new workflow. That is a rule about session
state, not about the words typed — a keyword list over the prompt is exactly the
guess ``DEFAULT_SYSTEM_PROMPT`` names as the tell for a rule nobody should
write. It will still sometimes route wrongly, so ``/author`` and ``/edit`` say
which explicitly, ``/new`` drops focus, and the diff shown before any write
means a misroute costs one keystroke rather than a file.

**A status line.** Project, store, workflows, and how many runs are parked on a
person. That last one is the question ``loom ui`` exists for, and needing a
different program to answer it is why nobody does.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.cli.config import ProjectConfig
from loom.cli.output import Exit, Printer, esc

__all__ = ["Session", "run_session"]

BANNER_GLYPH = "✻"

#: Where the session's own history lives. Inside `.loom/` because it is derived
#: state, and `loom init` already ignores that directory.
HISTORY_FILE = "history"


@dataclass
class Session:
    """One interactive session's state.

    Deliberately small. Anything durable — runs, journals, credentials — is the
    store's, and anything reusable is a subcommand's; what is left is the two
    things above plus a printer.
    """

    project: ProjectConfig
    out: Printer
    focus: Path | None = None
    """The workflow file free text edits. ``None`` means the next description
    writes a new one."""
    _closing: bool = field(default=False, repr=False)

    @property
    def root(self) -> Path:
        return self.project.root or Path.cwd()

    # -- one line ------------------------------------------------------------

    def handle(self, line: str) -> int:
        """Act on one line of input. Returns the exit code it produced."""
        text = line.strip()
        if not text:
            return Exit.OK
        if text.startswith("/"):
            return self._slash(text)
        return self._prose(text)

    def _slash(self, text: str) -> int:
        from loom.cli.repl.commands import dispatch, resolve_alias

        words = shlex.split(text)
        command = resolve_alias(words[0].lstrip("/"))
        if command is None:
            self.out.error(f"no command {words[0]!r} — /help lists them")
            return Exit.USAGE
        if not command.subcommand:
            return self._session_command(command.name, words[1:])

        # `/edit` with a file in focus and no path takes it, so the common case
        # is the short one. Passing the path explicitly still wins.
        extra: list[str] = []
        if command.subcommand == "edit" and self.focus and len(words) < 3:
            text = " ".join([words[0], str(self.focus), *words[1:]])
        code = dispatch(text, defaults=extra)
        return Exit.USAGE if code is None else code

    def _prose(self, text: str) -> int:
        """Free text: an edit of the file in focus, or a new workflow."""
        from loom.cli.repl.commands import dispatch

        if self.focus is not None:
            edit = f"/edit {shlex.quote(str(self.focus))} {shlex.quote(text)}"
            return dispatch(edit) or Exit.OK

        destination = self._next_filename(text)
        # `--run` here and nowhere else. A line typed at this prompt is a
        # *task* — "find my jira tickets" is a question whose answer is the
        # tickets, and answering it with a Python file and the sentence
        # `loom run my_jira_tickets` is the last step of the loop failing.
        # `loom author "spec" > flow.py` keeps writing a file, because that
        # shape is documented and a run would put its output in the file.
        code = dispatch(
            f"/author {shlex.quote(text)} -o {shlex.quote(str(destination))}",
            defaults=["--run"],
        )
        if destination.exists():
            # Focus follows what was just written, so the next line is an edit
            # of it. That is the loop this session exists for.
            self.focus = destination
            self.out.line(f"  [dim]working on {esc(destination)}[/dim]")
        return code or Exit.OK

    # -- session-owned commands ----------------------------------------------

    def _session_command(self, name: str, rest: list[str]) -> int:
        if name in ("exit", "quit"):
            self._closing = True
            return Exit.OK
        if name == "help":
            return self._help()
        if name == "clear":
            print("\033[2J\033[H", end="")
            return Exit.OK
        if name == "new":
            self.focus = None
            self.out.line(
                "  [dim]no file in focus — the next description writes one[/dim]"
            )
            return Exit.OK
        if name == "open":
            return self._open(rest)
        if name == "status":
            return self._status()
        return Exit.USAGE

    def _open(self, rest: list[str]) -> int:
        if not rest:
            self.out.error("which file? /open flows/digest.py")
            return Exit.USAGE
        path = Path(rest[0].lstrip("@")).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            self.out.error(f"no such file: {path}")
            return Exit.USAGE
        self.focus = path
        self.out.line(f"  [dim]working on {esc(path)}[/dim]")
        return Exit.OK

    def _help(self) -> int:
        from loom.cli.repl.commands import known

        self.out.line()
        self.out.line("  [bold]Type what you want.[/bold] With a file in focus that")
        self.out.line("  [dim]changes it; with none it writes a new one.[/dim]")
        self.out.line()
        for command in known():
            self.out.line(
                f"  [cyan]/{esc(command.name):<11}[/cyan] [dim]{esc(command.summary)}[/dim]"
            )
        self.out.line()
        self.out.line("  [dim]#workflow and @file complete on Tab. Ctrl+D exits.[/dim]")
        return Exit.OK

    def _status(self) -> int:
        from loom.cli.repl.commands import dispatch

        return dispatch("/doctor") or Exit.OK

    # -- helpers -------------------------------------------------------------

    def _next_filename(self, spec: str) -> Path:
        """A file to write a new workflow to, derived from the description.

        Named rather than numbered, because a session that produces
        ``workflow_3.py`` is one whose output nobody can find afterwards. A
        collision takes a suffix rather than overwriting: this decides where a
        *new* workflow goes, and nothing here has been shown a diff yet.
        """
        words = [
            part
            for part in "".join(
                c if c.isalnum() or c.isspace() else " " for c in spec.lower()
            ).split()
            if part not in _STOPWORDS
        ][:3]
        stem = "_".join(words) or "workflow"
        directory = self.root / "flows"
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / f"{stem}.py"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{stem}_{suffix}.py"
            suffix += 1
        return candidate

    def banner(self) -> None:
        parked = _parked_count(self.project)
        where = self.project.store_url
        if len(where) > 44:
            where = "…" + where[-43:]
        self.out.line()
        self.out.line(
            f"  [magenta]{BANNER_GLYPH}[/magenta] [bold]Loom[/bold]  "
            f"[dim]{esc(self.root.name)}[/dim]  [dim]{esc(where)}[/dim]"
            + (f"  [yellow]{parked} parked[/yellow]" if parked else "")
        )
        if self.project.ephemeral:
            self.out.line(
                "  [yellow]Runs are kept in memory here and lost on exit — "
                "there is no pyproject.toml above this directory.[/yellow]"
            )
        self.out.line("  [dim]Describe a workflow, or /help.[/dim]")
        self.out.line()


#: Dropped when a description becomes a filename. Short and unclever on
#: purpose: it exists to keep `flows/a_workflow_that.py` from happening, not to
#: understand English.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "to", "for", "of", "from", "with",
        "that", "which", "this", "it", "me", "my", "i", "we", "our",
    }
)


def _parked_count(project: ProjectConfig) -> int:
    """How many runs are waiting on a person. ``0`` on any failure.

    Reported in the banner because it is the question `loom ui` exists to
    answer, and a session that cannot answer it sends you to another program.
    A store that cannot be read is `loom doctor`'s finding, not the banner's.
    """
    import asyncio
    import contextlib

    async def count() -> int:
        from loom.stores import from_url

        store = from_url(project.store_url)
        try:
            records = await store.list_executions(status="suspended", limit=200)
            return len(records)
        finally:
            with contextlib.suppress(Exception):
                await store.close()

    try:
        return asyncio.run(count())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_session(args: Any) -> int:
    """Open a session. Returns the process exit code.

    Refuses rather than degrading when ``prompt_toolkit`` is absent: a line
    editor without history, completion or a usable Ctrl+C is not a smaller
    version of this, it is a worse one, and ``input()`` in a loop is how a
    session eats a paste.
    """
    out = Printer(debug=getattr(args, "debug", False))
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        out.error("the session needs the cli extra: pip install 'loomsdk[cli]'")
        out.hint("every command is still available directly: loom --help")
        return Exit.USAGE

    from loom.cli.repl.complete import LoomCompleter

    project = ProjectConfig.discover(store=getattr(args, "store", None))
    project.prepare()
    session = Session(project=project, out=out)
    session.banner()

    history_path = None
    if project.root is not None:
        (project.root / ".loom").mkdir(parents=True, exist_ok=True)
        history_path = project.root / ".loom" / HISTORY_FILE

    bindings = KeyBindings()
    prompt: Any = PromptSession(
        history=FileHistory(str(history_path)) if history_path else None,
        completer=_completer(LoomCompleter(session)),
        complete_while_typing=True,
        key_bindings=bindings,
        enable_history_search=True,
    )

    last = Exit.OK
    while not session._closing:
        try:
            line = prompt.prompt("> ")
        except KeyboardInterrupt:
            # Ctrl+C abandons the line being typed, as a shell does. Ending the
            # session on it would make an interrupted command an ended
            # conversation, which is the behaviour a REPL exists to avoid.
            continue
        except EOFError:
            break
        try:
            last = Exit(session.handle(line))
        except SystemExit as exit_:
            # A handler that called sys.exit. Reported, never obeyed: one
            # command must not be able to end the session.
            last = Exit(int(exit_.code or 0))
        except Exception as exc:
            from loom.cli.commands import unexpected

            unexpected(exc, debug=getattr(args, "debug", False))
            last = Exit.FAILED

    out.line("  [dim]bye[/dim]")
    # The last command's code, so `loom < script.txt` is still scriptable —
    # and 0 for a session someone simply closed.
    return Exit.OK if last in (Exit.USAGE, Exit.OK) else last


def _completer(completer: Any) -> Any:
    """Adapt our completer to ``prompt_toolkit``'s base class.

    Subclassing at import time would make this module import
    ``prompt_toolkit``, which is an optional extra — and this module is
    imported by ``loom.cli`` to decide whether a session is even possible.
    """
    from prompt_toolkit.completion import Completer

    class _Adapter(Completer):  # type: ignore[misc]
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            return completer.get_completions(document, complete_event)

    return _Adapter()
