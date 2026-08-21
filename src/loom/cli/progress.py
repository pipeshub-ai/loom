"""What the agent is doing, while it is doing it.

``loom author`` awaited one coroutine. Behind that await: up to twenty discovery
turns, a sixteen-stage verification pipeline, up to three repair rounds each
re-invoking the model, a subprocess smoke run, a replay, and optionally a second
critique model. The user saw nothing until all of it was over — and
``coding_agent`` has seventeen ``logger.info`` calls narrating exactly the right
things, every one of them going to a logger the CLI never configured.

This renders that. It is a **consumer of seams that already existed**: the agent
hook family the runner drives (``agent_start`` … ``tool_start``/``tool_end`` …
``agent_end``) and the ``on_stage`` callback on :class:`CheckPipeline`. Nothing
here reaches into the agent; unplugging it leaves the agent exactly as it was.

Three rules:

* **Progress goes to stderr.** ``loom author "spec" > flow.py`` must put the
  *code* in the file. It is also where :class:`CLIUserInteraction` writes, so a
  question the agent asks and the progress around it share one stream and
  interleave correctly.
* **It cannot change the outcome.** Every callback is wrapped so a rendering
  fault degrades the display and nothing else — the rule the non-deciding hook
  families already follow.
* **No terminal, no live region.** Piped or redirected, it emits one line per
  event, which is a log. A live region rewritten into a file is a file full of
  escape codes.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ProgressRenderer", "brief_arguments"]

#: Longest rendering of one call's arguments. A tool call is a headline, not
#: the payload — ``search_toolsets("jira issues")`` says what is happening and
#: three lines of JSON schema does not.
ARGUMENT_WIDTH = 56

#: Glyphs, sharing the run-status vocabulary in ``cli.output`` so a reader does
#: not have to learn a second one.
_RUNNING = "◐"
_DONE = "⏺"
_OK = "✓"
_WARN = "!"
_FAIL = "✗"
_SKIP = "-"


def brief_arguments(arguments: Any) -> str:
    """One line naming what a call was asked for.

    Values are truncated individually rather than the whole rendering being
    cut, so a call with one long argument and three short ones still shows all
    four names — which is the part that says what the agent is doing.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""
    parts: list[str] = []
    budget = ARGUMENT_WIDTH
    for key, value in arguments.items():
        text = value if isinstance(value, str) else repr(value)
        room = max(8, budget // max(1, len(arguments) - len(parts)))
        if len(text) > room:
            text = text[: room - 1] + "…"
        rendered = f'"{text}"' if isinstance(value, str) else text
        parts.append(rendered if len(arguments) == 1 else f"{key}={rendered}")
        budget -= len(rendered)
        if budget <= 0:
            parts.append("…")
            break
    return ", ".join(parts)


@dataclass
class _Spend:
    """What the job has cost so far, accumulated from each model response.

    Read off ``model_end`` rather than asked of the budget, because the budget
    is charged once per *call* — after the whole turn loop returns — and the
    number worth showing is the one that moves while you are waiting.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    model: str = ""

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.model = getattr(response, "model", "") or self.model
        try:
            from loom.agents.models import estimate_cost
            from loom.core.models import Usage

            total = Usage(
                input_tokens=self.input_tokens, output_tokens=self.output_tokens
            )
            self.cost_usd = estimate_cost(self.model, total)
        except Exception:
            # An unpriced model reports no dollars rather than a wrong number.
            pass

    def describe(self) -> str:
        tokens = (
            f"{self.total / 1000:.1f}k" if self.total >= 1000 else str(self.total)
        )
        money = f" · ${self.cost_usd:.2f}" if self.cost_usd else ""
        return f"turn {self.turns} · {tokens} tok{money}"


@dataclass
class ProgressRenderer:
    """Draws an authoring run as it happens.

    Attach with :meth:`install`, and pass :meth:`stage` as the pipeline's
    ``on_stage``. Both are safe to hand to a run that then fails: the live
    region is closed by ``agent_end`` and by :meth:`close`, whichever comes
    first.
    """

    #: ``False`` under ``--json``/``--quiet``, or when there is no rich. Every
    #: callback then returns immediately, so an unattached renderer costs one
    #: attribute read per event.
    enabled: bool = True
    #: Set when stderr is a terminal. Decides live region versus one line per
    #: event; the second is a log, and a log is what a redirect wants.
    live_capable: bool = False

    _console: Any = None
    _live: Any = None
    _spend: _Spend = field(default_factory=_Spend)
    _started: float = field(default_factory=time.monotonic)
    _open_calls: dict[str, float] = field(default_factory=dict)
    _stages: list[str] = field(default_factory=list)
    _label: str = "thinking"

    @classmethod
    def for_terminal(cls, *, enabled: bool = True) -> ProgressRenderer:
        """Build one bound to stderr, degrading wherever it has to."""
        if not enabled:
            return cls(enabled=False)
        try:
            from rich.console import Console
        except ImportError:
            # Without rich there is still a stream and still something worth
            # saying; only the live region is lost.
            return cls(enabled=True, live_capable=False)
        console = Console(stderr=True, highlight=False, soft_wrap=False)
        return cls(enabled=True, live_capable=console.is_terminal, _console=console)

    # -- wiring --------------------------------------------------------------

    #: The agent events this renderer draws. ``turn_end`` is absent because it
    #: says nothing a reader can act on — the turn counter has already moved
    #: and the next ``model_start`` is what shows work resuming.
    HANDLES = (
        "agent_start",
        "turn_start",
        "model_start",
        "model_end",
        "tool_start",
        "tool_end",
        "agent_end",
    )

    def install(self, hooks: Any) -> None:
        """Register this renderer's events on *hooks*.

        The registry is ordinary — nothing about it is CLI-specific — so a host
        embedding LOOM gets the same events by registering its own callbacks.
        """
        if not self.enabled:
            return
        for event in self.HANDLES:
            getattr(hooks, f"on_{event}")(getattr(self, f"_{event}"))

    def close(self) -> None:
        """Tear the live region down. Safe to call more than once."""
        live, self._live = self._live, None
        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()

    # -- agent events --------------------------------------------------------

    async def _agent_start(self, _: Any) -> None:
        self._started = time.monotonic()
        self._begin("thinking")

    async def _turn_start(self, ctx: Any) -> None:
        self._spend.turns = max(self._spend.turns, int(getattr(ctx, "turn", 0) or 0))
        self._redraw()

    async def _model_start(self, _: Any) -> None:
        self._label = "thinking"
        self._redraw()

    async def _model_end(self, ctx: Any) -> None:
        self._spend.add(getattr(ctx, "response", None))
        self._redraw()

    async def _tool_start(self, ctx: Any) -> None:
        name = str(getattr(ctx, "tool", "") or "")
        self._open_calls[name] = time.monotonic()
        self._label = self._call_text(ctx)
        self._redraw()

    async def _tool_end(self, ctx: Any) -> None:
        name = str(getattr(ctx, "tool", "") or "")
        self._open_calls.pop(name, None)
        elapsed = float(getattr(ctx, "elapsed", 0.0) or 0.0)
        outcome = str(getattr(ctx, "outcome", "") or "ok")
        style = "red" if outcome in ("error", "unknown", "rejected") else "dim"
        self._settle(
            f"  [magenta]{_DONE}[/magenta] {self._call_text(ctx)}",
            f"[{style}]{elapsed:.1f}s{'' if outcome == 'ok' else '  ' + outcome}[/{style}]",
        )
        self._label = "thinking"
        self._redraw()

    async def _agent_end(self, _: Any) -> None:
        self.close()

    # -- pipeline ------------------------------------------------------------

    async def __call__(self, check: Any, result: Any) -> None:
        """The pipeline's ``on_stage``.

        The renderer *is* the callback, rather than one of its bound methods
        being it. That is what lets the agent find :meth:`note` beside it for
        the things a stage callback cannot express — a repair round, a decline —
        without reaching through ``__self__`` to guess at an owner.
        """
        await self.stage(check, result)

    async def stage(self, check: Any, result: Any) -> None:
        """One verification stage: opening with ``None``, closing with a result.

        Stages are rendered as one accumulating line rather than one line each.
        There are sixteen of them, most finish in milliseconds, and a screen of
        ticks buries the two that did not.
        """
        if not self.enabled:
            return
        name = str(getattr(check, "name", ""))
        if result is None:
            self._label = f"checking {name}"
            self._redraw()
            return

        # A skipped stage is never a tick. A check that could not run has found
        # nothing, which is not the same as having found nothing wrong.
        if getattr(result, "skipped", False):
            mark, style = _SKIP, "dim"
        elif getattr(result, "errors", None):
            mark, style = _FAIL, "red"
        elif getattr(result, "warnings", None):
            mark, style = _WARN, "yellow"
        else:
            mark, style = _OK, "green"
        self._stages.append(f"[{style}]{mark}[/{style}] {name}")
        self._label = f"checking {name}"
        self._redraw()

    def flush_stages(self) -> None:
        """Settle the accumulated stage line into the scrollback."""
        if not self.enabled or not self._stages:
            return
        line, self._stages = "  " + "  ".join(self._stages), []
        self._settle(line, "")

    def note(self, text: str, *, style: str = "yellow") -> None:
        """One narrated event that is not a tool call — a repair round, a retry."""
        if not self.enabled:
            return
        self._settle(f"  [{style}]↻[/{style}] {text}", "")

    # -- drawing -------------------------------------------------------------

    def _call_text(self, ctx: Any) -> str:
        name = str(getattr(ctx, "tool", "") or "")
        arguments = brief_arguments(getattr(ctx, "arguments", None))
        return f"{name}({arguments})" if arguments else f"{name}()"

    def _begin(self, label: str) -> None:
        self._label = label
        if not (self.enabled and self.live_capable and self._console is not None):
            return
        if self._live is not None:
            return
        try:
            from rich.live import Live

            self._live = Live(
                self._status(),
                console=self._console,
                refresh_per_second=8,
                transient=True,
            )
            self._live.start()
        except Exception:
            self._live = None

    def _status(self) -> Any:
        from rich.text import Text

        line = Text("  ")
        line.append(f"{_RUNNING} ", style="yellow")
        line.append(self._label[:72])
        line.append(f"   {self._spend.describe()} · {self._elapsed()}", style="dim")
        return line

    def _elapsed(self) -> str:
        seconds = time.monotonic() - self._started
        return f"{seconds:.0f}s" if seconds < 60 else f"{seconds // 60:.0f}m{seconds % 60:02.0f}s"

    def _redraw(self) -> None:
        if self._live is None:
            self._begin(self._label)
        if self._live is None:
            return
        try:
            self._live.update(self._status())
        except Exception:
            self.close()

    def _settle(self, markup: str, suffix: str) -> None:
        """Move a finished thing into the scrollback, above the live region."""
        if not self.enabled:
            return
        text = f"{markup}  {suffix}" if suffix else markup
        if self._console is None:
            import sys

            from loom.cli.output import _strip_markup

            print(_strip_markup(text), file=sys.stderr)
            return
        try:
            target = self._live.console if self._live is not None else self._console
            target.print(text)
        except Exception:
            pass
