"""Nothing is written before it has been shown.

``loom edit`` called ``destination.write_text(result["code"])`` and *then*
printed the diff, the graph delta and the explanation. No confirmation, no
backup, and no check that the file was tracked by anything — so the first
answer to "what did that instruction do to my workflow?" arrived after the
answer was the only copy. ``loom author -o existing.py`` clobbered silently for
the same reason: the write was the first thing it did with the path.

Ordering is most of the fix. The diff was already computed — ``EditResult``
carries it, along with ``graph_changes``, which is the reviewable form for
somebody who does not read Python. What was missing was showing it *first*.

The rest is a permission ladder that remembers. A prompt on every write is a
prompt people learn to answer without reading, which is worse than no prompt at
all; "yes, and don't ask again for this file" is what keeps the question
meaningful for the writes that are not routine.

**Non-interactive denies.** A gate that could not run has not passed — the rule
``before`` hooks already follow in ``runtime/hooks.py``. ``--yes`` is the
explicit override, and the refusal names it, because a CI job that silently did
nothing is worse than one that stopped.
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from loom.cli.output import Exit, Printer, esc

__all__ = [
    "Allowlist",
    "Decision",
    "FileChange",
    "apply",
    "propose",
    "render",
    "session_allowlist",
]


class Decision(StrEnum):
    """What a person said about one proposed write."""

    APPLY = "apply"
    APPLY_ALWAYS = "apply-always"
    """Apply, and stop asking about this path for the rest of the session."""
    REFUSE = "refuse"


@dataclass
class FileChange:
    """A write that has not happened yet."""

    path: Path
    after: str
    #: Rendered by the caller when it has a better one than a text diff — an
    #: edit already carries a unified diff computed against the source the
    #: model was given, which is the diff the *model* acted on.
    diff: str = ""
    #: ``+summarise -fetch``, projected from both versions. The reviewable form
    #: for a reader who does not read Python, and the one that says whether the
    #: shape of the workflow changed rather than only its text.
    graph_changes: tuple[str, ...] = ()
    explanation: str = ""

    @property
    def creating(self) -> bool:
        return not self.path.exists()

    def before(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def unified(self) -> str:
        """The diff to show. The caller's, or one computed from disk.

        Computed here as a fallback rather than required from the caller, so a
        surface that has only "here is the new content" still cannot write
        without showing what it replaces.
        """
        if self.diff:
            return self.diff
        before, after = self.before(), self.after
        if before == after:
            return ""
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{self.path.name}",
                tofile=f"b/{self.path.name}",
                n=3,
            )
        )

    def summary(self) -> str:
        """``+48 -0`` — how much moved, for the line above the prompt."""
        added = removed = 0
        for line in self.unified().splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return f"+{added} -{removed}"


@dataclass
class Allowlist:
    """Paths a person has stopped being asked about.

    Per **session**, not per project, and not written to disk. "Don't ask again"
    is a statement about the next few minutes of work on a file somebody is
    watching; persisting it would silently disarm the gate for a later session
    that has a different reason to care — including one running unattended.
    """

    paths: set[Path]

    def __init__(self) -> None:
        self.paths = set()

    def allows(self, path: Path) -> bool:
        return path.resolve() in self.paths

    def remember(self, path: Path) -> None:
        self.paths.add(path.resolve())


#: The allowlist a session accumulates. Process-level because the session
#: dispatches through argparse — a slash command *is* the subcommand, parsed by
#: the same parser — so there is no object to thread an allowlist through
#: without giving every handler a parameter for it.
#:
#: Harmless for a one-shot command, which asks once and exits, and correct for a
#: session, which is the only thing that can accumulate one. It is never written
#: to disk: "don't ask again" is a statement about the next few minutes of work
#: on a file somebody is watching, and persisting it would silently disarm the
#: gate for a later run that has a different reason to care — including an
#: unattended one.
_SESSION: Allowlist | None = None


def session_allowlist() -> Allowlist:
    """The allowlist for this process, created on first use."""
    global _SESSION
    if _SESSION is None:
        _SESSION = Allowlist()
    return _SESSION


def forget_allowlist() -> None:
    """Drop it. For tests, and for a session that wants to be asked again."""
    global _SESSION
    _SESSION = None


def propose(
    change: FileChange,
    out: Printer,
    *,
    assume_yes: bool = False,
    allowlist: Allowlist | None = None,
    interactive: bool | None = None,
) -> Decision:
    """Show the change, ask, and return what was decided. Writes nothing.

    Separated from applying it so the caller keeps the choice of what a refusal
    means — a session returns to the prompt, a command exits 2.
    """
    if assume_yes:
        return Decision.APPLY
    if allowlist is not None and allowlist.allows(change.path):
        return Decision.APPLY

    render(change, out)

    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        # A gate that could not run has not passed.
        out.error(
            f"refusing to write {change.path} without confirmation. "
            "Pass --yes to write it, or --dry-run to see the diff only."
        )
        return Decision.REFUSE

    return ask(change, out, allowlist=allowlist)


def render(change: FileChange, out: Printer) -> None:
    """The diff, the graph delta, and the explanation — before anything else."""
    verb = "create" if change.creating else "edit"
    out.line()
    out.line(
        f"  [bold]{verb} {esc(change.path)}[/bold]  [dim]{esc(change.summary())}[/dim]"
    )
    diff = change.unified()
    if diff:
        # `verbatim`: a diff is full of characters rich reads as markup, and a
        # diff shown with pieces missing is worse than none.
        out.verbatim(_colourless(diff))
    if change.graph_changes:
        out.line(f"  [dim]graph: {esc(' '.join(change.graph_changes))}[/dim]")
    if change.explanation:
        out.line(f"  [dim]{esc(change.explanation)}[/dim]")


def ask(
    change: FileChange, out: Printer, *, allowlist: Allowlist | None = None
) -> Decision:
    """Put the question, and read one answer."""
    verb = "Create" if change.creating else "Apply"
    out.line()
    out.line(f"  [bold]{verb} this?[/bold]")
    out.line("    [cyan]1.[/cyan] Yes")
    out.line(f"    [cyan]2.[/cyan] Yes, and don't ask again for {esc(change.path.name)}")
    out.line("    [cyan]3.[/cyan] No")
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # Interrupting the question is not consent to the write.
        out.line()
        return Decision.REFUSE

    if answer in ("2", "a", "always"):
        if allowlist is not None:
            allowlist.remember(change.path)
        return Decision.APPLY_ALWAYS
    if answer in ("", "1", "y", "yes"):
        return Decision.APPLY
    return Decision.REFUSE


def apply(change: FileChange, out: Printer) -> Exit:
    """Write it, having been told to."""
    try:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_text(change.after, encoding="utf-8")
    except OSError as exc:
        out.error(f"could not write {change.path}: {exc}")
        return Exit.FAILED
    out.line(f"  [green]wrote {esc(change.path)}[/green]")
    return Exit.OK


def _colourless(diff: str) -> str:
    """A diff as text.

    Deliberately not syntax-coloured: ``verbatim`` is the only renderer that
    passes a string through untouched, and colouring would mean going back
    through the markup parser with the one kind of content most likely to
    contain a bracket.
    """
    return diff.rstrip("\n")
