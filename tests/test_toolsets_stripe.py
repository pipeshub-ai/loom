"""Stripe toolset — the traps this API sets, pinned.

Four of them, each a mistake that returns something plausible rather than an
error: form encoding rather than JSON, an idempotency key that must come from
the caller, paging whose cursor lives in the rows, and errors whose
retryability is in ``error.type`` and not the status.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

import pytest

from loom.core.exceptions import NonRetryableError
from loom.events.sources import VerificationFailed
from loom.testing.conformance import verify_effect_profile, verify_event_source
from loom.toolsets.stripe.client import (
    StripeAuthError,
    StripeCardDeclined,
    StripeClient,
    StripeIdempotencyConflict,
    StripeInvalidRequest,
    StripeNotFound,
    StripeRateLimited,
    _classify,
    form_encode,
)
from loom.toolsets.stripe.manifest import STRIPE_MANIFEST
from loom.toolsets.stripe.models import StripePaymentIntent
from loom.toolsets.stripe.source import StripeSource, parse_signature_header

KEY = "sk_test_abc123"


# ---------------------------------------------------------------------------
# A recording transport, so every test asserts on what went over the wire
# ---------------------------------------------------------------------------


class Call:
    """One recorded request."""

    def __init__(self, method: str, url: str, headers: Any, content: Any, params: Any):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self.content = content
        self.params = params

    @property
    def form(self) -> dict[str, str]:
        """The request body, decoded back out of form encoding."""
        raw = self.content
        if isinstance(raw, bytes):
            raw = raw.decode()
        return dict(parse_qsl(raw or "", keep_blank_values=True))


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace httpx with a transport that records and replies from a queue."""
    import httpx

    calls: list[Call] = []
    queue: list[tuple[int, dict[str, Any]]] = []

    class FakeResponse:
        def __init__(self, status: int, body: dict[str, Any]):
            self.status_code = status
            self._body = body
            self.headers = {"request-id": "req_test"}

        @property
        def content(self) -> bytes:
            return json.dumps(self._body).encode()

        def json(self) -> dict[str, Any]:
            return self._body

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def request(
            self,
            method: str,
            url: str,
            headers: Any = None,
            params: Any = None,
            content: Any = None,
        ) -> FakeResponse:
            calls.append(Call(method, url, headers, content, params))
            status, body = queue.pop(0) if queue else (200, {})
            return FakeResponse(status, body)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    class Wire:
        def __init__(self) -> None:
            self.calls = calls

        def reply(self, body: dict[str, Any], status: int = 200) -> None:
            queue.append((status, body))

        @property
        def last(self) -> Call:
            return calls[-1]

    return Wire()


def client() -> StripeClient:
    return StripeClient(api_key=KEY)


# ---------------------------------------------------------------------------
# Form encoding — the trap that reads as a wrong value
# ---------------------------------------------------------------------------


class TestFormEncoding:
    def test_nested_dicts_use_bracket_syntax(self) -> None:
        assert form_encode({"metadata": {"order": "A-1"}}) == [
            ("metadata[order]", "A-1")
        ]

    def test_lists_are_indexed(self) -> None:
        """Stripe reads ``expand[0]`` and ignores a repeated bare ``expand``."""
        assert form_encode({"expand": ["customer", "invoice"]}) == [
            ("expand[0]", "customer"),
            ("expand[1]", "invoice"),
        ]

    def test_none_is_dropped_not_stringified(self) -> None:
        """Form encoding has no null, so an omitted key is the only "leave it".

        Sending it would set the field to the literal text ``None``.
        """
        assert form_encode({"name": None, "email": "a@b.test"}) == [
            ("email", "a@b.test")
        ]

    def test_booleans_are_lowercase(self) -> None:
        assert form_encode({"paid": True, "refunded": False}) == [
            ("paid", "true"),
            ("refunded", "false"),
        ]

    def test_dicts_inside_lists_nest_correctly(self) -> None:
        assert form_encode({"items": [{"price": "p_1", "quantity": 2}]}) == [
            ("items[0][price]", "p_1"),
            ("items[0][quantity]", "2"),
        ]

    @pytest.mark.asyncio()
    async def test_a_write_is_sent_as_a_form_not_json(self, wire: Any) -> None:
        wire.reply({"id": "cus_1", "email": "a@b.test"})
        await client().create_customer(idempotency_key="k1", email="a@b.test")

        assert wire.last.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert wire.last.form == {"email": "a@b.test"}


