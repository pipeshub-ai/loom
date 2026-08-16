"""Accepting a delivery: verify, expand, append.

Two things are being asserted here, and only one of them is about parsing.

The first is that a provider cannot get an event into the log without proving
who it is, and that the proof is over the bytes that arrived — a source that
verifies a re-serialised payload accepts anything, and it passes every
hand-written test because the round trip is lossless in the happy case.

The second is that the *ingress* owns identity. A source says what happened; the
event id, the topic and the dedupe are the log's, so that adding a provider
costs a verifier and a normaliser and nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

from loom import Runtime
from loom.core.exceptions import ConfigurationError
from loom.events import (
    EventSource,
    EventSourceRegistry,
    InboundEvent,
    MalformedDelivery,
    SourceContext,
    StoreBackedEventLog,
    VerificationFailed,
    WebhookIngress,
    topic_for,
)
from loom.stores.memory import MemoryStore
from loom.testing.conformance import verify_event_source
from loom.toolsets.jira.source import JiraSource, issue_key, normalise_event_type
from loom.toolsets.slack.source import SlackSource

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


@pytest.fixture
def wired() -> tuple[Runtime, StoreBackedEventLog, WebhookIngress]:
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    return runtime, runtime.events, WebhookIngress(runtime)


# ---------------------------------------------------------------------------
# Slack — the signed, handshaking, retry-happy case
# ---------------------------------------------------------------------------


def slack_headers(body: bytes, *, secret: str = SECRET, at: float | None = None):
    ts = str(int(at if at is not None else time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


MESSAGE = json.dumps({
    "type": "event_callback",
    "event_id": "Ev123",
    "team_id": "T1",
    "api_app_id": "A1",
    "event": {
        "type": "message",
        "channel": "C_TECH",
        "user": "U1",
        "text": "deploy finished",
        "ts": "1701234567.000200",
        "event_ts": "1701234567.000200",
    },
}).encode()


class TestSlackVerification:
    def test_an_authentic_delivery_verifies(self) -> None:
        SlackSource(SECRET).verify(_lower(slack_headers(MESSAGE)), MESSAGE)

    def test_a_tampered_body_is_rejected(self) -> None:
        """The realistic attack: replay a valid signature over new bytes."""
        headers = _lower(slack_headers(MESSAGE))

        with pytest.raises(VerificationFailed, match="signature"):
            SlackSource(SECRET).verify(headers, MESSAGE + b" ")

    def test_the_signature_is_over_the_raw_bytes_not_the_parsed_payload(
        self,
    ) -> None:
        """A JSON round trip reorders keys and changes spacing. If the source
        verified the re-serialised form, this would pass — and every real
        delivery through a gateway that reformats would fail instead."""
        headers = _lower(slack_headers(MESSAGE))
        reserialised = json.dumps(json.loads(MESSAGE), indent=2).encode()
        assert reserialised != MESSAGE

        with pytest.raises(VerificationFailed):
            SlackSource(SECRET).verify(headers, reserialised)

    def test_a_stale_delivery_is_a_replay(self) -> None:
        """A signature does not expire on its own; this is what bounds it."""
        headers = _lower(slack_headers(MESSAGE, at=time.time() - 3600))

        with pytest.raises(VerificationFailed, match="replay"):
            SlackSource(SECRET).verify(headers, MESSAGE)

    def test_a_future_timestamp_is_also_rejected(self) -> None:
        """Clock skew cuts both ways; a one-sided check is half a check."""
        headers = _lower(slack_headers(MESSAGE, at=time.time() + 3600))

        with pytest.raises(VerificationFailed):
            SlackSource(SECRET).verify(headers, MESSAGE)

    def test_missing_headers_are_rejected_not_ignored(self) -> None:
        with pytest.raises(VerificationFailed, match="missing"):
            SlackSource(SECRET).verify({}, MESSAGE)

    def test_no_secret_refuses_rather_than_accepting_everything(self) -> None:
        """An endpoint that accepts anything looks identical to one that works
        until somebody finds it."""
        with pytest.raises(VerificationFailed, match="SLACK_SIGNING_SECRET"):
            SlackSource("").verify(_lower(slack_headers(MESSAGE)), MESSAGE)

    def test_skipping_verification_takes_an_explicit_opt_in(self) -> None:
        SlackSource("", require_signature=False).verify({}, MESSAGE)

    def test_the_wrong_secret_fails_with_a_message_naming_the_real_cause(
        self,
    ) -> None:
        """The usual cause is a re-encoded body, not a wrong secret, and being
        told the wrong one costs an afternoon."""
        with pytest.raises(VerificationFailed) as exc:
            SlackSource("other-secret").verify(_lower(slack_headers(MESSAGE)), MESSAGE)

        assert "raw request body" in str(exc.value)


class TestSlackHandshake:
    async def test_the_challenge_is_echoed_verbatim(self, wired) -> None:
        _, _log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        body = json.dumps(
            {"type": "url_verification", "challenge": "3eZbrw1a", "token": "x"}
        ).encode()

        result = await ingress.receive("slack", slack_headers(body), body)

        assert result.challenge is not None
        assert result.challenge.body == "3eZbrw1a"
        assert result.challenge.content_type == "text/plain", (
            "Slack will not enable an endpoint that JSON-wraps the challenge"
        )

    async def test_a_handshake_appends_nothing(self, wired) -> None:
        """It is not a business event, and a workflow triaging one is a bug."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()

        await ingress.receive("slack", slack_headers(body), body)

        assert await log.head("app.slack.url_verification") is None

    async def test_an_unverified_handshake_is_never_answered(self, wired) -> None:
        """Otherwise the endpoint is an oracle: anyone who guesses the URL gets
        a signed-looking reply and can complete someone else's registration."""
        _, _, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()

        with pytest.raises(VerificationFailed):
            await ingress.receive("slack", {"x-slack-signature": "v0=nope"}, body)


