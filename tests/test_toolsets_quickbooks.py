"""QuickBooks Online toolset — the traps this API sets, pinned.

Five of them, and four return something plausible rather than an error: a realm
id in every path, separate sandbox and production hosts, optimistic concurrency
through SyncToken, a 1-based query offset, and no idempotency key anywhere.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import loom.toolsets.quickbooks.client as qb_client
from loom.core.exceptions import NonRetryableError
from loom.testing.conformance import verify_effect_profile
from loom.toolsets.quickbooks.client import (
    PRODUCTION_URL,
    SANDBOX_URL,
    QuickBooksAuthError,
    QuickBooksClient,
    QuickBooksNotFound,
    QuickBooksStaleObject,
    QuickBooksThrottled,
    QuickBooksValidationError,
    _classify,
    escape_literal,
)
from loom.toolsets.quickbooks.manifest import QUICKBOOKS_MANIFEST
from loom.toolsets.quickbooks.models import QuickBooksCustomer, QuickBooksSalesReceipt

REALM = "9130350000000"


class Call:
    """One recorded request."""

    def __init__(self, method: str, url: str, headers: Any, params: Any, payload: Any):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self.params = dict(params or {})
        self.payload = payload


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
            json: Any = None,
        ) -> FakeResponse:
            calls.append(Call(method, url, headers, params, json))
            status, body = queue.pop(0) if queue else (200, {})
            return FakeResponse(status, body)

        async def post(
            self, url: str, headers: Any = None, data: Any = None
        ) -> FakeResponse:
            calls.append(Call("POST", url, headers, None, data))
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


def client(**kw: Any) -> QuickBooksClient:
    return QuickBooksClient(realm_id=REALM, access_token="tok", **kw)


# ---------------------------------------------------------------------------
# Construction — no constant base URL, and two hosts
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_a_missing_realm_fails_at_construction(self, monkeypatch: Any) -> None:
        """The company id is part of every path, so there is no URL without it."""
        monkeypatch.delenv("QUICKBOOKS_REALM_ID", raising=False)
        with pytest.raises(QuickBooksAuthError, match="QUICKBOOKS_REALM_ID"):
            QuickBooksClient(access_token="tok")

    def test_no_credentials_at_all_fails_at_construction(self, monkeypatch: Any) -> None:
        for name in (
            "QUICKBOOKS_CLIENT_ID",
            "QUICKBOOKS_CLIENT_SECRET",
            "QUICKBOOKS_REFRESH_TOKEN",
            "QUICKBOOKS_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(QuickBooksAuthError, match="QUICKBOOKS_CLIENT_ID"):
            QuickBooksClient(realm_id=REALM)

    def test_sandbox_and_production_are_different_hosts(self) -> None:
        """A sandbox token against production authenticates and finds nothing.

        That reads as an empty company file rather than as the wrong host.
        """
        assert client().base_url == PRODUCTION_URL
        assert client(environment="sandbox").base_url == SANDBOX_URL

    def test_the_realm_is_in_the_path(self) -> None:
        assert client().company_path == f"/v3/company/{REALM}"

    @pytest.mark.asyncio()
    async def test_the_minor_version_is_pinned(self, wire: Any) -> None:
        wire.reply({"Customer": {"Id": "1"}})
        await client().get_customer("1")
        assert wire.last.params["minorversion"]


# ---------------------------------------------------------------------------
# SyncToken — optimistic concurrency
# ---------------------------------------------------------------------------


class TestSyncToken:
    def test_a_read_carries_it(self) -> None:
        """Fetching it separately at write time is a round trip that can still
        lose the race, so it travels on the model."""
        customer = QuickBooksCustomer.from_api(
            {"Id": "1", "SyncToken": "7", "DisplayName": "Ada"}
        )
        assert customer.sync_token == "7"

    @pytest.mark.asyncio()
    async def test_an_update_sends_it_back(self, wire: Any) -> None:
        wire.reply({"Customer": {"Id": "1", "SyncToken": "8"}})
        await client().update_customer("1", "7", {"CompanyName": "Acme"})
        assert wire.last.payload["SyncToken"] == "7"

    @pytest.mark.asyncio()
    async def test_an_update_is_sparse(self, wire: Any) -> None:
        """Without ``sparse``, QuickBooks treats the payload as the whole
        record and blanks every field not sent — a data-loss bug that
        returns 200."""
        wire.reply({"Customer": {"Id": "1"}})
        await client().update_customer("1", "7", {"CompanyName": "Acme"})
        assert wire.last.payload["sparse"] is True

    def test_a_stale_token_is_not_retryable(self) -> None:
        """Retrying with the same token fails identically; the fix is to
        re-read and decide whether the change still applies."""
        raised = _classify(400, {"Fault": {"Error": [{"code": "5010"}]}})
        assert isinstance(raised, QuickBooksStaleObject)
        assert isinstance(raised, NonRetryableError)


# ---------------------------------------------------------------------------
# Queries — 1-based, and literals must be escaped
# ---------------------------------------------------------------------------


class TestQueries:
    def test_apostrophes_are_escaped(self) -> None:
        """``O'Brien`` terminates the literal, and the resulting query is
        either a syntax error or a valid query matching something else."""
        assert escape_literal("O'Brien") == "O\\'Brien"

    def test_backslashes_are_escaped_first(self) -> None:
        assert escape_literal("a\\b") == "a\\\\b"

    @pytest.mark.asyncio()
    async def test_the_first_page_starts_at_one_not_zero(self, wire: Any) -> None:
        """QuickBooks counts from 1. Sending 0 returns the first page again,
        which is why this drives ``collect`` rather than ``OffsetPaging``."""
        wire.reply({"QueryResponse": {"Customer": []}})
        await client().query("Customer", limit=10)
        assert "STARTPOSITION 1" in wire.last.params["query"]

    @pytest.mark.asyncio()
    async def test_it_follows_pages(self, wire: Any, monkeypatch: Any) -> None:
        """A second request only happens above the page cap.

        The client asks for ``min(limit, MAXRESULTS)`` in one go, so wanting 10
        rows is one request — paging is what happens when a caller wants more
        than 1000. Lowering the cap is how that path is exercised without a
        thousand-row fixture.
        """
        monkeypatch.setattr(qb_client, "QUICKBOOKS_MAX_PAGE", 2)
        wire.reply({"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}]}})
        wire.reply({"QueryResponse": {"Customer": [{"Id": "3"}]}})
        found = await client().query("Customer", limit=10)
        assert [row["Id"] for row in found] == ["1", "2", "3"]
        assert "STARTPOSITION 3" in wire.calls[1].params["query"]
        assert found.complete is True

    @pytest.mark.asyncio()
    async def test_a_full_last_page_is_reported_incomplete(self, wire: Any) -> None:
        """QuickBooks reports no total, so a short page is the only end signal.

        A full page that is genuinely the last one is indistinguishable from
        more data, and claiming completeness would be claiming what cannot be
        verified.
        """
        wire.reply({"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}]}})
        found = await client().query("Customer", limit=2)
        assert found.complete is False

    @pytest.mark.asyncio()
    async def test_a_lookup_escapes_the_email(self, wire: Any) -> None:
        wire.reply({"QueryResponse": {"Customer": []}})
        await client().find_customer_by_email("o'brien@example.com")
        assert "o\\'brien@example.com" in wire.last.params["query"]


class TestTheEmailResolverFallsBack:
    """QuickBooks marks each attribute filterable or not, and a WHERE on one
    that is not raises rather than being ignored. Whether PrimaryEmailAddr is
    filterable could not be confirmed from Intuit's own reference, so both
    paths are implemented and both are tested."""

    @pytest.mark.asyncio()
    async def test_the_direct_query_is_tried_first(self, wire: Any) -> None:
        wire.reply(
            {"QueryResponse": {"Customer": [{"Id": "1", "DisplayName": "Ada"}]}}
        )
        found = await client().find_customer_by_email("ada@example.com")
        assert found is not None and found.id == "1"
        assert len(wire.calls) == 1, "a working filter must cost one request"

    @pytest.mark.asyncio()
    async def test_a_refused_filter_falls_back_to_a_scan(self, wire: Any) -> None:
        wire.reply({"Fault": {"Error": [{"code": "4000"}]}}, status=400)
        wire.reply(
            {
                "QueryResponse": {
                    "Customer": [
                        {"Id": "1", "PrimaryEmailAddr": {"Address": "other@x.test"}},
                        {"Id": "2", "PrimaryEmailAddr": {"Address": "Ada@Example.com"}},
                    ]
                }
            }
        )
        found = await client().find_customer_by_email("ada@example.com")
        assert found is not None and found.id == "2", "matched case-insensitively"

    @pytest.mark.asyncio()
    async def test_a_truncated_scan_raises_rather_than_saying_not_found(
        self, wire: Any
    ) -> None:
        """"Not found" is a fact a caller acts on — it creates the customer —
        and it is only a fact if the whole set was searched."""
        monkey = client()
        wire.reply({"Fault": {"Error": [{"code": "4000"}]}}, status=400)
        # A full page, so `collect` reports complete=False.
        wire.reply(
            {"QueryResponse": {"Customer": [{"Id": str(n)} for n in range(2)]}}
        )
        with pytest.raises(QuickBooksValidationError, match="duplicate customer"):
            await monkey.find_customer_by_email("nobody@example.com", scan_limit=2)

    @pytest.mark.asyncio()
    async def test_a_complete_scan_with_no_match_is_none(self, wire: Any) -> None:
        wire.reply({"Fault": {"Error": [{"code": "4000"}]}}, status=400)
        wire.reply({"QueryResponse": {"Customer": [{"Id": "1"}]}})
        assert await client().find_customer_by_email("nobody@example.com") is None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_a_validation_fault_is_not_retryable(self) -> None:
        raised = _classify(
            400, {"Fault": {"Error": [{"code": "6240", "Message": "Duplicate Name"}]}}
        )
        assert isinstance(raised, QuickBooksValidationError)
        assert isinstance(raised, NonRetryableError)

    def test_a_missing_record_is_named(self) -> None:
        raised = _classify(400, {"Fault": {"Error": [{"code": "610"}]}})
        assert isinstance(raised, QuickBooksNotFound)

    def test_throttling_is_retryable(self) -> None:
        raised = _classify(429, {})
        assert isinstance(raised, QuickBooksThrottled)
        assert not isinstance(raised, NonRetryableError)

    def test_a_server_error_is_retryable(self) -> None:
        assert not isinstance(_classify(500, {}), NonRetryableError)

    def test_the_detail_is_carried_into_the_message(self) -> None:
        raised = _classify(
            400,
            {
                "Fault": {
                    "Error": [
                        {
                            "code": "6240",
                            "Message": "Duplicate Name Exists Error",
                            "Detail": "The name supplied already exists.",
                        }
                    ]
                }
            },
        )
        assert "already exists" in str(raised)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestRefresh:
    @pytest.mark.asyncio()
    async def test_a_401_refreshes_once_and_retries(self, wire: Any) -> None:
        target = QuickBooksClient(
            realm_id=REALM,
            client_id="cid",
            client_secret="secret",
            refresh_token="rt",
            access_token="stale",
        )
        wire.reply({}, status=401)
        wire.reply({"access_token": "fresh", "refresh_token": "rt2"})
        wire.reply({"Customer": {"Id": "1"}})

        await target.get_customer("1")

        assert len(wire.calls) == 3
        assert wire.calls[2].headers["Authorization"] == "Bearer fresh"

    @pytest.mark.asyncio()
    async def test_a_second_401_is_not_retried_again(self, wire: Any) -> None:
        """Twice would turn a revoked grant into a loop against the token
        endpoint."""
        target = QuickBooksClient(
            realm_id=REALM,
            client_id="cid",
            client_secret="secret",
            refresh_token="rt",
            access_token="stale",
        )
        wire.reply({}, status=401)
        wire.reply({"access_token": "fresh"})
        wire.reply({}, status=401)

        with pytest.raises(QuickBooksAuthError):
            await target.get_customer("1")
        assert len(wire.calls) == 3

    @pytest.mark.asyncio()
    async def test_a_rotated_refresh_token_is_kept(self, wire: Any) -> None:
        """Intuit rotates it on every exchange. Keeping the old one makes the
        *next* refresh fail, hours later, looking unrelated."""
        target = QuickBooksClient(
            realm_id=REALM,
            client_id="cid",
            client_secret="secret",
            refresh_token="rt-old",
        )
        wire.reply({"access_token": "fresh", "refresh_token": "rt-new"})
        wire.reply({"Customer": {"Id": "1"}})
        await target.get_customer("1")
        assert target._refresh_token == "rt-new"

    @pytest.mark.asyncio()
    async def test_a_refused_refresh_names_the_100_day_rule(self, wire: Any) -> None:
        target = QuickBooksClient(
            realm_id=REALM, client_id="c", client_secret="s", refresh_token="rt"
        )
        wire.reply({}, status=400)
        with pytest.raises(QuickBooksAuthError, match="100 days"):
            await target.get_customer("1")


# ---------------------------------------------------------------------------
# Sales receipts — decimal amounts, and the note that stands in for a key
# ---------------------------------------------------------------------------


class TestSalesReceipts:
    @pytest.mark.asyncio()
    async def test_the_amount_is_decimal_not_minor_units(self, wire: Any) -> None:
        """Stripe says 4200; QuickBooks says 42.00. The bridge converts once."""
        wire.reply({"SalesReceipt": {"Id": "1", "TotalAmt": "42.00"}})
        await client().create_sales_receipt(customer_id="9", amount=42.00)
        assert wire.last.payload["Line"][0]["Amount"] == 42.0

    @pytest.mark.asyncio()
    async def test_an_external_id_goes_in_the_private_note(self, wire: Any) -> None:
        """The idempotency key QuickBooks does not have."""
        wire.reply({"SalesReceipt": {"Id": "1"}})
        await client().create_sales_receipt(
            customer_id="9", amount=1.0, private_note="stripe:pi_1"
        )
        assert wire.last.payload["PrivateNote"] == "stripe:pi_1"

    @pytest.mark.asyncio()
    async def test_the_lookup_matches_that_note(self, wire: Any) -> None:
        wire.reply({"QueryResponse": {"SalesReceipt": [{"Id": "1"}]}})
        found = await client().find_sales_receipts(private_note="stripe:pi_1", limit=1)
        assert "stripe:pi_1" in wire.last.params["query"]
        assert [r.id for r in found] == ["1"]

    def test_the_model_reads_a_nested_customer_ref(self) -> None:
        receipt = QuickBooksSalesReceipt.from_api(
            {"Id": "1", "CustomerRef": {"value": "9"}, "TotalAmt": "42.00"}
        )
        assert receipt.customer_id == "9"
        assert receipt.total == 42.0


# ---------------------------------------------------------------------------
# The manifest, checked against the client it describes
# ---------------------------------------------------------------------------


class TestManifest:
    def test_effects_are_consistent_with_the_client(self) -> None:
        from pathlib import Path

        from loom.toolsets.quickbooks import tools

        source = Path("src/loom/toolsets/quickbooks/client.py").read_text(
            encoding="utf-8"
        )
        verify_effect_profile(
            QUICKBOOKS_MANIFEST, tools_module=tools, client_source=source
        )

    def test_the_creates_are_declared_non_idempotent(self) -> None:
        """There is no idempotency key, and the declaration has to say so —
        it is what `idempotent` on the OperationSpec means."""
        by_id = {op.id: op for op in QUICKBOOKS_MANIFEST.all_operations()}
        assert by_id["customers.create"].idempotent is False
        assert by_id["sales_receipts.create"].idempotent is False

    def test_the_resolvers_are_declared(self) -> None:
        """A customer by email, and an item by name — both take ids on write."""
        assert set(QUICKBOOKS_MANIFEST.resolvers()) >= {"customer", "item"}

    def test_every_operation_declares_an_effect(self) -> None:
        for op in QUICKBOOKS_MANIFEST.all_operations():
            assert "effect" in op.model_fields_set, f"{op.id} did not declare one"
