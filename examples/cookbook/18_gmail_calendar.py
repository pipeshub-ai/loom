"""Example 18 — Gmail and Calendar, written by the coding agent.

Nobody writes the workflow here. A plain-English spec goes in, the agent
discovers the Gmail and Calendar toolsets from their manifests, writes the
workflow, validates it, runs it once against fakes, and then it is executed for
real against your account.

The point of the example is the discovery path. The agent is given a registry
and nothing else — no hand-written tool documentation — so the manifests have to
carry enough for a model to write working code: what the operations are, and the
import that makes them callable. If a manifest omits that, the agent invents an
import and the workflow fails on its first line.

Read-only by default. The specs that send mail or create events only run with
``--write``, and generation happens either way so you can read the code first.

Requires (add to .env):
    OPENAI_API_KEY or ANTHROPIC_API_KEY   whichever you have
    GOOGLE_ACCESS_TOKEN
      or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN

Get Google credentials with:
    python -m workflow_builder.toolsets.google.setup --scopes read

Run:
    python3 examples/cookbook/18_gmail_calendar.py            # read-only
    python3 examples/cookbook/18_gmail_calendar.py --write    # includes a send
    python3 examples/cookbook/18_gmail_calendar.py --dry-run  # generate, don't execute
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import box, header, log, print_coding_result, require_any_env

from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.tool_registry import ToolsetRegistry
from workflow_builder.toolsets.google import GMAIL_MANIFEST, GOOGLE_CALENDAR_MANIFEST

WRITE = "--write" in sys.argv
DRY_RUN = "--dry-run" in sys.argv


class Spec(NamedTuple):
    """One demo spec, and whether running it changes anything in your account.

    Marked by hand rather than inferred. Every spec starts "Create a workflow",
    so any keyword heuristic reads them all as writes — and guessing wrong about
    someone's real mailbox is not a mistake worth risking.
    """

    writes: bool
    name: str
    spec: str
    input: str


SPECS = [
    Spec(
        writes=False,
        name="unread_digest",
        input="is:unread newer_than:2d",
        spec="""\
Create a workflow called "unread_digest" that takes a Gmail search query as
input. It should search the mailbox with that query, returning at most 15
messages, and produce a dict with the number found and a list of one-line
summaries, each holding the sender and the subject.

Do not send anything. This workflow only reads.""",
    ),
    Spec(
        writes=False,
        name="day_ahead",
        input="primary",
        spec="""\
Create a workflow called "day_ahead" that takes a calendar id as input.

It should list the events on that calendar for the next 24 hours and return a
dict with the count and, for each event, its title, start time, and location.

Derive the time window from ctx.now() — the workflow body must stay
deterministic, so it must not read the wall clock directly. Do not create,
change, or delete anything.""",
    ),
    Spec(
        writes=True,
        name="mail_yourself_the_agenda",
        input="primary",
        spec="""\
Create a workflow called "mail_yourself_the_agenda" that takes a calendar id as
input.

It should list the next 24 hours of events on that calendar, format them as a
short plain-text agenda, look up the authenticated Gmail profile to find the
user's own email address, and send that agenda to that address.

Derive the time window from ctx.now(). Return the sent message id and the number
of events included.""",
    ),
]


async def generate_and_run(
    build_agent: Callable[[Any], WorkflowCodingAgent],
    spec: Spec,
    index: int,
    total: int,
) -> None:
    """Generate one workflow from its spec, then execute it."""
    # The smoke run gets the input this workflow will really receive. With the
    # default of None a workflow taking a calendar id is exercised with None,
    # and the failure that reports is about the fake input, not the code.
    agent = build_agent(spec.input)
    header(f"[{index}/{total}] {spec.name}")
    box(spec.spec, "spec")

    log("coding-agent", "generating…")
    try:
        result = await agent.generate(spec.spec)
    except Exception as exc:
        # One spec running out of turns, or a provider hiccup, should not end
        # the demo — the remaining specs are still worth seeing.
        log("coding-agent", f"generation failed: {type(exc).__name__}: {exc}")
        return
    print_coding_result(result)

    box(result.code, "generated workflow")

    if not result.code.strip():
        log("coding-agent", "no code was produced; skipping execution")
        return

    # A warning is not a blocker: the smoke sandbox holds no Google credentials,
    # so a workflow that really talks to Gmail cannot be verified there. Errors
    # are different — those mean the code itself is wrong.
    blocking = [issue for issue in result.issues if issue.severity == "error"]
    if blocking:
        log("validate", f"{len(blocking)} blocking issue(s); not executing")
        return

    if DRY_RUN:
        log("run", "--dry-run, so the generated workflow is not executed")
        return

    await execute(result.code, spec)


async def execute(code: str, spec: Spec) -> None:
    """Run the generated workflow against the real account."""
    from utils import load_workflow

    from workflow_builder import Runtime
    from workflow_builder.state.memory import MemoryStore

    try:
        definition = load_workflow(code)
    except Exception as exc:
        # The agent's code, not ours: report it rather than raising, so one bad
        # generation does not end the demo.
        log("run", f"generated module did not import: {type(exc).__name__}: {exc}")
        return

    if definition is None:
        log("run", "the generated file declares no @workflow")
        return

    runtime = Runtime(store=MemoryStore())
    runtime.register(definition)

    log("run", f"executing {definition.name} with input {spec.input!r}")
    result = await runtime.run(definition.name, spec.input)

    if result.status.value == "failed":
        log("run", f"failed: {str(result.error.message if result.error else '')[:300]}")
        return
    log("run", f"{result.status.value}")
    box(str(result.output)[:1200], "output")


def pick_model() -> object:
    """Return whichever provider has a usable key, preferring Anthropic."""
    from workflow_builder.agents.providers import AnthropicProvider, OpenAIProvider

    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    return OpenAIProvider()


async def main() -> int:
    header("Gmail + Calendar, written by the coding agent")

    require_any_env(("ANTHROPIC_API_KEY",), ("OPENAI_API_KEY",))
    require_any_env(
        ("GOOGLE_ACCESS_TOKEN",),
        ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"),
    )

    # The registry is the whole toolset input. No hand-written tool docs are
    # passed, so the manifests have to be good enough on their own — which is
    # what this example is really testing.
    registry = ToolsetRegistry()
    registry.register(GMAIL_MANIFEST)
    registry.register(GOOGLE_CALENDAR_MANIFEST)

    model = pick_model()

    def build_agent(smoke_input: Any) -> WorkflowCodingAgent:
        return WorkflowCodingAgent(
            model,
            tool_registry=registry,
            # The turn budget is max_repair_attempts + 6, and discovery spends
            # several before any code is written — two leaves nothing for a repair.
            max_repair_attempts=4,
            smoke_test=True,
            smoke_input=smoke_input,
        )

    specs = SPECS if WRITE else [s for s in SPECS if not s.writes]
    mode = "read + write" if WRITE else "read-only"
    log("mode", mode + (" (generate only)" if DRY_RUN else ""))
    if not WRITE:
        skipped = len(SPECS) - len(specs)
        log("mode", f"{skipped} spec that writes to your account was skipped")

    for index, spec in enumerate(specs, 1):
        await generate_and_run(build_agent, spec, index, len(specs))

    header("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