class TestSlackExpansion:
    async def test_a_message_becomes_one_namespaced_event(self, wired) -> None:
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))

        result = await ingress.receive("slack", slack_headers(MESSAGE), MESSAGE)

        assert result.topics == ["app.slack.message"]
        stored = await log.read("app.slack.message", after=None, limit=10)
        assert len(stored) == 1
        assert stored[0].payload["text"] == "deploy finished"

    async def test_the_ordering_key_is_the_channel(self, wired) -> None:
        """Per key is the only ordering promise a partitioned backend keeps,
        and per channel is the one that matters for a conversation."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))

        await ingress.receive("slack", slack_headers(MESSAGE), MESSAGE)

        stored = await log.read("app.slack.message", after=None, limit=10)
        assert stored[0].record.key == "C_TECH"

    async def test_occurred_at_comes_from_slacks_own_timestamp(
        self, wired
    ) -> None:
        """A redelivery three days later must not look like a fresh event."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))

        await ingress.receive("slack", slack_headers(MESSAGE), MESSAGE)

        stored = await log.read("app.slack.message", after=None, limit=10)
        assert stored[0].record.occurred_at is not None
        assert stored[0].record.occurred_at.year == 2023

    async def test_a_bot_message_is_flagged_rather_than_dropped(
        self, wired
    ) -> None:
        """A triage workflow that replies to bots talks to itself — but dropping
        them here would hide the decision, so it is a field a filter can use."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        body = json.dumps({
            "type": "event_callback",
            "event_id": "Ev_bot",
            "event": {
                "type": "message",
                "channel": "C1",
                "bot_id": "B1",
                "text": "build failed",
            },
        }).encode()

        await ingress.receive("slack", slack_headers(body), body)

        stored = await log.read("app.slack.message", after=None, limit=10)
        assert stored[0].payload["bot"] is True

    async def test_an_unmodelled_type_is_dropped_not_errored(self, wired) -> None:
        """A 4xx here teaches Slack to disable the endpoint."""
        _, _, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        body = json.dumps({"type": "something_new"}).encode()

        result = await ingress.receive("slack", slack_headers(body), body)

        assert result.accepted and result.count == 0

    async def test_a_form_encoded_interactive_payload_is_unwrapped(
        self, wired
    ) -> None:
        """Slack posts these as form data with JSON inside a `payload` field. A
        source reading only JSON sees an empty dict and expands to nothing."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))
        from urllib.parse import quote

        inner = json.dumps({"type": "block_actions", "user": {"id": "U9"}})
        body = ("payload=" + quote(inner)).encode()

        result = await ingress.receive("slack", slack_headers(body), body)

        assert result.topics == ["app.slack.block_actions"]
        stored = await log.read("app.slack.block_actions", after=None, limit=10)
        assert stored[0].record.key == "U9"


