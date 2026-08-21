"""Terminal rendering and exit codes.

Three rules shape everything here:

* **Every command can emit JSON.** A CLI you cannot pipe into ``jq`` is half a
  CLI, and this one will run in CI.
* **Colour is for humans only.** Styling is written through ``rich`` when stdout
  is a TTY and stripped when it is not, so redirecting to a file captures text
  rather than escape codes.
* **Data never reaches the markup parser.** A run's output, a step id, an
  exception message — none of it is ours, and ``rich`` reads ``[/tag]`` in it as
  a closing style tag. That used to raise ``MarkupError`` from inside
  ``Printer.value``, which nothing caught, so a run that *completed* printed a
  traceback and exited 1. The renderers that carry data build ``Text`` objects
  instead, which have no markup to parse; the ones that carry our own literals
  keep markup and are guarded, because a rendering fault must never be able to
  change what a command reports.

``rich`` is an optional extra. Without it every renderer falls back to plain
text — narrower, but never broken.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from enum import IntEnum
from typing import Any


class Exit(IntEnum):
    """Process exit codes.

    ``SUSPENDED`` is the one that matters. A run parked for three weeks waiting
    on a human has neither succeeded nor failed, and collapsing it into either
    makes calling scripts do the wrong thing. It is also what a ``watch`` that
    ran out of patience reports: the run is still going, which is not success.

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
#:
#: ``running`` and ``pending`` are deliberately absent. A command that stops
#: looking at a run still in flight has not watched it succeed, and mapping
#: those to ``OK`` is how ``loom watch --timeout`` reported a live run as a
#: pass. :func:`exit_for` answers for them explicitly.
STATUS_EXIT: dict[str, Exit] = {
    "completed": Exit.OK,
    "failed": Exit.FAILED,
    "suspended": Exit.SUSPENDED,
    "cancelled": Exit.CANCELLED,
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

#: The style tags this package actually writes. ``_strip_markup`` removes these
#: and nothing else — the old pattern matched any bracketed lowercase word, so
#: the plain-text path deleted ``[dev]`` out of
#: ``pip install -e '.[dev]'`` and handed a new user an install line that leaves
#: them without pytest, which the next line of the same message tells them to
#: run.
_TAGS = (
    "dim",
    "bold",
    "italic",
    "red",
    "green",
    "yellow",
    "cyan",
    "blue",
    "magenta",
    "white",
)
_MARKUP = re.compile(r"\[/?(?:" + "|".join(_TAGS) + r")\]")


def esc(value: Any) -> str:
    """Text that must survive markup rendering unchanged.

    For the call sites that interpolate data into a string which is otherwise
    ours — a step name inside a ``[dim]`` sequence number, a toolset summary
    beside a version. The structured renderers below need no help; this is for
    :meth:`Printer.line`, which stays markup-capable because its templates are
    literals in this repository.
    """
    text = value if isinstance(value, str) else str(value)
    try:
        from rich.markup import escape
    except ImportError:
        return text
    return str(escape(text))


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


def _text(value: Any = "", style: str = "") -> Any:
    """A ``rich.text.Text``, which carries styling without parsing markup."""
    from rich.text import Text

    return Text(value if isinstance(value, str) else str(value), style=style)


class Printer:
    """Renders command output as either text for people or JSON for programs."""

    def __init__(
        self, *, as_json: bool = False, quiet: bool = False, debug: bool = False
    ) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.debug = debug
        self._console = None if as_json else _rich()
        # Errors go to stderr even in JSON mode, so `> out.json` stays valid
        # while the operator still sees what went wrong.
        self._errors = _rich(stderr=True)

    # -- primitives ----------------------------------------------------------

    def line(self, text: str = "", *, style: str = "") -> None:
        """One line of human output. Suppressed entirely in JSON mode.

        Markup-capable, because every template passed to it is a literal in
        this repository. Data interpolated into one of those templates goes
        through :func:`esc` at the call site — and if a site is ever missed,
        the guard below prints it verbatim rather than letting a bracket end
        the command.
        """
        if self._mute():
            return
        if self._console is None:
            print(_strip_markup(text))
            return
        try:
            if style:
                self._console.print(text, style=style)
            else:
                self._console.print(text)
        except Exception:
            # A markup fault is a rendering defect, never grounds for changing
            # what the command reports. Fall back to the escaped form.
            self._console.print(_text(_strip_markup(text)))

    def verbatim(self, text: str) -> None:
        """Text printed exactly as given, with no markup interpretation.

        ``line`` renders through rich, which reads ``[human]`` as a style tag
        and prints nothing where the category was. Anything that is *code* — a
        rendered node contract, a generated snippet — has to come through here,
        because the whole point of it is that a reader can copy it and have it
        work.
        """
        if self._mute():
            return
        if self._console is not None:
            self._console.print(text, markup=False, highlight=False)
        else:
            print(text)

    def error(self, text: str) -> None:
        """A problem, on stderr so it survives ``> out.json``.

        Built as ``Text`` rather than ``[red]{text}[/red]``: most of what
        reaches here is an exception message, which is the least trustworthy
        string the CLI handles and the one most likely to contain a bracket.
        """
        if self._errors is not None:
            self._errors.print(_text(text, "red"))
        else:
            print(text, file=sys.stderr)

    def hint(self, text: str) -> None:
        """A suggested next command."""
        if self._mute():
            return
        if self._console is not None:
            self._console.print(_text(f"  {text}", "dim"))
        else:
            print(f"  {text}")

    def json(self, payload: Any) -> None:
        """Emit the machine-readable form. Only in JSON mode."""
        if self.as_json:
            print(json.dumps(payload, indent=2, default=str))

    # -- domain renderers ----------------------------------------------------

    def _mute(self) -> bool:
        """Whether human output is suppressed.

        Checked by each domain renderer rather than only by :meth:`line`: they
        write to the stream directly, so that data never passes through the
        markup parser, and would otherwise corrupt ``--json`` output.
        """
        return self.as_json or self.quiet

    def status(self, run: dict[str, Any], *, prefix: str = "") -> None:
        """One run's headline: id, status, workflow."""
        if self._mute():
            return
        state = str(run.get("status", ""))
        style, glyph = _STATUS_STYLE.get(state, ("", "·"))
        run_id = str(run.get("run_id", ""))
        workflow = str(run.get("workflow", ""))
        if self._console is None:
            self.line(f"{prefix}{glyph} {run_id}  {state}  {workflow}")
            return
        rendered = _text(prefix)
        rendered.append(f"{glyph} ", style=style)
        rendered.append(run_id, style="bold")
        rendered.append("  ")
        rendered.append(state, style=style)
        rendered.append("  ")
        rendered.append(workflow, style="dim")
        self._console.print(rendered)

    def table(
        self,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        title: str = "",
        status_column: int | None = None,
    ) -> None:
        """A table, styled when rich is present and aligned when it is not.

        Cells are ``Text``: every one of them is data — a run id, a workflow
        name, a toolset summary — and a table is where the most of it appears
        at once.
        """
        if self._mute():
            return
        materialised = [list(row) for row in rows]
        if not materialised:
            self.line("[dim]none[/dim]" if self._console else "none")
            return

        if self._console is None:
            widths = [
                max(len(str(columns[i])), *(len(str(r[i])) for r in materialised))
                for i in range(len(columns))
            ]
            print(
                "  ".join(
                    str(c).ljust(w) for c, w in zip(columns, widths, strict=True)
                )
            )
            for row in materialised:
                print(
                    "  ".join(
                        str(v).ljust(w) for v, w in zip(row, widths, strict=True)
                    )
                )
            return

        from rich.table import Table

        table = Table(title=title or None, box=None, pad_edge=False, header_style="dim")
        for column in columns:
            table.add_column(str(column))
        for row in materialised:
            cells = [_text(value) for value in row]
            if status_column is not None and status_column < len(cells):
                style, _ = _STATUS_STYLE.get(str(row[status_column]), ("", ""))
                if style:
                    cells[status_column].stylize(style)
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
        """A single labelled field, skipped when empty.

        This is where a workflow's own output is printed, so it is the renderer
        that must never interpret what it is given.
        """
        if self._mute() or value in (None, "", [], {}):
            return
        rendered = value if isinstance(value, str) else json.dumps(value, default=str)
        if self._console is None:
            print(f"  {label:<9} {rendered}")
            return
        line = _text(f"  {label:<9} ", "dim")
        line.append(rendered)
        self._console.print(line)


def _strip_markup(text: str) -> str:
    """Remove the style tags this package writes, and nothing else.

    Narrow on purpose: a pattern matching any bracketed word deletes real
    content — ``.[dev]`` out of a pip command, ``[0]`` out of an error message —
    and the plain-text path is the one a CI log captures.
    """
    return _MARKUP.sub("", text)


def exit_for(run: dict[str, Any], *, settled: bool = True) -> Exit:
    """The process exit code implied by a run's status.

    *settled* is how a caller says it stopped looking before the run stopped
    moving. ``loom watch --timeout`` used to report a run still in flight as
    ``OK``, so a CI job that gave up after five minutes went green — the exact
    conflation the ``SUSPENDED`` code exists to prevent, one state over. A run
    that is neither done nor failed is ``SUSPENDED`` whether it is parked on a
    person or simply still working.
    """
    state = str(run.get("status", ""))
    if state in STATUS_EXIT:
        return STATUS_EXIT[state]
    return Exit.OK if settled else Exit.SUSPENDED
