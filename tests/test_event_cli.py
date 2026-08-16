"""``loom events``, driven the way an operator drives it.

Through ``main(argv)`` rather than by calling the handlers, because most of what
can break here is not in the handler: it is argument parsing, the store the
command resolves, the exit code, and whether ``--json`` produces something `jq`
can read. A test that calls the coroutine directly proves none of that.

The exit codes carry the weight. ``loom events status`` exists to be run by a
monitoring check, and a status command that always exits 0 can only be read by a
person — which is exactly the failure this subsystem exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loom.cli import main
from loom.events import (
    EventRecord,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
)
from loom.events.manager import SubscriptionManager
from loom.stores.sqlite import SQLiteStore

TOPIC = "app.slack.message"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store the CLI will resolve from the environment, as a host would."""
    path = tmp_path / "runs.db"
    monkeypatch.setenv("LOOM_STORE", f"sqlite://{path}")
    # Keep the CLI from finding a pyproject's `[tool.loom] modules` and
    # importing this repo's workflows into every case.
    monkeypatch.chdir(tmp_path)
    return path


def sync(coro: Any) -> Any:
    """Drive a coroutine from a synchronous test.

    Synchronous on purpose: ``cmd_events`` calls ``asyncio.run``, exactly as a
    CLI process does, and an ``async def`` test is already inside a loop — so
    testing this from one would exercise a path the real command never takes.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(
    path: Path,
    *,
    events: int = 5,
    subscriber: str | None = "triage",
    position: str | None = None,
    dead: int = 0,
) -> None:
    store = SQLiteStore(str(path))
    log = StoreBackedEventLog(store)
    marks = StoreBackedCheckpoints(store)

    if events:
        await log.append(TOPIC, [
            EventRecord(
                event_id=f"{TOPIC}/slack:Ev{i}",
                type="slack.message",
                payload={"channel": "C_TECH", "n": i},
                key="C_TECH",
                source="slack",
            )
            for i in range(events)
        ])
    if dead:
        await log.append(f"{TOPIC}.dead", [
            EventRecord(
                event_id=f"{TOPIC}.dead/slack:Ev{i}",
                type="slack.message.dead",
                payload={
                    "event_id": f"Ev{i}",
                    "subscriber": "triage",
                    "workflow": "triage",
                    "error": "TypeError: payload is not a dict",
                    "original": {"n": i},
                },
            )
            for i in range(dead)
        ])
    if subscriber is not None:
        manager = SubscriptionManager(store, log=log, checkpoints=marks)
        await manager.add(Subscription(subscriber, TOPIC, "triage"))
        if position is not None:
            await marks.commit(subscriber, TOPIC, position)


def seed(*args: Any, **kw: Any) -> None:
    sync(_seed(*args, **kw))


def run(capsys: Any, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_json(capsys: Any, *argv: str) -> tuple[int, Any]:
    code, out = run(capsys, *argv, "--json")
    return code, json.loads(out or "null")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestTopics:
    def test_it_lists_a_topic_and_its_head(self, db, capsys) -> None:
        seed(db)

        code, rows = run_json(capsys, "events", "topics")

        assert code == 0
        assert {"topic": TOPIC, "head": "5", "empty": False} in rows

    def test_an_empty_deployment_says_so_rather_than_erroring(
        self, db, capsys
    ) -> None:
        """A fresh install running this should learn something, not see a
        traceback about a table that was never created."""
        code, out = run(capsys, "events", "topics")

        assert code == 0
        assert "no topics" in out


class TestTail:
    def test_it_shows_the_most_recent_events(self, db, capsys) -> None:
        """The most recent, not the oldest: `tail` answers "is the webhook
        reaching us *now*", and the first page of a busy topic answers a
        question nobody asked."""
        seed(db, events=10)

        code, rows = run_json(capsys, "events", "tail", TOPIC, "--limit", "3")

        assert code == 0
        assert [r["payload"]["n"] for r in rows] == [7, 8, 9]

    def test_it_carries_what_an_operator_needs_to_correlate(
        self, db, capsys
    ) -> None:
        seed(db, events=1)

        _, (row,) = run_json(capsys, "events", "tail", TOPIC)

        assert row["event_id"] == f"{TOPIC}/slack:Ev0"
        assert row["source"] == "slack"
        assert row["key"] == "C_TECH"
        assert row["appended_at"]

    def test_a_topic_with_nothing_on_it_is_not_an_error(
        self, db, capsys
    ) -> None:
        """"Nothing arrived" is the answer, and it is a useful one."""
        seed(db, events=0, subscriber=None)

        code, out = run(capsys, "events", "tail", "app.slack.message")

        assert code == 0
        assert "nothing on" in out


class TestSubscriptions:
    def test_it_reports_position_and_lag(self, db, capsys) -> None:
        seed(db, events=10, position="4")

        code, (row,) = run_json(capsys, "events", "subscriptions")

        assert code == 0
        assert row["position"] == "4" and row["lag"] == 6

    def test_a_subscriber_that_has_never_run_reads_as_new(
        self, db, capsys
    ) -> None:
        """Not *behind*. A subscription registered a minute ago and one wedged
        for a week must not look the same."""
        seed(db, events=3, position=None)

        _, out = run(capsys, "events", "subscriptions")

        assert "new" in out

    def test_it_scopes_to_one_topic(self, db, capsys) -> None:
        seed(db)
        store = SQLiteStore(str(db))
        manager = SubscriptionManager(store, log=StoreBackedEventLog(store))
        sync(manager.add(Subscription("other", "app.jira.issue_created", "w")))

        _, rows = run_json(capsys, "events", "subscriptions", "--topic", TOPIC)

        assert [r["subscriber"] for r in rows] == ["triage"]


# ---------------------------------------------------------------------------
# status — the one an alert reads
# ---------------------------------------------------------------------------


class TestStatus:
    def test_a_healthy_deployment_exits_zero(self, db, capsys) -> None:
        seed(db, events=3, position="3")

        code, out = run(capsys, "events", "status")

        assert code == 0
        assert "all current" in out

    def test_a_lagging_subscriber_exits_one(self, db, capsys) -> None:
        """The exit code is the point: a status command that always succeeds
        can only be read by a person."""
        seed(db, events=10, position="1")

        code, payload = run_json(capsys, "events", "status", "--max-lag", "5")

        assert code == 1
        assert payload["ok"] is False
        assert payload["lagging"][0]["subscriber"] == "triage"

    def test_lag_under_the_threshold_is_not_reported(
        self, db, capsys
    ) -> None:
        """Otherwise every deployment is permanently amber and nobody looks."""
        seed(db, events=10, position="9")

        code, payload = run_json(capsys, "events", "status", "--max-lag", "5")

        assert code == 0 and payload["ok"]

    def test_a_dead_letter_topic_exits_one(self, db, capsys) -> None:
        """Undeliverable events are a problem even when every subscriber is
        current — which is precisely when nobody would otherwise notice."""
        seed(db, events=3, position="3", dead=2)

        code, payload = run_json(capsys, "events", "status")

        assert code == 1
        assert payload["dead_letters"][0]["topic"] == f"{TOPIC}.dead"

    def test_a_quarantined_subscriber_exits_one(self, db, capsys) -> None:
        seed(db, events=3, position="3")
        store = SQLiteStore(str(db))
        manager = SubscriptionManager(
            store, log=StoreBackedEventLog(store),
            checkpoints=StoreBackedCheckpoints(store),
        )
        sync(manager.quarantine("triage", TOPIC, "abandoned"))

        code, payload = run_json(capsys, "events", "status")

        assert code == 1
        assert payload["unhealthy"][0]["reason"] == "abandoned"

    def test_an_empty_deployment_is_healthy_not_broken(
        self, db, capsys
    ) -> None:
        code, _ = run(capsys, "events", "status")

        assert code == 0


class TestDead:
    def test_it_shows_why_each_one_failed(self, db, capsys) -> None:
        """A dead-letter nobody can read is a log line with extra steps."""
        seed(db, dead=2)

        code, rows = run_json(capsys, "events", "dead")

        assert code == 0
        assert len(rows) == 2
        assert rows[0]["error"].startswith("TypeError")
        assert rows[0]["original"] == {"n": 0}

    def test_the_suffix_is_optional(self, db, capsys) -> None:
        """An operator holds the topic their workflow subscribes to, not the
        dead-letter's name."""
        seed(db, dead=1)

        _, with_suffix = run_json(capsys, "events", "dead", f"{TOPIC}.dead")
        _, without = run_json(capsys, "events", "dead", TOPIC)

        assert with_suffix == without and len(without) == 1

    def test_nothing_dead_lettered_is_not_an_error(self, db, capsys) -> None:
        seed(db)

        code, out = run(capsys, "events", "dead")

        assert code == 0
        assert "nothing dead-lettered" in out