class TestSlackRedelivery:
    async def test_all_three_retries_produce_one_event(self, wired) -> None:
        """Slack redelivers on any non-2xx or any response over three seconds,
        with the same `Ev…` in the body."""
        _, log, ingress = wired
        ingress.sources.register(SlackSource(SECRET))

        first = await ingress.receive("slack", slack_headers(MESSAGE), MESSAGE)
        for attempt in (1, 2):
            headers = slack_headers(MESSAGE)
            headers["X-Slack-Retry-Num"] = str(attempt)
            await ingress.receive("slack", headers, MESSAGE)

        stored = await log.read("app.slack.message", after=None, limit=10)
        assert len(stored) == 1, "a redelivery must deduplicate"
        assert stored[0].event_id == first.event_ids[0]

    def test_the_delivery_id_is_the_event_id_not_the_retry_number(self) -> None:
        """`X-Slack-Retry-Num` is what distinguishes the attempts, so using it
        would defeat exactly the dedupe it looks like it supports."""
        source = SlackSource(SECRET)
        payload = json.loads(MESSAGE)

        assert source.delivery_id({"x-slack-retry-num": "2"}, payload) == "Ev123"


# ---------------------------------------------------------------------------
# Jira — no delivery id, optional signature
# ---------------------------------------------------------------------------


ISSUE = json.dumps({
    "webhookEvent": "jira:issue_created",
    "timestamp": 1701234567000,
    "issue": {"id": "10001", "key": "ENG-4", "fields": {"summary": "It broke"}},
}).encode()


