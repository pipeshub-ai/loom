"""The two ingress routes, over real HTTP.

`/hooks/{source}` is the provider-typed one — LOOM knows who is calling, so it
can verify a signature, answer a handshake, and fan one delivery out to every
subscriber. `/webhook{path}` is the URL ``Webhook.describe()`` has been
publishing all along, and an advertised URL is a promise: providers are already
configured against it.

The status codes are the contract here. A provider decides whether to resend
from them, so getting one wrong either loses an event or delivers it forever.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

from loom import Context, Runtime, workflow
from loom.events import (
    EventDispatcher,
    StoreBackedEventLog,
)
from loom.stores.memory import MemoryStore
from loom.toolsets.slack.source import SlackSource
from loom.triggers.filter import FilterSpec
from loom.triggers.specs import OnAppEvent, Webhook

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from loom.server.app import create_app  # noqa: E402

SECRET = "test-signing-secret"
RAN: list[tuple[str, Any]] = []


@pytest.fixture(autouse=True)
def _clear() -> None:
    RAN.clear()


@workflow(name="triage_http", triggers=[
    OnAppEvent("app.slack.message", where=FilterSpec(conditions={"channel": "C_TECH"})),
])
async def triage(ctx: Context, message: dict) -> str:
    RAN.append(("triage", message.get("text")))
    return "ok"


@workflow(name="on_push", triggers=[
    Webhook("/gh/push", idempotency_header="X-GitHub-Delivery"),
])
async def on_push(ctx: Context, delivery: dict) -> str:
    RAN.append(("push", delivery))
    return "ok"


def sign(body: bytes, *, at: float | None = None) -> dict[str, str]:
    ts = str(int(at if at is not None else time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


def message_body(event_id: str = "Ev1", *, channel: str = "C_TECH") -> bytes:
    return json.dumps({
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": channel,
            "text": "deploy finished",
            "ts": "1701234567.000200",
        },
    }).encode()


@pytest.fixture
def served() -> tuple[Runtime, TestClient]:
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    runtime.sources.register(SlackSource(SECRET))
    runtime.register(on_push)
    return runtime, TestClient(create_app(runtime))


class TestProviderIngress:
    def test_a_signed_delivery_is_accepted_and_logged(self, served) -> None:
        _, client = served
        body = message_body()

        response = client.post("/hooks/slack", content=body, headers=sign(body))

        assert response.status_code == 202
        assert response.json()["topics"] == ["app.slack.message"]

    def test_an_unsigned_delivery_is_401(self, served) -> None:
        """401 and never retried — this one is somebody lying about who they
        are, which is a different thing from a payload we cannot parse."""
        _, client = served

        response = client.post("/hooks/slack", content=message_body())

        assert response.status_code == 401

    def test_a_tampered_body_is_401(self, served) -> None:
        _, client = served
        body = message_body()
        headers = sign(body)

        response = client.post("/hooks/slack", content=body + b" ", headers=headers)

        assert response.status_code == 401

    def test_an_unknown_source_is_404(self, served) -> None:
        _, client = served

        response = client.post("/hooks/shopify", content=b"{}")

        assert response.status_code == 404
        assert "loom_event_source" in response.json()["detail"]

    def test_the_handshake_returns_the_bare_challenge(self, served) -> None:
        """JSON-wrapping it means Slack never enables the endpoint, and the
        failure it reports is 'we did not get the challenge back'."""
        _, client = served
        body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()

        response = client.post("/hooks/slack", content=body, headers=sign(body))

        assert response.status_code == 200
        assert response.text == "abc123"
        assert response.headers["content-type"].startswith("text/plain")

    def test_a_delivery_with_no_configured_log_is_503_not_202(self) -> None:
        """A 2xx we cannot back up is a lost event nobody will resend."""
        runtime = Runtime(store=MemoryStore())
        client = TestClient(create_app(runtime))
        body = message_body()

        response = client.post("/hooks/slack", content=body, headers=sign(body))

        assert response.status_code == 503
        assert "StoreBackedEventLog" in response.json()["detail"]

    def test_the_response_does_not_wait_for_any_workflow(self, served) -> None:
        """Slack retries anything slower than three seconds, so a dispatcher
        running inline would turn a slow workflow into a duplicate delivery."""
        _, client = served
        body = message_body()

        client.post("/hooks/slack", content=body, headers=sign(body))

        assert RAN == [], "the route must return on the append, not on the run"

    async def test_a_delivery_reaches_a_subscribed_workflow(self, served) -> None:
        """The whole path: HTTP in, verified, appended, dispatched, run."""
        runtime, client = served
        dispatcher = EventDispatcher(runtime)
        await dispatcher.register(triage)

        body = message_body()
        assert client.post("/hooks/slack", content=body, headers=sign(body)).status_code == 202
        await dispatcher.poll_once()
        for _ in range(50):
            import asyncio

            await asyncio.sleep(0)

        assert RAN == [("triage", "deploy finished")]

    async def test_a_filtered_out_channel_starts_nothing(self, served) -> None:
        runtime, client = served
        dispatcher = EventDispatcher(runtime)
        await dispatcher.register(triage)

        body = message_body("Ev2", channel="C_RANDOM")
        client.post("/hooks/slack", content=body, headers=sign(body))
        await dispatcher.poll_once()

        assert RAN == []


class TestWebhookTriggerRoutes:
    def test_the_advertised_production_url_fires_the_workflow(
        self, served
    ) -> None:
        """`Webhook.describe()` publishes `/webhook{path}`, and providers are
        configured against it — an advertised URL is a contract."""
        _, client = served

        response = client.post("/webhook/gh/push", json={"ref": "main"})

        assert response.status_code == 202
        assert len(response.json()["runs"]) == 1

    def test_the_test_url_is_separate(self, served) -> None:
        """Two URLs exist so that pointing a provider at a laptop cannot fire
        production runs."""
        _, client = served

        response = client.post("/webhook-test/gh/push", json={"ref": "main"})

        assert response.status_code == 202

    def test_a_path_nothing_listens_on_is_404(self, served) -> None:
        """A quiet 202 looks identical to a working integration, and is found
        when somebody asks why nothing happened."""
        _, client = served

        assert client.post("/webhook/nope", json={}).status_code == 404

    def test_the_body_headers_and_query_are_carried_separately(
        self, served
    ) -> None:
        """Merged, a provider posting a field called `headers` would overwrite
        them and the workflow would read the wrong thing with nothing to
        notice."""
        _, client = served

        client.post(
            "/webhook/gh/push?dry=1",
            json={"headers": "not the real ones"},
            headers={"X-GitHub-Delivery": "d1"},
        )

        (_, delivery), = RAN
        assert delivery["body"] == {"headers": "not the real ones"}
        assert delivery["headers"]["x-github-delivery"] == "d1"
        assert delivery["query"] == {"dry": "1"}

    def test_the_idempotency_header_dedupes_a_redelivery(self, served) -> None:
        _, client = served
        headers = {"X-GitHub-Delivery": "same"}

        first = client.post("/webhook/gh/push", json={}, headers=headers)
        second = client.post("/webhook/gh/push", json={}, headers=headers)

        assert first.json()["runs"] == second.json()["runs"], (
            "a redelivery carrying the provider's own id must resolve to the "
            "original run rather than starting a second"
        )

    def test_a_method_the_trigger_does_not_declare_does_not_fire(
        self, served
    ) -> None:
        _, client = served

        assert client.get("/webhook/gh/push").status_code == 404
