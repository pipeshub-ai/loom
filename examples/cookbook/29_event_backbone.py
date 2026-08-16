"""Example 29 — Events from the outside world.

The third trigger shape, after cron (13) and a queue (15), and the one with the
most moving parts: a provider pushes, LOOM records it, and **many** workflows
consume it independently.

The design is a log, not a bus. A bus makes *delivery* durable, so a subscriber
that was down missed those events permanently. A log makes *the record* durable
and delivery a resumable read — which is the only shape where several workflows
each consume the same event and every one of them survives being killed.

Five things happen below, and each is a property you would otherwise have to
take on trust:

1. a signed Slack delivery is verified and appended;
2. two workflows read the same topic through different filters;
3. the process "restarts" — new Runtime, new dispatcher, same file — and
   replays nothing;
4. an event nobody can dispatch is dead-lettered and stepped over, while a
   workflow that *raises* stays an ordinary failed run;
5. an operator reads the lag, which is what `loom events status` prints.

Everything here is offline. `StoreBackedEventLog` rides the store you already
have — memory, SQLite, Postgres, Mongo — so this needs no broker and no
credentials. A host that outgrows it supplies an `EventLog` adapter and proves
it with `loom.testing.conformance.verify_event_log`.

Run:
    python3 examples/cookbook/29_event_backbone.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from loom import Context, Runtime, workflow
from loom.events import (
    EventDispatcher,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
    WebhookIngress,
)
from loom.events.manager import SubscriptionManager
from loom.toolsets.slack.source import SlackSource
from loom.triggers.filter import FilterSpec
from loom.triggers.specs import OnAppEvent

#: Slack signs deliveries with this. In production it comes from the app's
#: Basic Information page as $SLACK_SIGNING_SECRET — it is *not* the bot token,
#: and using the token instead fails every signature with no hint why.
SIGNING_SECRET = "example-signing-secret"

#: One topic per {source}.{event_type}. `app.slack.message` rather than
#: `app.slack` means a workflow interested only in messages never reads a
#: single reaction.
TOPIC = "app.slack.message"


# ---------------------------------------------------------------------------
# Two workflows, one topic
# ---------------------------------------------------------------------------


@workflow(
    name="tech_triage",
    triggers=[
        # The filter is versioned with the code, and costs one log read per
        # rejected event. Provider-side filtering is cheaper still and belongs
        # in the provider's own config; a topic split is the middle ground.
        OnAppEvent(TOPIC, where=FilterSpec(conditions={"channel": "C_TECH"})),
    ],
)
async def tech_triage(ctx: Context, message: dict) -> str:
    """Only #tech. Everything else is filtered before a run is created."""
    return f"triaged: {message['text']}"


@workflow(name="archive_everything", triggers=[OnAppEvent(TOPIC)])
async def archive_everything(ctx: Context, message: dict) -> str:
    """Every channel — and its own cursor, so it can never hold triage back."""
    if message["text"] == "boom":
        raise ValueError("this message breaks the archiver")
    return f"archived: {message['text']}"


# ---------------------------------------------------------------------------
# Standing in for a provider
# ---------------------------------------------------------------------------


def slack_delivery(event_id: str, *, channel: str, text: str) -> tuple[dict, bytes]:
    """The bytes Slack would POST, and the headers it would sign them with.

    The signature covers the **raw body**: `v0:{timestamp}:{body}`, HMAC-SHA256.
    Re-serialising the parsed JSON produces different bytes — different key
    order, different spacing — and the check then fails for every legitimate
    delivery, which reads as a wrong secret.
    """
    body = json.dumps({
        "type": "event_callback",
        "event_id": event_id,
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": channel,
            "user": "U1",
            "text": text,
            "event_ts": "1701234567.000200",
        },
    }).encode()

    stamp = str(int(time.time()))
    digest = hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + stamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": f"v0={digest}",
    }, body


def deployment(path: Path) -> tuple[Runtime, EventDispatcher, WebhookIngress]:
    """Everything a host wires up, over one store.

    Composed at the edge, exactly as `store`, `human`, `blobs` and `sandbox`
    already are — `loom.events` imports protocols and nothing else.
    """
    from loom.stores.sqlite import SQLiteStore

    store = SQLiteStore(str(path))
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    runtime.sources.register(SlackSource(SIGNING_SECRET))

    dispatcher = EventDispatcher(
        runtime, log=runtime.events, checkpoints=StoreBackedCheckpoints(store)
    )
    return runtime, dispatcher, WebhookIngress(runtime)