# ---------------------------------------------------------------------------
# Idempotency — the key comes from the caller, never from here
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio()
    async def test_a_write_sends_the_callers_key(self, wire: Any) -> None:
        wire.reply({"id": "cus_1"})
        await client().create_customer(idempotency_key="order-42", email="a@b.test")
        assert wire.last.headers["Idempotency-Key"] == "order-42"

    @pytest.mark.asyncio()
    async def test_two_attempts_with_one_key_send_the_same_key(self, wire: Any) -> None:
        """The property the parameter exists for.

        A key minted inside the client would be new on every attempt, which is
        exactly the case Stripe's idempotency is there to prevent.
        """
        wire.reply({"id": "cus_1"})
        wire.reply({"id": "cus_1"})
        target = client()
        await target.create_customer(idempotency_key="order-42", email="a@b.test")
        await target.create_customer(idempotency_key="order-42", email="a@b.test")
        assert [c.headers["Idempotency-Key"] for c in wire.calls] == [
            "order-42",
            "order-42",
        ]

    @pytest.mark.asyncio()
    async def test_a_read_sends_no_key(self, wire: Any) -> None:
        wire.reply({"id": "cus_1"})
        await client().get_customer("cus_1")
        assert "Idempotency-Key" not in wire.last.headers

    @pytest.mark.asyncio()
    async def test_an_update_sends_no_key(self, wire: Any) -> None:
        """Setting the same fields twice reaches the same end state."""
        wire.reply({"id": "cus_1"})
        await client().update_customer("cus_1", values={"name": "Ada"})
        assert "Idempotency-Key" not in wire.last.headers


