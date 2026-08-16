"""One deployment, two providers, a restart, and an operator.

Every other event test isolates a component. This one does not, deliberately:
it is the backbone's acid test, in the shape ``test_host_integration.py`` takes
for the host story. A signed Slack delivery and a Gmail push notification enter
over HTTP, fan out to workflows with different filters, one of them poisons and
dead-letters, the process **dies and comes back**, and an operator finds all of
it through `loom events`.

Two things make it worth its runtime.

**It runs on SQLite, not memory.** Everything durable here round-trips through a
real store — records, checkpoints, the subscription registry, the source cursor
— and that is where a value that serialises fine in memory and comes back naive,
or as a string, or not at all, shows up. `_aware()` exists in the manager
because of exactly that.

**The restart is a new process's worth of objects.** New Runtime, new dispatcher,
new reconciler, new manager, sharing only the file. Anything held in memory that
should have been durable disappears here and nowhere else.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest

from loom import Context, ExecutionStatus, Runtime, workflow
from loom.events import (
    EventDispatcher,
    PointerReconciler,
    SourceState,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
)
from loom.events.manager import SubscriptionManager
from loom.stores.sqlite import SQLiteStore
from loom.toolsets.google.gmail.source import GmailReconciler, GmailSource
from loom.toolsets.slack.source import SlackSource
from loom.triggers.filter import FilterSpec
from loom.triggers.specs import OnAppEvent

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from loom.server.app import create_app  # noqa: E402

SECRET = "e2e-signing-secret"
SLACK_TOPIC = "app.slack.message"
GMAIL_TOPIC = "app.gmail.message"

#: What each workflow saw, across restarts. A module-level list because the
#: point is what survived, not which object observed it.
SEEN: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def _clear() -> None:
    SEEN.clear()


# ---------------------------------------------------------------------------
# The deployment's workflows
# ---------------------------------------------------------------------------


@workflow(name="tech_triage", triggers=[
    OnAppEvent(SLACK_TOPIC, where=FilterSpec(conditions={"channel": "C_TECH"})),
])
async def tech_triage(ctx: Context, message: dict) -> str:
    SEEN.append(("triage", message["text"]))
    return "triaged"


@workflow(name="archive_all", triggers=[OnAppEvent(SLACK_TOPIC)])
async def archive_all(ctx: Context, message: dict) -> str:
    if message.get("text") == "poison":
        raise ValueError("this one can never be handled")
    SEEN.append(("archive", message["text"]))
    return "archived"


@workflow(name="mail_triage", triggers=[OnAppEvent(GMAIL_TOPIC)])
async def mail_triage(ctx: Context, email: dict) -> str:
    SEEN.append(("mail", email["subject"]))
    return "read"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def slack_body(event_id: str, *, channel: str = "C_TECH", text: str = "hi") -> bytes:
    return json.dumps({
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


def slack_headers(body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    digest = hmac.new(
        SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


def gmail_body(history_id: str) -> bytes:
    inner = json.dumps({"emailAddress": "team@x.com", "historyId": int(history_id)})
    return json.dumps({
        "message": {
            "data": base64.b64encode(inner.encode()).decode(),
            "messageId": f"psm-{history_id}",
        },
        "subscription": "projects/p/subscriptions/s",
    }).encode()


class FakeGmail:
    """Just enough of GmailClient for a reconciler, shared across restarts."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.latest = "1000"

    def deliver(self, message_id: str) -> None:
        self.records.append({"messagesAdded": [{"message": {"id": message_id}}]})
        self.latest = str(int(self.latest) + 1)

    async def list_history(self, start_history_id: str, **kw: Any) -> Any:
        from loom.toolsets.google.gmail.models import GmailHistory

        return GmailHistory(
            start_history_id=start_history_id,
            history_id=self.latest,
            records=self.records,
        )

    async def get_message(self, message_id: str) -> Any:
        class Message:
            def model_dump(self) -> dict[str, Any]:
                return {"id": message_id, "subject": f"about {message_id}"}

        return Message()


# ---------------------------------------------------------------------------
# One deployment, assembled the way a host assembles it
# ---------------------------------------------------------------------------


