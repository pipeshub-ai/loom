"""Terminal rendering and exit codes.

Two rules shape everything here:

* **Every command can emit JSON.** A CLI you cannot pipe into ``jq`` is half a
  CLI, and this one will run in CI.
* **Colour is for humans only.** Styling is written through ``rich`` when stdout
  is a TTY and stripped when it is not, so redirecting to a file captures text
  rather than escape codes.

``rich`` is an optional extra. Without it every renderer falls back to plain
text — narrower, but never broken.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from enum import IntEnum
from typing import Any


class Exit(IntEnum):
    """Process exit codes.

    ``SUSPENDED`` is the one that matters. A run parked for three weeks waiting
    on a human has neither succeeded nor failed, and collapsing it into either
    makes calling scripts do the wrong thing.

    ``INTERRUPTED`` follows the shell's convention of ``128 + signum`` rather
    than joining the small numbers above, and is 130 for a Ctrl+C and 143 for a
    ``docker stop``. Reporting either as ``USAGE`` — which is what happened
    before — tells a calling script to go fix its arguments.
    """

    OK = 0
    FAILED = 1
    USAGE = 2
    SUSPENDED = 3
    CANCELLED = 4
    INTERRUPTED = 130


#: Terminal run statuses mapped to the code the process should exit with.
STATUS_EXIT: dict[str, Exit] = {
    "completed": Exit.OK,
    "failed": Exit.FAILED,
    "suspended": Exit.SUSPENDED,
    "cancelled": Exit.CANCELLED,
    "running": Exit.OK,
    "pending": Exit.OK,
}

#: Colour and glyph per status. The glyph carries the meaning on its own, so the
#: output still reads in a monochrome terminal or to a colour-blind reader.
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "completed": ("green", "●"),
    "failed": ("red", "✗"),
    "suspended": ("yellow", "◐"),
    "cancelled": ("dim", "⊘"),
    "running": ("cyan", "▸"),
    "pending": ("dim", "·"),
    "waiting": ("yellow", "◐"),
    "skipped": ("dim", "-"),
}


def _rich(*, stderr: bool = False) -> Any:
    """A rich console, or ``None`` when rich is not installed.

    ``stderr`` is a construction-time choice in rich, not a per-call one — the
    stream has to be bound to the Console.
    """
    try:
        from rich.console import Console
    except ImportError:
        return None
    # force_terminal is left unset so rich strips styling when piped.
    return Console(highlight=False, soft_wrap=False, stderr=stderr)


class Printer:
    """Renders command output as either text for people or JSON for programs."""

    def __init__(self, *, as_json: bool = False, quiet: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self._console = None if as_json else _rich()
        # Errors go to stderr even in JSON mode, so `> out.json` stays valid
        # while the operator still sees what went wrong.
        self._errors = _rich(stderr=True)

    # -- primitives ----------------------------------------------------------

    def line(self, text: str = "", *, style: str = "") -> None:
        """One line of human output. Suppressed entirely in JSON mode."""
        if self.as_json or self.quiet:
            return
        if self._console is not None and style:
            self._console.print(text, style=style)
        elif self._console is not None:
            self._console.print(text)
        else:
            print(_strip_markup(text))

    def verbatim(self, text: str) -> None:
        """Text printed exactly as given, with no markup interpretation.

        ``line`` renders through rich, which reads ``[human]`` as a style tag
        and prints nothing where the category was. Anything that is *code* — a
        rendered node contract, a generated snippet — has to come through here,
        because the whole point of it is that a reader can copy it and have it
        work.
        """
        if self.as_json or self.quiet:
            return
        if self._console is not None:
            self._console.print(text, markup=False, highlight=False)
        else:
            print(text)

    def error(self, text: str) -> None:
        """A problem, on stderr so it survives ``> out.json``."""
        if self._errors is not None:
            self._errors.print(f"[red]{text}[/red]")
        else:
            print(text, file=sys.stderr)

    def hint(self, text: str) -> None:
        """A suggested next command."""
        self.line(f"  [dim]{text}[/dim]" if self._console else f"  {text}")

    def json(self, payload: Any) -> None:
        """Emit the machine-readable form. Only in JSON mode."""
        if self.as_json:
            print(json.dumps(payload, indent=2, default=str))

    # -- domain renderers ----------------------------------------------------

    def status(self, run: dict[str, Any], *, prefix: str = "") -> None:
        """One run's headline: id, status, workflow."""
        state = str(run.get("status", ""))
        style, glyph = _STATUS_STYLE.get(state, ("", "·"))
        run_id = run.get("run_id", "")
        workflow = run.get("workflow", "")
        if self._console is not None:
            self._console.print(
                f"{prefix}[{style}]{glyph}[/{style}] "
                f"[bold]{run_id}[/bold]  [{style}]{state}[/{style}]  [dim]{workflow}[/dim]"
            )
        else:
            self.line(f"{prefix}{glyph} {run_id}  {state}  {workflow}")

    def table(
        self,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        title: str = "",
        status_column: int | None = None,
    ) -> None:
        """A table, styled when rich is present and aligned when it is not."""
        materialised = [list(row) for row in rows]
        if not materialised:
            self.line("[dim]none[/dim]" if self._console else "none")
            return

        if self._console is None:
            widths = [
                max(len(str(columns[i])), *(len(str(r[i])) for r in materialised))
                for i in range(len(columns))
            ]
            self.line("  ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=True)))
            for row in materialised:
                self.line(
                    "  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True))
                )
            return

        from rich.table import Table

        table = Table(title=title or None, box=None, pad_edge=False, header_style="dim")
        for column in columns:
            table.add_column(column)
        for row in materialised:
            cells = [str(value) for value in row]
            if status_column is not None and status_column < len(cells):
                state = cells[status_column]
                style, _ = _STATUS_STYLE.get(state, ("", ""))
                if style:
                    cells[status_column] = f"[{style}]{state}[/{style}]"
            table.add_row(*cells)
        self._console.print(table)

    def journal(self, entries: Sequence[dict[str, Any]]) -> None:
        """The durable operations of a run, in order."""
        self.table(
            ["seq", "step", "kind", "status", "attempts"],
            [
                [
                    entry.get("seq", ""),
                    entry.get("step_id") or entry.get("name", ""),
                    entry.get("kind", ""),
                    entry.get("status", ""),
                    entry.get("attempts", ""),
                ]
                for entry in entries
            ],
            status_column=3,
        )

    def value(self, label: str, value: Any) -> None:
        """A single labelled field, skipped when empty."""
        if value in (None, "", [], {}):
            return
        rendered = value if isinstance(value, str) else json.dumps(value, default=str)
        if self._console is not None:
            self._console.print(f"  [dim]{label:<9}[/dim] {rendered}")
        else:
            self.line(f"  {label:<9} {rendered}")


def _strip_markup(text: str) -> str:
    """Remove rich markup so the plain-text path is not littered with tags."""
    import re

    return re.sub(r"\[/?[a-z][a-z0-9 _#]*\]", "", text)


def exit_for(run: dict[str, Any]) -> Exit:
    """The process exit code implied by a run's terminal status."""
    return STATUS_EXIT.get(str(run.get("status", "")), Exit.OK)