def jira_headers(body: bytes, *, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature": f"sha256={digest}"}


class TestJira:
    def test_an_authentic_delivery_verifies(self) -> None:
        JiraSource(SECRET).verify(_lower(jira_headers(ISSUE)), ISSUE)

    def test_an_unsigned_delivery_is_refused_by_default(self) -> None:
        """A webhook created in the admin UI cannot sign, and accepting one
        silently means an endpoint anybody can post issue events to."""
        with pytest.raises(VerificationFailed, match="JIRA_WEBHOOK_SECRET"):
            JiraSource("").verify({}, ISSUE)

    def test_a_configured_secret_with_no_header_says_which_side_is_wrong(
        self,
    ) -> None:
        with pytest.raises(VerificationFailed) as exc:
            JiraSource(SECRET).verify({}, ISSUE)

        assert "registered without a `secret`" in str(exc.value)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("jira:issue_created", "jira.issue_created"),
            ("jira:issue_updated", "jira.issue_updated"),
            ("comment_created", "jira.comment_created"),
            ("sprint_started", "jira.sprint_started"),
        ],
    )
    def test_both_naming_families_normalise_to_one(
        self, raw: str, expected: str
    ) -> None:
        """Jira prefixes issue events with `jira:` and leaves comment and sprint
        events bare. A subscriber should not have to know which."""
        assert normalise_event_type(raw) == expected

    def test_the_ordering_key_is_the_human_issue_key(self) -> None:
        assert issue_key({"issue": {"id": "10001", "key": "ENG-4"}}) == "ENG-4"

    def test_it_falls_back_to_the_id_when_there_is_no_key(self) -> None:
        assert issue_key({"issue": {"id": "10001"}}) == "10001"

    def test_the_timestamp_is_milliseconds(self) -> None:
        """Read as seconds it lands in the year 55000."""
        source = JiraSource(SECRET)
        events = _sync(source.expand(json.loads(ISSUE), _ctx("jira")))

        assert events[0].occurred_at is not None
        assert events[0].occurred_at.year == 2023

    async def test_it_publishes_no_delivery_id_so_the_body_is_hashed(
        self, wired
    ) -> None:
        """Jira sends none, and every substitute is worse: `timestamp` repeats
        across a redelivery *and* across two events in one millisecond, and
        `issue.id` would collapse an issue's whole history into one event."""
        _, log, ingress = wired
        ingress.sources.register(JiraSource(SECRET))

        first = await ingress.receive("jira", jira_headers(ISSUE), ISSUE)
        await ingress.receive("jira", jira_headers(ISSUE), ISSUE)

        stored = await log.read("app.jira.issue_created", after=None, limit=10)
        assert len(stored) == 1, "an identical redelivery must deduplicate"
        assert "sha256:" in first.event_ids[0]

    async def test_two_different_updates_are_two_events(self, wired) -> None:
        """The limitation of hashing is that identical bodies collapse; two
        genuinely different ones must not."""
        _, log, ingress = wired
        ingress.sources.register(JiraSource(SECRET))
        second = ISSUE.replace(b"It broke", b"It broke again")

        await ingress.receive("jira", jira_headers(ISSUE), ISSUE)
        await ingress.receive("jira", jira_headers(second), second)

        stored = await log.read("app.jira.issue_created", after=None, limit=10)
        assert len(stored) == 2

    async def test_a_lifecycle_callback_with_no_event_type_is_ignored(
        self, wired
    ) -> None:
        _, _, ingress = wired
        ingress.sources.register(JiraSource(SECRET))
        body = json.dumps({"key": "some-connect-app"}).encode()

        result = await ingress.receive("jira", jira_headers(body), body)

        assert result.count == 0 and result.accepted


# ---------------------------------------------------------------------------
# The ingress itself — identity, topics, grouping
# ---------------------------------------------------------------------------


class TestIngressOwnsIdentity:
    async def test_the_event_id_carries_the_topic(self, wired) -> None:
        """Two topics can legitimately carry one delivery. Without the topic in
        the id, whichever landed second would deduplicate away and the workflow
        subscribing to the other one would simply never run."""
        _, _log, ingress = wired
        ingress.sources.register(_TwoTopicSource())

        result = await ingress.receive("two", {}, b"{}")

        assert len(set(result.event_ids)) == 2
        assert all("app." in eid for eid in result.event_ids)
        assert result.event_ids[0].startswith("app.two.first/")

    async def test_several_events_from_one_delivery_stay_distinct(
        self, wired
    ) -> None:
        _, log, ingress = wired
        ingress.sources.register(_BatchSource())

        result = await ingress.receive("batch", {}, b"{}")

        assert len(set(result.event_ids)) == 3
        stored = await log.read("app.batch.item", after=None, limit=10)
        assert len(stored) == 3

    async def test_a_batch_is_appended_once_per_topic(self, wired) -> None:
        """`append` takes the topic's lock per call; forty events across two
        topics should cost two acquisitions rather than forty."""
        _, log, ingress = wired
        ingress.sources.register(_BatchSource())
        calls: list[int] = []
        original = log.append

        async def counting(topic: str, records: Any) -> Any:
            calls.append(len(records))
            return await original(topic, records)

        log.append = counting  # type: ignore[method-assign]
        await ingress.receive("batch", {}, b"{}")

        assert calls == [3], f"expected one call carrying three records, got {calls}"

    def test_the_topic_is_derived_from_the_event_type(self) -> None:
        assert topic_for("slack.message") == "app.slack.message"

    async def test_an_unknown_source_names_what_is_reachable(self, wired) -> None:
        _, _, ingress = wired

        with pytest.raises(ConfigurationError) as exc:
            await ingress.receive("shopify", {}, b"{}")

        assert "loom_event_source" in str(exc.value)

    def test_an_ingress_without_a_log_says_what_to_pass(self) -> None:
        runtime = Runtime(store=MemoryStore())

        with pytest.raises(ConfigurationError, match="StoreBackedEventLog"):
            WebhookIngress(runtime)

    async def test_headers_reach_expand_lower_cased(self, wired) -> None:
        """Several providers put the event type in a header. Case matters
        because every proxy capitalises differently."""
        _, _, ingress = wired
        seen: dict[str, str] = {}

        class Peeking(_TwoTopicSource):
            id = "peek"

            async def expand(self, payload: Any, ctx: SourceContext) -> Any:
                seen.update(ctx.headers)
                return []

        ingress.sources.register(Peeking())
        await ingress.receive("peek", {"X-Shopify-Topic": "orders/create"}, b"{}")

        assert seen["x-shopify-topic"] == "orders/create"