# ---------------------------------------------------------------------------
# Error classification — on the type, not the status
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def _error(self, status: int, **error: Any) -> Any:
        return _classify(status, {"error": error}, "req_1")

    def test_a_declined_card_is_not_retryable(self) -> None:
        """It will decline again; retrying burns the budget to learn nothing."""
        raised = self._error(402, type="card_error", code="insufficient_funds")
        assert isinstance(raised, StripeCardDeclined)
        assert isinstance(raised, NonRetryableError)
        assert raised.code == "insufficient_funds"

    def test_a_bad_parameter_is_not_retryable(self) -> None:
        raised = self._error(400, type="invalid_request_error")
        assert isinstance(raised, StripeInvalidRequest)
        assert isinstance(raised, NonRetryableError)

    def test_a_rate_limit_is_retryable(self) -> None:
        raised = self._error(429, type="rate_limit_error")
        assert isinstance(raised, StripeRateLimited)
        assert not isinstance(raised, NonRetryableError)

    def test_a_server_error_is_retryable(self) -> None:
        """Stripe's own, so worth another attempt — unlike everything above."""
        raised = self._error(500, type="api_error")
        assert not isinstance(raised, NonRetryableError)

    def test_a_reused_key_with_different_parameters_is_its_own_type(self) -> None:
        raised = self._error(400, type="idempotency_error")
        assert isinstance(raised, StripeIdempotencyConflict)

    def test_a_missing_object_names_the_test_live_mode_trap(self) -> None:
        """The likeliest cause when an id was copied from a dashboard."""
        raised = self._error(404, type="invalid_request_error", code="resource_missing")
        assert isinstance(raised, StripeNotFound)
        assert "test-mode" in str(raised)

    def test_the_type_outranks_the_status(self) -> None:
        """A 400 carrying a card_error is still a decline, not a bad request."""
        assert isinstance(self._error(400, type="card_error"), StripeCardDeclined)

    def test_the_request_id_is_carried(self) -> None:
        """The first thing Stripe support asks for."""
        assert self._error(500, type="api_error").request_id == "req_1"

    def test_the_decline_code_is_carried(self) -> None:
        """The *issuer's* reason, and the actionable half.

        ``code`` says ``card_declined``; ``decline_code`` says
        ``insufficient_funds`` against ``lost_card`` — a different
        conversation with the customer, and in one case with fraud.
        """
        raised = self._error(
            402, type="card_error", code="card_declined", decline_code="lost_card"
        )
        assert raised.decline_code == "lost_card"

    def test_the_only_four_error_types_are_the_documented_ones(self) -> None:
        """https://docs.stripe.com/api/errors lists exactly these four.

        There is no ``authentication_error`` and no ``rate_limit_error``,
        though both read as though there should be — an earlier version of the
        table listed them, which claimed a contract the API does not have.
        Authentication and rate limiting are *statuses* here.
        """
        from loom.toolsets.stripe.client import _PERMANENT_TYPES

        assert set(_PERMANENT_TYPES) == {
            "card_error",
            "invalid_request_error",
            "idempotency_error",
        }, "api_error is retryable, so it is deliberately absent"

    def test_a_403_names_the_key_rather_than_the_parameters(self) -> None:
        """"The API key doesn't have permissions to perform the request" — a
        restricted key missing a scope, not a malformed request."""
        raised = self._error(403)
        assert isinstance(raised, StripeAuthError)
        assert "permission" in str(raised)

    def test_a_409_is_an_idempotency_conflict_even_with_no_type(self) -> None:
        """"The request conflicts with another request (perhaps due to using
        the same idempotent key)." """
        assert isinstance(self._error(409), StripeIdempotencyConflict)

    def test_a_424_is_retryable(self) -> None:
        """"External Dependency Failed" — something Stripe depends on, not the
        request. The only 4xx here that is worth sending again."""
        assert not isinstance(self._error(424), NonRetryableError)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_a_missing_key_fails_at_construction(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("STRIPE_API_KEY", raising=False)
        with pytest.raises(StripeAuthError, match="STRIPE_API_KEY"):
            StripeClient()

    def test_a_publishable_key_is_refused_by_name(self) -> None:
        """It authenticates and can read almost nothing.

        Left to the API it surfaces as a scatter of 401s across unrelated
        calls rather than as "you used the browser key".
        """
        with pytest.raises(StripeAuthError, match="publishable"):
            StripeClient(api_key="pk_test_abc")

    def test_livemode_is_readable_from_the_key(self) -> None:
        assert StripeClient(api_key="sk_live_x").livemode is True
        assert StripeClient(api_key="sk_test_x").livemode is False

    @pytest.mark.asyncio()
    async def test_the_api_version_is_pinned(self, wire: Any) -> None:
        """Without it Stripe uses the account default, so a response shape can
        change under a workflow with no deploy on this side."""
        wire.reply({"id": "cus_1"})
        await client().get_customer("cus_1")
        assert wire.last.headers["Stripe-Version"]


# ---------------------------------------------------------------------------
# Paging — the cursor is the last row's id
# ---------------------------------------------------------------------------


class TestPaging:
    @pytest.mark.asyncio()
    async def test_it_follows_starting_after_through_pages(self, wire: Any) -> None:
        wire.reply(
            {
                "data": [{"id": "pi_1", "amount": 100}, {"id": "pi_2", "amount": 200}],
                "has_more": True,
            }
        )
        wire.reply({"data": [{"id": "pi_3", "amount": 300}], "has_more": False})

        found = await client().list_payment_intents(limit=10)

        assert [p.id for p in found] == ["pi_1", "pi_2", "pi_3"]
        assert found.complete is True
        assert "starting_after=pi_2" in str(wire.calls[1].params)

    @pytest.mark.asyncio()
    async def test_coverage_survives_the_mapping(self, wire: Any) -> None:
        """`.mapped` rather than a comprehension, which drops `.complete`."""
        wire.reply({"data": [{"id": "pi_1"}], "has_more": True})
        found = await client().list_payment_intents(limit=1)
        assert found.complete is False

    @pytest.mark.asyncio()
    async def test_a_filter_travels_with_every_page(self, wire: Any) -> None:
        wire.reply({"data": [{"id": "pi_1"}], "has_more": True})
        wire.reply({"data": [{"id": "pi_2"}], "has_more": False})
        await client().list_payment_intents(customer_id="cus_9", limit=10)
        assert all("customer=cus_9" in str(call.params) for call in wire.calls)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_only_succeeded_means_the_money_moved(self) -> None:
        """A receipt filed on any other status is a receipt for nothing."""
        assert StripePaymentIntent.from_api({"id": "pi", "status": "succeeded"}).settled
        for status in ("processing", "requires_payment_method", "canceled"):
            assert not StripePaymentIntent.from_api({"id": "pi", "status": status}).settled

    def test_amounts_keep_their_unit_in_the_name(self) -> None:
        """`amount_cents` is the whole warning: ¥1000 is ¥1000, not ¥10."""
        intent = StripePaymentIntent.from_api({"id": "pi", "amount": 1000})
        assert intent.amount_cents == 1000

    def test_an_expanded_customer_object_does_not_become_an_id(self) -> None:
        """Stripe returns either a string id or the whole object under
        ``customer``; only the string is an id."""
        expanded = StripePaymentIntent.from_api(
            {"id": "pi", "customer": {"id": "cus_1", "object": "customer"}}
        )
        assert expanded.customer_id == ""


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


class TestRefunds:
    @pytest.mark.asyncio()
    async def test_it_refuses_both_targets(self, wire: Any) -> None:
        with pytest.raises(StripeInvalidRequest, match="exactly one"):
            await client().create_refund(
                idempotency_key="k", payment_intent_id="pi_1", charge_id="ch_1"
            )

    @pytest.mark.asyncio()
    async def test_it_refuses_neither_target(self, wire: Any) -> None:
        with pytest.raises(StripeInvalidRequest, match="exactly one"):
            await client().create_refund(idempotency_key="k")

    @pytest.mark.asyncio()
    async def test_a_zero_amount_means_the_full_refund(self, wire: Any) -> None:
        """Sent as an omission, not as ``amount=0``, which refunds nothing."""
        wire.reply({"id": "re_1", "amount": 500})
        await client().create_refund(idempotency_key="k", payment_intent_id="pi_1")
        assert "amount" not in wire.last.form


# ---------------------------------------------------------------------------
# The webhook source
# ---------------------------------------------------------------------------

SAMPLE_EVENT = json.dumps(
    {
        "id": "evt_1",
        "object": "event",
        "type": "payment_intent.succeeded",
        "created": 1699000000,
        "livemode": False,
        "data": {"object": {"id": "pi_1", "amount": 4200, "status": "succeeded"}},
    }
).encode()

SECRET = "whsec_test"


def sign(body: bytes, *, secret: str = SECRET, timestamp: str = "") -> dict[str, str]:
    """The headers a genuine Stripe delivery would carry."""
    stamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), stamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {"stripe-signature": f"t={stamp},v1={digest}"}