class Deployment:
    """Everything a host wires up, over one store."""

    def __init__(self, path: Path, gmail: FakeGmail) -> None:
        self.store = SQLiteStore(str(path))
        self.runtime = Runtime(
            store=self.store, events=StoreBackedEventLog(self.store)
        )
        self.runtime.sources.register(SlackSource(SECRET))
        self.runtime.sources.register(GmailSource(require_token=False))

        self.marks = StoreBackedCheckpoints(self.store)
        self.dispatcher = EventDispatcher(
            self.runtime, log=self.runtime.events, checkpoints=self.marks
        )
        self.reconciler = PointerReconciler(
            GmailReconciler(gmail),
            log=self.runtime.events,
            checkpoints=self.marks,
            state=SourceState(self.store, "gmail"),
        )
        self.manager = SubscriptionManager(
            self.store, log=self.runtime.events, checkpoints=self.marks
        )
        self.client = TestClient(create_app(self.runtime))

    async def register(self) -> None:
        for flow in (tech_triage, archive_all, mail_triage):
            await self.dispatcher.register(flow)
            for spec in flow.triggers:
                await self.manager.add(spec.subscription_for(flow.name))

    async def drive(self, rounds: int = 4) -> None:
        """Reconcile, dispatch, and let the runs actually execute."""
        for _ in range(rounds):
            await self.reconciler.drain()
            await self.dispatcher.poll_once()
            for _ in range(60):
                await asyncio.sleep(0)
            await asyncio.sleep(0.02)

    async def shutdown(self) -> None:
        await self.runtime.shutdown(drain=0.5)


@pytest.fixture
def gmail() -> FakeGmail:
    return FakeGmail()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