# ---------------------------------------------------------------------------
# The registry — how a third party arrives
# ---------------------------------------------------------------------------


class TestSourceRegistry:
    def test_a_runtime_chains_to_the_process_global_registry(self) -> None:
        from loom.events import register_event_source, unregister_event_source

        source = _BatchSource()
        register_event_source(source)
        try:
            assert Runtime(store=MemoryStore()).sources.get("batch") is source
        finally:
            unregister_event_source("batch")

    def test_a_local_registration_does_not_leak_to_another_runtime(self) -> None:
        first = Runtime(store=MemoryStore())
        second = Runtime(store=MemoryStore())
        first.sources.register(_BatchSource())

        assert second.sources.get("batch") is None

    def test_a_local_source_wins_over_the_parent(self) -> None:
        """Local-first is what makes a test's source local to the test."""
        parent = EventSourceRegistry()
        parent.register(_BatchSource())
        child = EventSourceRegistry(parent=parent)
        mine = _BatchSource()
        child.register(mine)

        assert child.get("batch") is mine

    def test_the_shipped_sources_are_reachable_without_registration(self) -> None:
        registry = EventSourceRegistry()

        assert isinstance(registry.get("slack"), SlackSource)
        assert isinstance(registry.get("jira"), JiraSource)

    def test_listing_includes_builtins_and_locals(self) -> None:
        registry = EventSourceRegistry()
        registry.register(_BatchSource())

        listed = registry.list_sources()
        assert "batch" in listed and "slack" in listed and "jira" in listed

    def test_a_source_without_an_id_is_refused(self) -> None:
        class Nameless:
            id = ""

        with pytest.raises(ConfigurationError, match="no `id`"):
            EventSourceRegistry().register(Nameless())  # type: ignore[arg-type]

    def test_a_source_satisfies_the_protocol_structurally(self) -> None:
        """No base class to inherit — that is what lets a third party write one
        without importing anything of ours but the dataclasses."""
        assert isinstance(SlackSource(SECRET), EventSource)
        assert isinstance(JiraSource(SECRET), EventSource)


# ---------------------------------------------------------------------------
# The conformance kit — what a third-party source proves about itself
# ---------------------------------------------------------------------------