# ---------------------------------------------------------------------------
# replay — the only thing here that changes anything
# ---------------------------------------------------------------------------


class TestReplay:
    def test_it_prints_the_plan_and_does_nothing_without_yes(
        self, db, capsys
    ) -> None:
        """The number is the safeguard. `EARLIEST` is refused in a declaration
        because the author cannot see it; here it is printed before anything
        happens."""
        seed(db, events=10, position="10")

        code, payload = run_json(
            capsys, "events", "replay", "--subscriber", "triage", "--topic", TOPIC,
            "--max-events", "4",
        )

        assert code == 0
        assert payload["events"] == 4 and payload["applied"] is False

        store = SQLiteStore(str(db))
        assert sync(StoreBackedCheckpoints(store).load("triage", TOPIC)) == "10", (
            "planning must not move the checkpoint"
        )

    def test_yes_applies_exactly_what_the_plan_said(
        self, db, capsys
    ) -> None:
        seed(db, events=10, position="10")

        code, payload = run_json(
            capsys, "events", "replay", "--subscriber", "triage", "--topic", TOPIC,
            "--max-events", "4", "--yes",
        )

        assert code == 0 and payload["applied"] is True
        store = SQLiteStore(str(db))
        after = sync(StoreBackedCheckpoints(store).load("triage", TOPIC))
        remaining = sync(
            StoreBackedEventLog(store).read(TOPIC, after=after, limit=50)
        )
        assert len(remaining) == payload["events"] == 4

    def test_a_since_window_narrows_the_plan(self, db, capsys) -> None:
        seed(db, events=5, position="5")

        _, recent = run_json(
            capsys, "events", "replay", "--subscriber", "triage", "--topic", TOPIC,
            "--since", "1h",
        )
        _, nothing = run_json(
            capsys, "events", "replay", "--subscriber", "triage", "--topic", TOPIC,
            "--since", "0s",
        )

        assert recent["events"] == 5
        assert nothing["events"] == 0

    def test_a_bare_number_is_refused_rather_than_guessed(
        self, db, capsys
    ) -> None:
        """`--since 7` meaning seconds when the writer meant days turns a
        deliberate backfill into a no-op that reports success."""
        seed(db)

        with pytest.raises(SystemExit):
            main([
                "events", "replay", "--subscriber", "triage", "--topic", TOPIC,
                "--since", "7",
            ])

    def test_a_missing_required_argument_is_a_usage_error(self, db) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["events", "replay", "--topic", TOPIC])

        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Shape of the command itself
# ---------------------------------------------------------------------------


class TestCommandShape:
    def test_no_subcommand_is_a_usage_error_naming_them(self, db, capsys) -> None:
        code = main(["events"])

        assert code == 2
        assert "topics" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "sub", ["topics", "subscriptions", "status", "dead"]
    )
    def test_every_read_command_emits_parseable_json(
        self, db, capsys, sub: str
    ) -> None:
        """`--json` on every command, so output pipes into `jq` — the same
        contract every other loom command keeps."""
        seed(db, dead=1)

        _, payload = run_json(capsys, "events", sub)

        assert payload is not None

    def test_json_mode_prints_nothing_but_json(self, db, capsys) -> None:
        """A stray human line makes `loom events status --json | jq` fail on a
        healthy deployment, which is when nobody is watching."""
        seed(db, events=3, position="3")

        _, out = run(capsys, "events", "status", "--json")

        json.loads(out)

    def test_there_is_no_install_command(self, db) -> None:
        """Registering a webhook with a provider means owning N provider admin
        APIs forever for a once-per-deployment act. Deliberately absent, and
        pinned so it does not arrive by accident."""
        with pytest.raises(SystemExit) as exc:
            main(["events", "install", "slack"])

        assert exc.value.code == 2