# ---------------------------------------------------------------------------
# The narrative
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_the_whole_path_survives_a_restart(self, path, gmail) -> None:
        """The one test. Read the assertions as the story."""
        first = Deployment(path, gmail)
        await first.register()

        # --- 1. Two providers deliver over HTTP -------------------------------
        body = slack_body("Ev1", text="deploy finished")
        assert first.client.post(
            "/hooks/slack", content=body, headers=slack_headers(body)
        ).status_code == 202

        other = slack_body("Ev2", channel="C_RANDOM", text="lunch?")
        first.client.post("/hooks/slack", content=other, headers=slack_headers(other))

        assert first.client.post(
            "/hooks/gmail", content=gmail_body("1000")
        ).status_code == 202

        # --- 2. Fan-out with per-subscriber filters ---------------------------
        await first.drive()

        assert sorted(SEEN) == [
            ("archive", "deploy finished"),
            ("archive", "lunch?"),
            ("triage", "deploy finished"),
        ], "each subscriber applies its own filter, not a shared one"

        # The Gmail pointer was adopted, not back-filled: a watch established
        # now says nothing about what came before it.
        assert not any(kind == "mail" for kind, _ in SEEN)

        # --- 3. A workflow that raises is a *failed run*, not a dead letter ---
        #
        # The asymmetry matters and is easy to get backwards. The dead-letter
        # is about *delivery*: an event nobody could dispatch. A workflow that
        # was dispatched and then raised is a recorded failure with a journal,
        # and `retry`/`replay` semantics a dead-letter would throw away.
        poison = slack_body("Ev3", text="poison")
        first.client.post("/hooks/slack", content=poison, headers=slack_headers(poison))
        after = slack_body("Ev4", text="still flowing")
        first.client.post("/hooks/slack", content=after, headers=slack_headers(after))
        await first.drive()

        assert ("archive", "still flowing") in SEEN, (
            "one failing run must cost one run, not the stream"
        )
        assert ("triage", "poison") in SEEN, (
            "and it must only affect the subscriber whose workflow raised"
        )

        failed = await first.runtime.list_runs(status=ExecutionStatus.FAILED)
        assert len(failed) == 1, "the failure is a run, findable by `loom runs`"
        assert failed[0].workflow == "archive_all"

        assert await first.runtime.events.head(f"{SLACK_TOPIC}.dead") is None, (
            "nothing was dead-lettered: the event was delivered fine"
        )

        # --- 4. An event nobody can dispatch *is* dead-lettered ---------------
        await first.dispatcher.subscribe(
            Subscription("orphan", SLACK_TOPIC, "no-such-workflow")
        )
        orphaned = slack_body("Ev5", text="for a workflow that is gone")
        first.client.post(
            "/hooks/slack", content=orphaned, headers=slack_headers(orphaned)
        )
        await first.drive()

        dead = await first.runtime.events.read(
            f"{SLACK_TOPIC}.dead", after=None, limit=10
        )
        assert len(dead) == 1
        assert dead[0].payload["subscriber"] == "orphan"
        orphan_health = {
            row.subscriber: row for row in await first.manager.health()
        }["orphan"]
        assert orphan_health.lag == 0, (
            "and it stepped over rather than stalling behind an event that "
            "would fail identically forever"
        )

        # --- 5. The process dies ----------------------------------------------
        before_restart = sorted(SEEN)
        await first.shutdown()

        # --- 6. And comes back: new objects, same file -------------------------
        second = Deployment(path, gmail)
        await second.register()
        await second.drive()

        assert sorted(SEEN) == before_restart, (
            "a restart must replay nothing: every checkpoint was durable"
        )

        # --- 7. New work after the restart still flows -------------------------
        fresh = slack_body("Ev6", text="after the restart")
        second.client.post("/hooks/slack", content=fresh, headers=slack_headers(fresh))
        gmail.deliver("m1")
        second.client.post("/hooks/gmail", content=gmail_body("1001"))
        await second.drive()

        assert ("archive", "after the restart") in SEEN
        assert ("mail", "about m1") in SEEN, (
            "the pointer shape must reconcile across a restart too — its "
            "provider cursor is durable, not held in the reconciler"
        )

        # --- 8. An operator finds all of it ------------------------------------
        health = {row.subscriber: row for row in await second.manager.health()}
        assert set(health) >= {"tech_triage", "archive_all", "mail_triage"}
        assert all(
            health[name].lag == 0
            for name in ("tech_triage", "archive_all", "mail_triage")
        ), f"the deployed workflows should be current: { {k: v.lag for k, v in health.items()} }"

        # `orphan` was subscribed by the first process and not by the second,
        # so its checkpoint outlived its subscription and it is now falling
        # behind. That is not a bug being tolerated — it is the "removed from
        # the code but not from the deployment" case, and the whole reason
        # health reads the checkpoints as well as the registry. A view built
        # from the registry alone would show a tidy, complete, wrong picture.
        assert health["orphan"].lag == 1
        assert "orphan" not in {s.subscriber for s in await second.manager.subscriptions()}

        dead = await second.runtime.events.read(
            f"{SLACK_TOPIC}.dead", after=None, limit=10
        )
        assert len(dead) == 1, "the dead letter survived the restart too"
        assert dead[0].payload["workflow"] == "no-such-workflow"

        # --- 9. And a replay repeats no work -----------------------------------
        plan = await second.manager.replay("archive_all", SLACK_TOPIC, max_events=100)
        assert plan.events >= 4
        counted = sorted(SEEN)
        await second.drive()

        assert sorted(SEEN) == counted, (
            "re-reading must resolve to the original runs: the dispatch key is "
            "{event_id}#{subscriber}, so nothing already handled runs twice"
        )

        await second.shutdown()

    async def test_a_redelivery_across_a_restart_is_still_one_event(
        self, path, gmail
    ) -> None:
        """Providers redeliver on any non-2xx, and a restart is the most likely
        cause of one. The dedupe has to be in the store, not in the process."""
        first = Deployment(path, gmail)
        await first.register()
        body = slack_body("Ev1", text="once")
        first.client.post("/hooks/slack", content=body, headers=slack_headers(body))
        await first.drive()
        await first.shutdown()

        second = Deployment(path, gmail)
        await second.register()
        second.client.post("/hooks/slack", content=body, headers=slack_headers(body))
        await second.drive()

        assert [entry for entry in SEEN if entry[0] == "archive"] == [
            ("archive", "once")
        ]
        await second.shutdown()

    async def test_a_subscriber_added_after_the_fact_does_not_replay_history(
        self, path, gmail
    ) -> None:
        """`LATEST` is pinned at subscribe. A workflow deployed on Tuesday must
        not process Monday's traffic on its first tick."""
        first = Deployment(path, gmail)
        await first.register()
        for n in range(3):
            body = slack_body(f"Ev{n}", text=f"old-{n}")
            first.client.post("/hooks/slack", content=body, headers=slack_headers(body))
        await first.drive()
        await first.shutdown()

        second = Deployment(path, gmail)
        await second.register()

        @workflow(name="latecomer", triggers=[OnAppEvent(SLACK_TOPIC)])
        async def latecomer(ctx: Context, message: dict) -> str:
            SEEN.append(("late", message["text"]))
            return "ok"

        await second.dispatcher.register(latecomer)
        await second.drive()

        assert not any(kind == "late" for kind, _ in SEEN)

        body = slack_body("Ev9", text="new")
        second.client.post("/hooks/slack", content=body, headers=slack_headers(body))
        await second.drive()

        assert ("late", "new") in SEEN
        await second.shutdown()

    async def test_an_unverified_delivery_never_reaches_a_workflow(
        self, path, gmail
    ) -> None:
        """The whole chain refuses, not just the verifier: nothing is appended,
        so nothing can dispatch, so no restart can resurrect it."""
        deployment = Deployment(path, gmail)
        await deployment.register()

        response = deployment.client.post(
            "/hooks/slack", content=slack_body("Ev1", text="forged")
        )
        await deployment.drive()

        assert response.status_code == 401
        assert SEEN == []
        assert await deployment.runtime.events.head(SLACK_TOPIC) is None
        await deployment.shutdown()

    async def test_the_journal_and_the_event_log_share_one_store(
        self, path, gmail
    ) -> None:
        """The deployment story: `pip install loomflow`, one SQLite file, and
        both logs in it. No broker, no second migration."""
        deployment = Deployment(path, gmail)
        await deployment.register()
        body = slack_body("Ev1", text="one file")
        deployment.client.post(
            "/hooks/slack", content=body, headers=slack_headers(body)
        )
        await deployment.drive()

        runs = await deployment.runtime.list_runs()

        assert runs, "runs and events both landed in the same file"
        assert await deployment.runtime.events.head(SLACK_TOPIC) is not None
        await deployment.shutdown()
