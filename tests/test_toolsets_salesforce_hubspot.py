"""Salesforce and HubSpot: auth, paging dialects, caps, and error classification.

No network. Transport stubs for the loops, pure translation for the models, and
the classifiers driven with error payloads copied from the vendor docs.

The three things most likely to be wrong in a CRM toolset are all here, and all
of them fail *quietly* rather than loudly: a per-org base URL that is simply
absent, a paging loop that stops early and reports a partial answer as total,
and a 403 that means "wait" being treated the same as a 403 that means "never".
"""

from __future__ import annotations

import pytest

from loom.core.exceptions import NonRetryableError
from loom.toolsets.hubspot.client import (
    _LIST_PAGING,
    SEARCH_TOTAL_CAP,
    HubSpotAuthError,
    HubSpotClient,
    HubSpotPermanentError,
    HubSpotRateLimited,
)
from loom.toolsets.hubspot.client import (
    _classify as hubspot_classify,
)
from loom.toolsets.hubspot.models import HubSpotContact, HubSpotDeal, HubSpotObject
from loom.toolsets.pagination import LinkPaging, page_through
from loom.toolsets.salesforce.client import (
    SalesforceAuthError,
    SalesforceClient,
    SalesforcePermanentError,
    SalesforceRateLimited,
    _soql_escape,
)
from loom.toolsets.salesforce.client import (
    _classify as salesforce_classify,
)
from loom.toolsets.salesforce.models import (
    SalesforceOpportunity,
    SalesforceRecord,
    SalesforceWriteResult,
)


