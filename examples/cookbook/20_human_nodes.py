"""Example 20 — Human-in-the-loop with typed nodes.

Example 5 does this with ``ctx.wait_for_event`` directly, which works and has
two gaps: the answer is untyped, and **nobody is told** the run is waiting. A
parked run exists only as a journal entry someone has to go looking for, which
in practice means it is found a day late.

``human.*`` nodes close both. The answer comes back as a model, and a
``HumanChannel`` delivers the request — LOOM parks the run and validates the
reply, the provider does the asking.

Demonstrates: ctx.node(), human.approval, human.choice, human.review_edit,
HumanChannel, and `loom pending` / `loom respond`.

Run:
    python3 examples/cookbook/20_human_nodes.py
"""

from __future__ import annotations

import asyncio

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.facade import LocalFacade
from workflow_builder.nodes.human import (
    ApprovalIn,
    ChoiceIn,
    DeliveryReceipt,
    HumanRequest,
    ReviewIn,
)
from workflow_builder.state.memory import MemoryStore


@step
async def draft_reply(complaint: str) -> str:
    """Write a first draft. A model would do this for real."""
    return f"Hi — sorry about this. We are looking into: {complaint[:40]}"


# ---------------------------------------------------------------------------
# The provider half: where a request reaches a person.
# ---------------------------------------------------------------------------


class DemoChannel:
    """A channel that prints requests and remembers them.

    A real one posts a Slack block, sends mail, or opens a ticket. It needs no
    LOOM internals: ``HumanRequest`` carries the JSON Schema of the answer, so a
    provider renders a form from the request rather than hard-coding one shape
    per node.
    """

    name = "demo"

    def __init__(self) -> None:
        self.raised: list[HumanRequest] = []

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        self.raised.append(request)
        who = ", ".join(request.assignees) or "anyone"
        print(f"  → asking {who}: {request.prompt}")
        for field in sorted(request.response_schema.get("properties", {})):
            print(f"      expects: {field}")
        # Journaled, so this runs exactly once per request across replays. A
        # restart must not re-ping the same person.
        return DeliveryReceipt(
            channel=self.name, delivered=True, reference=request.request_id
        )

    async def withdraw(self, request_id: str, reason: str) -> None:
        print(f"  → withdrawing {request_id}: {reason}")


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


@workflow(name="handle_complaint")
async def handle_complaint(ctx: Context, complaint: str) -> str:
    """Route a complaint, draft a reply, and get both checked by a person."""
    team = await ctx.node(
        "human.choice",
        ChoiceIn(
            subject="route",
            prompt="Which team should own this?",
            options=["billing", "platform", "support"],
            assignees=["triage@acme.com"],
        ),
    )

    draft = await ctx.step(draft_reply, complaint)
    reviewed = await ctx.node(
        "human.review_edit",
        ReviewIn(
            subject="reply",
            draft=draft,
            prompt="Check tone and facts before this goes out.",
            assignees=[f"{team.selected[0]}@acme.com"],
        ),
    )

    decision = await ctx.node(
        "human.approval",
        ApprovalIn(
            subject="send",
            prompt="Send this reply?",
            context={"team": team.selected[0], "edited": reviewed.edited},
            assignees=["lead@acme.com"],
            on_timeout="reject",
        ),
    )
    if not decision.approved:
        return f"held by {decision.responder or 'the reviewer'}"
    return f"[{team.selected[0]}] {reviewed.content}"


async def main() -> None:
    channel = DemoChannel()
    runtime = Runtime(store=MemoryStore(), human=channel)
    runtime.register(handle_complaint)
    facade = LocalFacade(runtime)

    print("Starting the run — it will park on the first question.\n")
    result = await runtime.run(handle_complaint, "double-charged on invoice 4821")
    print(f"\nstatus: {result.status.value}")

    # `loom pending` is this, from the command line. It is what turns a parked
    # run into a queue item rather than something you have to already know about.
    for waiting in await facade.pending():
        print(f"waiting: {waiting['subject']} — {waiting['prompt']}")
        print(f"         {waiting['next_action']}")

    print("\nAnswering each question in turn.\n")

    result = await facade.respond(
        result.run_id, "route", {"selected": ["billing"], "responder": "dana"}
    )
    result = await facade.respond(
        result["run_id"],
        "reply",
        {"content": "Hi — sorry about this, a refund is on the way.", "responder": "sam"},
    )
    run = await facade.respond(
        result["run_id"], "send", {"approved": True, "responder": "lead"}
    )

    print(f"\nstatus: {run['status']}")
    print(f"output: {run['output']}")
    print(f"\nrequests raised: {len(channel.raised)} (one per question, never repeated)")
    print(f"still waiting:   {len(await facade.pending())}")


if __name__ == "__main__":
    asyncio.run(main())