class TestStripeSource:
    @pytest.mark.asyncio()
    async def test_it_conforms(self) -> None:
        """The kit checks what an author would not think to: that verification
        is over the *bytes*, that a tampered body is rejected, that the
        delivery id is stable, and that expansion is pure.

        Awaited, obviously — but worth saying: an un-awaited coroutine here is
        a test that passes having run nothing, which is the failure this whole
        area exists to catch.
        """
        await verify_event_source(
            StripeSource(signing_secret=SECRET),
            sign=sign,
            sample=SAMPLE_EVENT,
            expected_types=["stripe.payment_intent.succeeded"],
        )

    def test_a_genuine_delivery_verifies(self) -> None:
        StripeSource(signing_secret=SECRET).verify(sign(SAMPLE_EVENT), SAMPLE_EVENT)

    def test_a_tampered_body_is_rejected(self) -> None:
        headers = sign(SAMPLE_EVENT)
        with pytest.raises(VerificationFailed, match="mismatch"):
            StripeSource(signing_secret=SECRET).verify(headers, SAMPLE_EVENT + b" ")

    def test_any_listed_signature_may_match(self) -> None:
        """Stripe sends one per active secret while a rotation is in progress.

        Checking only the first fails every delivery for the length of the
        rollover — which is exactly when nobody wants to debug a webhook.
        """
        stamp = str(int(time.time()))
        genuine = sign(SAMPLE_EVENT, timestamp=stamp)["stripe-signature"]
        real = genuine.split("v1=")[1]
        header = f"t={stamp},v1=deadbeef,v1={real}"
        StripeSource(signing_secret=SECRET).verify(
            {"stripe-signature": header}, SAMPLE_EVENT
        )

    def test_an_old_delivery_is_refused_as_a_replay(self) -> None:
        old = str(int(time.time()) - 3600)
        with pytest.raises(VerificationFailed, match="replay"):
            StripeSource(signing_secret=SECRET).verify(
                sign(SAMPLE_EVENT, timestamp=old), SAMPLE_EVENT
            )

    def test_no_secret_refuses_rather_than_accepting(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        with pytest.raises(VerificationFailed, match="STRIPE_WEBHOOK_SECRET"):
            StripeSource().verify(sign(SAMPLE_EVENT), SAMPLE_EVENT)

    def test_verification_can_be_delegated_to_a_gateway(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        StripeSource(require_signature=False).verify({}, SAMPLE_EVENT)

    def test_there_is_no_handshake(self) -> None:
        """Stripe verifies an endpoint by sending it a real event."""
        assert StripeSource(signing_secret=SECRET).challenge({}, SAMPLE_EVENT) is None

    def test_the_delivery_id_is_the_event_id(self) -> None:
        source = StripeSource(signing_secret=SECRET)
        assert source.delivery_id({}, json.loads(SAMPLE_EVENT)) == "evt_1"

    def test_a_non_event_id_is_not_accepted_as_one(self) -> None:
        """``id`` is on every Stripe object; only ``evt_…`` names a delivery."""
        source = StripeSource(signing_secret=SECRET)
        assert source.delivery_id({}, {"id": "pi_1"}) is None

    @pytest.mark.asyncio()
    async def test_expansion_carries_livemode_out_of_the_envelope(self) -> None:
        """A test-mode delivery reaching a production workflow is otherwise
        invisible: it is on the envelope, not on the object."""
        source = StripeSource(signing_secret=SECRET)
        events = await source.expand(json.loads(SAMPLE_EVENT), None)  # type: ignore[arg-type]
        assert len(events) == 1
        assert events[0].type == "stripe.payment_intent.succeeded"
        assert events[0].payload["livemode"] is False
        assert events[0].payload["event_id"] == "evt_1"
        assert events[0].key == "pi_1"

    def test_the_header_parser_returns_every_signature(self) -> None:
        stamp, signatures = parse_signature_header("t=123,v1=aaa,v1=bbb,v0=old")
        assert stamp == "123"
        assert signatures == ["aaa", "bbb"]


# ---------------------------------------------------------------------------
# The manifest, checked against the client it describes
# ---------------------------------------------------------------------------


class TestManifest:
    def test_effects_are_consistent_with_the_client(self) -> None:
        """Declared classes, idempotency, and HTTP verbs must agree."""
        from pathlib import Path

        from loom.toolsets.stripe import tools

        source = Path(
            "src/loom/toolsets/stripe/client.py"
        ).read_text(encoding="utf-8")
        verify_effect_profile(
            STRIPE_MANIFEST, tools_module=tools, client_source=source
        )

    def test_the_resolver_is_declared(self) -> None:
        """Every write takes a cus_… id, and an email matches nothing."""
        assert STRIPE_MANIFEST.resolvers()

    def test_every_operation_declares_an_effect(self) -> None:
        for op in STRIPE_MANIFEST.all_operations():
            assert "effect" in op.model_fields_set, f"{op.id} did not declare one"