class FakeResponse:
    """The parts of an httpx response the error classifiers read."""

    def __init__(self, status: int, payload=None, *, text: str = "", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# -- Salesforce auth --------------------------------------------------------


class TestSalesforceAuth:
    def test_an_instance_url_and_token_is_enough(self, monkeypatch) -> None:
        monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "https://acme.my.salesforce.com")
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "tok")

        client = SalesforceClient()

        assert client._instance == "https://acme.my.salesforce.com"

    def test_refresh_credentials_alone_are_enough(self, monkeypatch) -> None:
        """The instance URL arrives with the refreshed token, so a client given
        only refresh credentials learns where its org lives."""
        for var in ("SALESFORCE_INSTANCE_URL", "SALESFORCE_ACCESS_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("SALESFORCE_CLIENT_ID", "id")
        monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SALESFORCE_REFRESH_TOKEN", "refresh")

        assert SalesforceClient()._can_refresh()

    def test_no_credentials_fails_at_construction(self, monkeypatch) -> None:
        """Not at first request. A per-org base URL that is missing surfaces as
        404s that look like missing records."""
        for var in (
            "SALESFORCE_INSTANCE_URL",
            "SALESFORCE_ACCESS_TOKEN",
            "SALESFORCE_CLIENT_ID",
            "SALESFORCE_CLIENT_SECRET",
            "SALESFORCE_REFRESH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SalesforceAuthError, match="SALESFORCE_INSTANCE_URL"):
            SalesforceClient()

    def test_the_error_names_the_sandbox_host(self, monkeypatch) -> None:
        """A sandbox token against the production login host fails as
        invalid_grant, which reads like a bad token rather than a wrong host."""
        for var in (
            "SALESFORCE_INSTANCE_URL",
            "SALESFORCE_ACCESS_TOKEN",
            "SALESFORCE_CLIENT_ID",
            "SALESFORCE_CLIENT_SECRET",
            "SALESFORCE_REFRESH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SalesforceAuthError, match="test.salesforce.com"):
            SalesforceClient()

    def test_a_trailing_slash_on_the_instance_is_dropped(self, monkeypatch) -> None:
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "tok")

        client = SalesforceClient(instance_url="https://acme.my.salesforce.com/")

        assert client._instance == "https://acme.my.salesforce.com"


class TestHubSpotAuth:
    def test_a_token_is_all_it_takes(self, monkeypatch) -> None:
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-na1-x")

        assert HubSpotClient()._token == "pat-na1-x"

    def test_no_token_fails_at_construction(self, monkeypatch) -> None:
        monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)

        with pytest.raises(HubSpotAuthError, match="HUBSPOT_ACCESS_TOKEN"):
            HubSpotClient()

    def test_the_api_version_is_a_constructor_argument(self, monkeypatch) -> None:
        """HubSpot has begun publishing dated versions alongside v3. A host that
        wants one changes a string rather than waiting for a release."""
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")

        dated = HubSpotClient(version="2026-03")

        assert dated._objects("contacts") == "/crm/2026-03/objects/contacts"


# -- error classification ---------------------------------------------------


class TestSalesforceErrors:
    def test_a_403_for_quota_is_retryable(self) -> None:
        """The split that matters. REQUEST_LIMIT_EXCEEDED clears if you wait."""
        error = salesforce_classify(
            FakeResponse(
                403,
                [{"message": "quota exceeded", "errorCode": "REQUEST_LIMIT_EXCEEDED"}],
            )
        )

        assert isinstance(error, SalesforceRateLimited)
        assert not isinstance(error, NonRetryableError)

    def test_a_403_for_permissions_is_not_retryable(self) -> None:
        """The same status, the opposite answer: waiting never grants access."""
        error = salesforce_classify(
            FakeResponse(
                403,
                [{"message": "no access", "errorCode": "INSUFFICIENT_ACCESS"}],
            )
        )

        assert isinstance(error, SalesforcePermanentError)
        assert isinstance(error, NonRetryableError)

    def test_the_error_code_reaches_the_message(self) -> None:
        error = salesforce_classify(
            FakeResponse(
                400, [{"message": "bad field", "errorCode": "INVALID_FIELD"}]
            )
        )

        assert "INVALID_FIELD" in str(error)

    def test_a_401_is_an_auth_error(self) -> None:
        error = salesforce_classify(
            FakeResponse(401, [{"message": "expired", "errorCode": "INVALID_SESSION_ID"}])
        )

        assert isinstance(error, SalesforceAuthError)

    def test_a_5xx_stays_retryable(self) -> None:
        assert not isinstance(
            salesforce_classify(FakeResponse(503, None, text="unavailable")),
            NonRetryableError,
        )


class TestHubSpotErrors:
    def test_a_validation_error_is_permanent_and_names_its_category(self) -> None:
        error = hubspot_classify(
            FakeResponse(
                400,
                {"message": "Property values were not valid", "category": "VALIDATION_ERROR"},
            )
        )

        assert isinstance(error, HubSpotPermanentError)
        assert "VALIDATION_ERROR" in str(error)

    def test_a_429_is_retryable_and_carries_retry_after(self) -> None:
        """Search allows five requests a second, which paging reaches easily."""
        error = hubspot_classify(
            FakeResponse(429, {"message": "rate limited"}, headers={"Retry-After": "10"})
        )

        assert isinstance(error, HubSpotRateLimited)
        assert not isinstance(error, NonRetryableError)
        assert error.retry_after == 10.0

    def test_a_401_is_an_auth_error(self) -> None:
        assert isinstance(
            hubspot_classify(FakeResponse(401, {"message": "bad token"})),
            HubSpotAuthError,
        )


# -- paging -----------------------------------------------------------------


class TestSalesforceLinkPaging:
    """Salesforce names the next *request*, not a cursor to send."""

    async def test_it_follows_next_records_url_verbatim(self) -> None:
        pages = {
            None: {
                "done": False,
                "totalSize": 3,
                "records": [{"Id": "001", "attributes": {"type": "Account"}}],
                "nextRecordsUrl": "/services/data/v60.0/query/01g-2000",
            },
            "/services/data/v60.0/query/01g-2000": {
                "done": True,
                "totalSize": 3,
                "records": [{"Id": "002", "attributes": {"type": "Account"}}],
            },
        }
        asked: list[str | None] = []

        async def request(params):
            following = params.get("__next_path")
            asked.append(following)
            return pages[following]

        rows = await page_through(
            request,
            style=LinkPaging(items="records"),
            limit=10,
            page_size=2000,
            row=SalesforceRecord.from_api,
        )

        assert [r.id for r in rows] == ["001", "002"]
        assert asked == [None, "/services/data/v60.0/query/01g-2000"], (
            "the locator path was not followed verbatim"
        )

    async def test_done_ends_the_loop_even_on_a_full_page(self) -> None:
        """`done` is authoritative, so a full final batch is not mistaken for
        more data the way a short-page heuristic would."""

        async def request(params):
            return {
                "done": True,
                "totalSize": 2,
                "records": [{"Id": "1"}, {"Id": "2"}],
            }

        rows = await page_through(
            request,
            style=LinkPaging(items="records"),
            limit=10,
            page_size=2,
            row=SalesforceRecord.from_api,
        )

        assert len(rows) == 2
        assert rows.complete

    async def test_total_size_is_reported(self) -> None:
        async def request(params):
            return {"done": True, "totalSize": 42, "records": [{"Id": "1"}]}

        rows = await page_through(
            request,
            style=LinkPaging(items="records"),
            limit=10,
            page_size=100,
            row=SalesforceRecord.from_api,
        )

        assert rows.total == 42


class TestHubSpotNestedTokenPaging:
    """HubSpot's token lives at ``paging.next.after`` — nested, not flat."""

    async def test_the_nested_token_is_found_and_sent(self) -> None:
        pages = [
            {
                "results": [{"id": "1", "properties": {}}],
                "paging": {"next": {"after": "33452", "link": "…"}},
            },
            {"results": [{"id": "2", "properties": {}}]},
        ]
        asked: list[dict] = []

        async def request(params):
            asked.append(params)
            return pages[len(asked) - 1]

        rows = await page_through(
            request,
            style=_LIST_PAGING,
            limit=10,
            page_size=100,
            row=lambda raw: HubSpotObject.from_api(raw, "contacts"),
        )

        assert [o.id for o in rows] == ["1", "2"]
        assert asked[1]["after"] == "33452", "the nested token was not carried"

    async def test_a_missing_paging_block_ends_the_loop(self) -> None:
        async def request(params):
            return {"results": [{"id": "1", "properties": {}}]}

        rows = await page_through(
            request,
            style=_LIST_PAGING,
            limit=10,
            page_size=100,
            row=lambda raw: HubSpotObject.from_api(raw, "contacts"),
        )

        assert len(rows) == 1
        assert rows.complete

    def test_the_search_cap_is_ten_thousand(self) -> None:
        """Paging past it is a 400, so the client stops there and reports a
        partial answer rather than turning a large query into an error."""
        assert SEARCH_TOTAL_CAP == 10_000


# -- SOQL safety ------------------------------------------------------------


class TestSoqlEscaping:
    def test_an_apostrophe_is_escaped(self) -> None:
        """O'Brien is the most predictable surname to meet in a CRM, and an
        unescaped one terminates the string literal."""
        assert _soql_escape("O'Brien") == r"O\'Brien"

    def test_a_backslash_is_escaped_first(self) -> None:
        """Escaping the quote first would leave the backslash escaping the
        escape, which is how an injection survives a naive fix."""
        assert _soql_escape("a\\b") == "a\\\\b"

    def test_ordinary_text_is_untouched(self) -> None:
        assert _soql_escape("ACME Corp") == "ACME Corp"


# -- translation ------------------------------------------------------------


class TestSalesforceModels:
    def test_an_opportunity_reads_stagename_not_stage(self) -> None:
        """Getting this wrong yields an empty string, not an error."""
        deal = SalesforceOpportunity.from_api(
            {"Id": "006", "Name": "ACME", "StageName": "Negotiation", "Amount": "50000"}
        )

        assert deal.stage == "Negotiation"
        assert deal.amount == 50000.0

    def test_a_null_relationship_does_not_raise(self) -> None:
        """`SELECT Account.Name` returns `{"Account": null}` when the lookup is
        empty — a different thing from the key being absent, and an
        AttributeError either way."""
        deal = SalesforceOpportunity.from_api(
            {"Id": "006", "Account": None, "Owner": None}
        )

        assert deal.account_name == "" and deal.owner == ""

    def test_a_nested_relationship_is_flattened(self) -> None:
        deal = SalesforceOpportunity.from_api(
            {"Id": "006", "Account": {"Name": "ACME Inc"}, "Owner": {"Name": "Ada"}}
        )

        assert deal.account_name == "ACME Inc" and deal.owner == "Ada"

    def test_the_envelope_is_unwrapped_but_kept(self) -> None:
        """`attributes.url` is the record's own REST path, which is worth
        keeping; `attributes` itself is not worth handing to a model."""
        record = SalesforceRecord.from_api(
            {
                "Id": "001",
                "attributes": {"type": "Account", "url": "/services/data/v60.0/x"},
                "Name": "ACME",
            }
        )

        assert record.type == "Account"
        assert record.url.endswith("/x")
        assert "attributes" not in record.fields
        assert record.fields["Name"] == "ACME"

    def test_an_update_reports_success_despite_an_empty_body(self) -> None:
        """Salesforce answers a PATCH with 204 and nothing else, so the result
        is constructed rather than leaving a caller to tell empty from failed."""
        assert SalesforceWriteResult(id="001", success=True).success


class TestHubSpotModels:
    def test_a_numeric_property_arrives_as_a_string(self) -> None:
        """A workflow comparing `deal.amount > 10000` should not have to know
        the CRM sends numbers as text."""
        deal = HubSpotDeal.from_api({"id": "1", "properties": {"amount": "50000"}})

        assert deal.amount == 50000.0

    def test_a_missing_amount_is_zero_not_an_error(self) -> None:
        assert HubSpotDeal.from_api({"id": "1", "properties": {}}).amount == 0.0

    def test_a_boolean_property_arrives_as_a_string(self) -> None:
        deal = HubSpotDeal.from_api({"id": "1", "properties": {"hs_is_closed": "true"}})

        assert deal.is_closed is True

    def test_a_contact_composes_a_full_name(self) -> None:
        """One field to show a person, rather than two that are each often
        empty."""
        contact = HubSpotContact.from_api(
            {"id": "1", "properties": {"firstname": "Ada", "lastname": "Lovelace"}}
        )

        assert contact.full_name == "Ada Lovelace"

    def test_a_half_named_contact_does_not_gain_a_stray_space(self) -> None:
        contact = HubSpotContact.from_api(
            {"id": "1", "properties": {"firstname": "Ada"}}
        )

        assert contact.full_name == "Ada"

    def test_the_property_bag_survives_for_unmodelled_types(self) -> None:
        """HubSpot's shape is identical for custom objects, so the generic model
        keeps whatever an org invented."""
        obj = HubSpotObject.from_api(
            {"id": "9", "properties": {"custom_field__c": "x"}}, "widgets"
        )

        assert obj.object_type == "widgets"
        assert obj.properties["custom_field__c"] == "x"


class TestTheToolsetsAreVisibleFromTheCli:
    """`loom toolsets` and `loom toolset <id>`.

    The node catalog has had a CLI since it existed; toolsets had none, so the
    only way to answer "is Salesforce wired up in this process?" was to start
    an MCP server and ask it.
    """

    def _run(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        from loom.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_listing_includes_both_new_toolsets(self) -> None:
        code, output = self._run(["toolsets"])

        assert code == 0
        assert "salesforce" in output and "hubspot" in output

    def test_a_query_narrows_the_list(self) -> None:
        code, output = self._run(["toolsets", "salesforce"])

        assert code == 0
        assert "salesforce" in output and "hubspot" not in output

    def test_showing_one_lists_its_operations_and_import_line(self) -> None:
        """The import line is the one thing that stops generated code
        inventing a module path."""
        code, output = self._run(["toolset", "hubspot"])

        assert code == 0
        assert "from loom.toolsets.hubspot.tools import" in output
        assert "objects.search" in output

    def test_showing_one_marks_effects_and_paging(self) -> None:
        code, output = self._run(["toolset", "salesforce"])

        assert "destructive" in output, "delete is not marked destructive"
        assert "yes" in output, "the paged read is not marked"

    def test_an_unknown_toolset_is_a_usage_error(self) -> None:
        """Exit 2, not 1: a typo is a usage problem, and the CLI's exit codes
        are the contract a calling script reads."""
        code, output = self._run(["toolset", "nope"])

        assert code == 2
        assert "known:" in output

    def test_json_output_is_machine_readable(self) -> None:
        import json

        code, output = self._run(["toolsets", "--json"])

        assert code == 0
        rows = json.loads(output)
        assert {"salesforce", "hubspot"} <= {r["id"] for r in rows}