class TestConformance:
    async def test_slack_conforms(self) -> None:
        await verify_event_source(
            SlackSource(SECRET),
            sign=lambda body: slack_headers(body),
            sample=MESSAGE,
            expected_types=["slack.message"],
        )

    async def test_jira_conforms(self) -> None:
        await verify_event_source(
            JiraSource(SECRET),
            sign=jira_headers,
            sample=ISSUE,
            expected_types=["jira.issue_created"],
        )

    async def test_the_kit_catches_a_source_that_parses_before_verifying(
        self,
    ) -> None:
        """The defect the kit exists for: it passes every hand-written test,
        because a JSON round trip is lossless in the happy case."""

        class Sloppy:
            id = "sloppy"

            def verify(self, headers: Any, body: bytes) -> None:
                # Re-serialises, then checks. Accepts reformatted bytes.
                canonical = json.dumps(json.loads(body), sort_keys=True).encode()
                digest = hmac.new(SECRET.encode(), canonical, hashlib.sha256)
                if headers.get("x-sig") != digest.hexdigest():
                    raise VerificationFailed("no")

            def challenge(self, headers: Any, body: bytes) -> None:
                return None

            def delivery_id(self, headers: Any, payload: Any) -> str:
                return "fixed"

            async def expand(self, payload: Any, ctx: Any) -> list[InboundEvent]:
                return [InboundEvent(type="sloppy.thing", payload={})]

        def sign(body: bytes) -> dict[str, str]:
            canonical = json.dumps(json.loads(body), sort_keys=True).encode()
            return {
                "x-sig": hmac.new(
                    SECRET.encode(), canonical, hashlib.sha256
                ).hexdigest()
            }

        with pytest.raises(AssertionError, match="raw bytes"):
            await verify_event_source(Sloppy(), sign=sign, sample=MESSAGE)

    async def test_the_kit_catches_an_unnamespaced_event_type(self) -> None:
        class Flat(JiraSource):
            id = "flat"

            async def expand(self, payload: Any, ctx: Any) -> list[InboundEvent]:
                return [InboundEvent(type="issue_created", payload={})]

        with pytest.raises(AssertionError, match="namespaced"):
            await verify_event_source(Flat(SECRET), sign=jira_headers, sample=ISSUE)

    async def test_the_kit_catches_an_unstable_delivery_id(self) -> None:
        counter = {"n": 0}

        class Drifting(JiraSource):
            id = "drifting"

            def delivery_id(self, headers: Any, payload: Any) -> str:
                counter["n"] += 1
                return f"id-{counter['n']}"

        with pytest.raises(AssertionError, match="not stable"):
            await verify_event_source(
                Drifting(SECRET), sign=jira_headers, sample=ISSUE
            )

    async def test_the_kit_catches_a_source_that_never_verifies(self) -> None:
        class Open(JiraSource):
            id = "open"

            def verify(self, headers: Any, body: bytes) -> None:
                return None

        with pytest.raises(AssertionError, match="does not match its signature"):
            await verify_event_source(Open(SECRET), sign=jira_headers, sample=ISSUE)


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _ctx(source_id: str) -> SourceContext:
    from loom.events.sources import SourceState

    return SourceContext(source_id=source_id, state=SourceState(MemoryStore(), source_id))


def _sync(coro: Any) -> Any:
    """Drive a coroutine from a synchronous test."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _TwoTopicSource:
    """One delivery, two topics — Slack's app_mention is also a message."""

    id = "two"

    def verify(self, headers: Any, body: bytes) -> None:
        return None

    def challenge(self, headers: Any, body: bytes) -> None:
        return None

    def delivery_id(self, headers: Any, payload: Any) -> str:
        return "D1"

    async def expand(self, payload: Any, ctx: SourceContext) -> list[InboundEvent]:
        return [
            InboundEvent(type="two.first", payload={"n": 1}),
            InboundEvent(type="two.second", payload={"n": 2}),
        ]


class _BatchSource:
    id = "batch"

    def verify(self, headers: Any, body: bytes) -> None:
        return None

    def challenge(self, headers: Any, body: bytes) -> None:
        return None

    def delivery_id(self, headers: Any, payload: Any) -> str:
        return "B1"

    async def expand(self, payload: Any, ctx: SourceContext) -> list[InboundEvent]:
        return [InboundEvent(type="batch.item", payload={"n": n}) for n in range(3)]


class TestMalformed:
    def test_a_non_object_payload_is_malformed_not_a_crash(self) -> None:
        with pytest.raises(MalformedDelivery):
            _sync(SlackSource(SECRET).expand([1, 2, 3], _ctx("slack")))

    def test_an_event_callback_with_no_event_is_malformed(self) -> None:
        with pytest.raises(MalformedDelivery, match="no `event`"):
            _sync(
                SlackSource(SECRET).expand(
                    {"type": "event_callback"}, _ctx("slack")
                )
            )
