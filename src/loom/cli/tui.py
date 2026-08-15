"""Terminal UI for watching and steering runs.

Most tools do not need a TUI. A durable execution engine does, because its
central question — *what are my runs doing, and which are stuck waiting on me?*
— is live, multi-object, and not answerable by one command's output.

Three panes, each answering one part of it:

* **Runs** — every run, filterable, refreshing on a timer.
* **Journal** — the selected run's durable operations, with status as they land.
* **Waiting** — runs parked on a human, actionable in place. Finding these
  otherwise means knowing they exist and querying for them.

Driven by the same :class:`CliBackend` the CLI uses, so it works against an
in-process Runtime or a remote server with no change.

    pip install loomflow[tui]
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static

#: How often the run list re-reads the store, in seconds. Runs move at the
#: granularity of steps, so anything faster is wasted queries.
REFRESH_SECONDS = 1.5

_GLYPH = {
    "completed": "[green]●[/green]",
    "failed": "[red]✗[/red]",
    "suspended": "[yellow]◐[/yellow]",
    "cancelled": "[dim]⊘[/dim]",
    "running": "[cyan]▸[/cyan]",
    "pending": "[dim]·[/dim]",
}


def _glyph(status: str) -> str:
    return _GLYPH.get(status, "[dim]·[/dim]")


class LoomApp(App[int]):
    """The LOOM terminal UI."""

    CSS: ClassVar[str] = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #left { width: 34; min-width: 30; border-right: solid $panel; }
    #right { width: 1fr; }
    #filter { dock: bottom; display: none; }
    #filter.visible { display: block; }
    #detail { height: auto; padding: 0 1; }
    #waiting { height: auto; padding: 0 1; color: $warning; }
    DataTable { height: 1fr; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "quit"),
        Binding("r", "retry", "retry"),
        Binding("c", "cancel", "cancel"),
        Binding("p", "replay", "replay"),
        Binding("a", "approve", "approve"),
        Binding("x", "reject", "reject"),
        Binding("slash", "filter", "filter"),
        Binding("escape", "clear_filter", "clear", show=False),
    ]

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self._backend = backend
        self._runs: list[dict[str, Any]] = []
        self._selected: str | None = None
        self._filter = ""

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="runs", cursor_type="row")
            with Vertical(id="right"):
                yield Static("", id="detail")
                yield DataTable(id="journal", cursor_type="row")
                yield Static("", id="waiting")
        # Disabled while hidden: a CSS-hidden Input is still focusable, and if it
        # takes focus on start it swallows every shortcut as text.
        yield Input(placeholder="filter by workflow or status…", id="filter", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        runs = self.query_one("#runs", DataTable)
        runs.add_columns(" ", "run", "workflow", "status")
        journal = self.query_one("#journal", DataTable)
        journal.add_columns("seq", "step", "status", "att")
        # Focus the list explicitly so the key bindings work immediately.
        runs.focus()
        self.set_interval(REFRESH_SECONDS, self.refresh_runs)
        self.refresh_runs()

    # -- data ----------------------------------------------------------------

    @work(exclusive=True)
    async def refresh_runs(self) -> None:
        """Reload the run list, preserving the cursor where possible."""
        try:
            runs = await self._backend.list_runs(limit=200)
        except Exception as exc:  # a transient store or network error
            self.query_one("#detail", Static).update(f"[red]{exc}[/red]")
            return

        if self._filter:
            needle = self._filter.lower()
            runs = [
                run
                for run in runs
                if needle in str(run.get("workflow", "")).lower()
                or needle in str(run.get("status", "")).lower()
            ]
        self._runs = runs

        table = self.query_one("#runs", DataTable)
        cursor = table.cursor_row
        table.clear()
        for run in runs:
            status = str(run.get("status", ""))
            table.add_row(
                _glyph(status),
                str(run.get("run_id", ""))[:14] + "…",
                str(run.get("workflow", "")),
                status,
            )
        if runs:
            table.move_cursor(row=min(cursor, len(runs) - 1))
            await self._select(min(cursor, len(runs) - 1))

        waiting = [r for r in runs if r.get("status") == "suspended"]
        self.query_one("#waiting", Static).update(
            f"◐ {len(waiting)} run(s) waiting" if waiting else ""
        )

    async def _select(self, index: int) -> None:
        if not (0 <= index < len(self._runs)):
            return
        run = self._runs[index]
        run_id = str(run.get("run_id", ""))
        self._selected = run_id

        status = str(run.get("status", ""))
        lines = [f"{_glyph(status)} [bold]{run_id}[/bold]  {run.get('workflow', '')}"]
        if run.get("output") not in (None, ""):
            lines.append(f"[dim]output[/dim] {str(run.get('output'))[:200]}")
        if run.get("error"):
            lines.append(f"[red]error[/red]  {run['error']}")
        awaiting = run.get("awaiting_event")
        if awaiting:
            action = (
                "[green][a][/green] approve  [red][x][/red] reject"
                if str(awaiting).startswith("approval:")
                else ""
            )
            lines.append(f"[yellow]▸ waiting on '{awaiting}'[/yellow]  {action}")
        self.query_one("#detail", Static).update("\n".join(lines))

        journal = self.query_one("#journal", DataTable)
        journal.clear()
        try:
            entries = await self._backend.journal(run_id)
        except Exception:
            return
        for entry in entries:
            journal.add_row(
                str(entry.get("seq", "")),
                str(entry.get("step_id", "")),
                str(entry.get("status", "")),
                str(entry.get("attempts", "")),
            )

    async def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.data_table.id == "runs":
            await self._select(event.cursor_row)

    # -- actions -------------------------------------------------------------

    def action_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.disabled = False
        field.add_class("visible")
        field.focus()

    def action_clear_filter(self) -> None:
        self._filter = ""
        self._hide_filter(clear=True)
        self.refresh_runs()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        self._filter = event.value.strip()
        self._hide_filter()
        self.refresh_runs()

    def _hide_filter(self, *, clear: bool = False) -> None:
        field = self.query_one("#filter", Input)
        if clear:
            field.value = ""
        field.remove_class("visible")
        field.disabled = True
        self.query_one("#runs", DataTable).focus()

    @work
    async def action_retry(self) -> None:
        await self._operate("retry")

    @work
    async def action_cancel(self) -> None:
        await self._operate("cancel")

    @work
    async def action_replay(self) -> None:
        await self._operate("replay")

    @work
    async def action_approve(self) -> None:
        await self._decide(approved=True)

    @work
    async def action_reject(self) -> None:
        await self._decide(approved=False)

    async def _operate(self, action: str) -> None:
        if self._selected is None:
            return
        try:
            await getattr(self._backend, action)(self._selected)
            self.notify(f"{action} {self._selected[:16]}…")
        except Exception as exc:
            self.notify(str(exc), severity="error")
        self.refresh_runs()

    async def _decide(self, *, approved: bool) -> None:
        """Answer the approval the selected run is parked on."""
        run = next(
            (r for r in self._runs if r.get("run_id") == self._selected), None
        )
        awaiting = str(run.get("awaiting_event", "")) if run else ""
        if not awaiting.startswith("approval:"):
            self.notify("selected run is not waiting for an approval", severity="warning")
            return

        try:
            await self._backend.send_event(
                self._selected, awaiting, {"approved": approved}
            )
            self.notify(f"{'approved' if approved else 'rejected'} {awaiting}")
        except Exception as exc:
            self.notify(str(exc), severity="error")
        self.refresh_runs()

    async def on_unmount(self) -> None:
        await self._backend.close()


def run_tui(backend: Any) -> int:
    """Launch the UI. Returns a process exit code."""
    app = LoomApp(backend)
    result = app.run()
    # Textual returns whatever `exit()` was given; a clean quit yields None.
    return int(result) if isinstance(result, int) else 0


__all__ = ["LoomApp", "run_tui"]


if __name__ == "__main__":  # pragma: no cover
    from loom.cli.targets import resolve

    asyncio.run(asyncio.sleep(0))
    raise SystemExit(run_tui(resolve(None).backend))