async def settle(runtime: Runtime, reports: list) -> None:
    """Wait for the runs a pass started, and print what each returned."""
    for report in reports:
        for run_id in report.started:
            result = await runtime.wait(run_id, timeout=5)
            if result.status.value == "completed":
                log(report.subscriber, f"completed: {result.output}")
            else:
                detail = result.error.message if result.error else ""
                log(report.subscriber, f"{result.status.value}: {detail}")


# ---------------------------------------------------------------------------


async def main() -> None:
    # Two failures below are deliberate — an event nobody can dispatch, and a
    # workflow that raises — and both log at ERROR. Quietened so the narrative
    # reads as a narrative; a real deployment wants them at full volume, which
    # is why they are logged rather than swallowed.
    logging.getLogger("workflow.events").setLevel(logging.CRITICAL)
    logging.getLogger("workflow.engine").setLevel(logging.CRITICAL)

    workspace = Path(tempfile.mkdtemp(prefix="loom-events-"))
    path = workspace / "runs.db"

    try:
        header("1. A signed delivery is verified and recorded")
        runtime, dispatcher, ingress = deployment(path)
        await dispatcher.register(tech_triage)
        await dispatcher.register(archive_everything)

        headers, body = slack_delivery("Ev1", channel="C_TECH", text="deploy done")
        result = await ingress.receive("slack", headers, body)
        log("ingress", f"accepted -> {result.topics[0]} as {result.event_ids[0]}")

        # A forged delivery never reaches the log at all.
        try:
            await ingress.receive("slack", {}, body)
        except Exception as exc:
            log("ingress", f"unsigned delivery refused: {type(exc).__name__}")

        header("2. Two workflows, one event, different filters")
        headers, body = slack_delivery("Ev2", channel="C_RANDOM", text="lunch?")
        await ingress.receive("slack", headers, body)

        reports = await dispatcher.poll_once()
        for report in reports:
            log(
                "dispatch",
                f"{report.subscriber}: read={report.read} matched={report.matched} "
                f"filtered={report.filtered} @{report.committed_through}",
            )
        await settle(runtime, reports)
        log("note", "triage saw one message; archive saw both. Separate cursors.")

        header("3. The process restarts — and replays nothing")
        await runtime.shutdown(drain=1.0)

        # New Runtime, new dispatcher, same file. Anything held in memory that
        # should have been durable disappears here and nowhere else.
        runtime, dispatcher, ingress = deployment(path)
        await dispatcher.register(tech_triage)
        await dispatcher.register(archive_everything)

        reports = await dispatcher.poll_once()
        log("dispatch", f"after restart: read={sum(r.read for r in reports)}")
        log("note", "zero. Every checkpoint was committed after its dispatch.")

        headers, body = slack_delivery("Ev3", channel="C_TECH", text="still flowing")
        await ingress.receive("slack", headers, body)
        await settle(runtime, await dispatcher.poll_once())

        header("4. A dead letter is about delivery, not execution")
        # An event nobody can dispatch: the subscription names a workflow this
        # process does not have. Retrying it forever would stall the subscriber
        # behind an event that will fail identically every time.
        await dispatcher.subscribe(Subscription("orphan", TOPIC, "deleted_workflow"))
        headers, body = slack_delivery("Ev4", channel="C_TECH", text="for a ghost")
        await ingress.receive("slack", headers, body)
        await settle(runtime, await dispatcher.poll_once())

        for event in await runtime.events.read(f"{TOPIC}.dead", after=None, limit=5):
            log("dead-letter", f"{event.payload['subscriber']}: {event.payload['error']}")

        # A workflow that *raises* is the other case, and stays a failed run —
        # with a journal, and `retry`/`replay` semantics a dead letter would
        # throw away. `loom runs --status failed` is where it lives.
        headers, body = slack_delivery("Ev5", channel="C_TECH", text="boom")
        await ingress.receive("slack", headers, body)
        await settle(runtime, await dispatcher.poll_once())
        log("note", "the archiver failed as a *run*; nothing was dead-lettered")

        header("5. What an operator sees")
        manager = SubscriptionManager(
            runtime.store,
            log=runtime.events,
            checkpoints=StoreBackedCheckpoints(runtime.store),
        )
        for row in await manager.health():
            state = "quarantined" if row.quarantined else ("new" if not row.started else "ok")
            log(
                "status",
                f"{row.subscriber:<18} pos={row.position or '-':<4} "
                f"lag={row.lag} {state}",
            )

        log("note", "the same rows `loom events subscriptions` prints")
        log("note", "`loom events status` exits 1 when any of them is unhealthy")

        await runtime.shutdown(drain=1.0)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
