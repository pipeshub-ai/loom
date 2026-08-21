"""Asking a question at the session prompt.

The fourth :class:`~loom.agents.interaction.UserInteraction`, beside the
stdin/stderr one, the MCP elicitation one, and the recorded one. **Only the
renderer is new** — the protocol, the three outcomes, the budget gate and the
recording are untouched, because those are the parts that were designed
carefully and none of them is about a terminal.

What changes is that a choice is made with arrow keys instead of by typing a
number. That sounds cosmetic and is not: the recommended option is *preselected*,
so Enter is a one-keystroke `decline` that takes the recommendation, which is the
outcome the three-outcome protocol exists to make usable. Typing a number makes
declining and accepting cost the same, and a person under mild pressure picks
whichever is on top.

Everything the terminal cannot do falls back to
:class:`~loom.agents.interaction.CLIUserInteraction`, which is where the
non-TTY, no-``prompt_toolkit`` and timeout behaviour already lives. This class
adds a rendering, never a second set of rules.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loom.agents.interaction import Answer, CLIUserInteraction, Question

__all__ = ["PromptUserInteraction"]


class PromptUserInteraction:
    """Asks with a ``prompt_toolkit`` selection list, falling back to text."""

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout
        #: The one that already knows every rule. Used verbatim wherever the
        #: fancier rendering cannot apply, so the two can never disagree about
        #: what an empty answer means.
        self._plain = CLIUserInteraction(timeout=timeout)

    def available(self) -> bool:
        return self._plain.available() and _has_prompt_toolkit()

    async def ask(self, questions: list[Question]) -> list[Answer]:
        if not self.available():
            return await self._plain.ask(questions)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._ask_sync, questions), self._timeout
            )
        except TimeoutError:
            # Cancel, never decline: nobody chose anything. The two lead to
            # different fallbacks, and conflating them is what made a
            # five-minute silence look like a considered "proceed".
            return [Answer(action="cancel") for _ in questions]

    # -- rendering -----------------------------------------------------------

    def _ask_sync(self, questions: list[Question]) -> list[Answer]:
        answers: list[Answer] = []
        for index, question in enumerate(questions, 1):
            try:
                answers.append(self._one(question, index, len(questions)))
            except (EOFError, KeyboardInterrupt):
                # Everything after is cancelled rather than left unasked: a
                # partial list would silently misalign answers with questions.
                answers.append(Answer(action="cancel"))
                answers += [Answer(action="cancel")] * (len(questions) - index)
                break
        return answers

    def _one(self, question: Question, index: int, total: int) -> Answer:
        self._headline(question, index, total)
        if question.kind == "confirm" or not question.choices:
            # A free-text or yes/no answer is a line of input, which is what
            # the plain implementation already reads correctly.
            return self._plain._one(question)
        return self._choose(question)

    def _headline(self, question: Question, index: int, total: int) -> None:
        from prompt_toolkit import HTML, print_formatted_text
        from prompt_toolkit.output import create_output

        counter = f"[{index}/{total}] " if total > 1 else ""
        chip = f"({question.header}) " if question.header else ""
        # stderr, because under `loom mcp --transport stdio` stdout is the
        # protocol channel — and because progress is written there too, so a
        # question and the work around it interleave on one stream.
        output = create_output(stdout=_stderr())
        print_formatted_text(
            HTML(f"\n  <b>{_escape(counter + chip + question.question)}</b>"),
            output=output,
        )
        if question.context:
            print_formatted_text(
                HTML(f"  <ansigray>{_escape(question.context)}</ansigray>"),
                output=output,
            )

    def _choose(self, question: Question) -> Answer:
        from prompt_toolkit.shortcuts import radiolist_dialog

        shown = question.ordered()
        values = [
            (choice.value, choice.shown() + (" (recommended)" if choice.recommended else ""))
            for choice in shown
        ]
        if question.allow_other:
            values.append((_OTHER, "Other — type your own answer"))

        recommended = question.recommended()
        picked = radiolist_dialog(
            title="loom",
            text=question.question,
            values=values,
            default=recommended.value if recommended is not None else values[0][0],
        ).run()

        if picked is None:
            # Escape, or the dialog dismissed. Nobody chose, so this is a
            # cancel rather than a decline.
            return Answer(action="cancel")
        if picked == _OTHER:
            typed = self._plain._read("    Your answer > ").strip()
            return (
                Answer(action="accept", other=typed)
                if typed
                else self._plain._declined(question)
            )
        return Answer(action="accept", values=[picked])


#: Sentinel for the off-list option. A string because the dialog's values are
#: strings, and one nobody would type.
_OTHER = "\x00loom.other"


def _has_prompt_toolkit() -> bool:
    import importlib.util

    return importlib.util.find_spec("prompt_toolkit") is not None


def _stderr() -> Any:
    import sys

    return sys.stderr


def _escape(text: str) -> str:
    """Angle brackets are markup to ``HTML``, and a question is not ours."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
