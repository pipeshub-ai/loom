"""Airtable toolset — the traps this API sets, pinned.

Four of them, and three return something plausible rather than an error: a
response keyed by a renameable field name, an empty field omitted rather than
nulled, a ten-record write cap with no transaction across batches, and a rate
limit whose penalty is thirty seconds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from loom.core.exceptions import NonRetryableError
from loom.testing.conformance import verify_effect_profile
from loom.toolsets.airtable.client import (
    AIRTABLE_MAX_BATCH,
    AirtableAuthError,
    AirtableClient,
    AirtableInvalidRequest,
    AirtableNotFound,
    AirtableRateLimited,
    _classify,
    escape_formula,
)
from loom.toolsets.airtable.manifest import AIRTABLE_MANIFEST
from loom.toolsets.airtable.models import AirtableRecord

TOKEN = "patTESTTOKEN"
BASE = "appTESTBASE"


class Call:
    def __init__(self, method: str, url: str, params: Any, payload: Any):
        self.method = method
        self.url = url
        self.params = dict(params or {})
        self.payload = payload


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
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
            calls.append(Call(method, url, params, json))
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


def client() -> AirtableClient:
    return AirtableClient(token=TOKEN, base_id=BASE)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_a_missing_token_fails_at_construction(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
        with pytest.raises(AirtableAuthError, match="AIRTABLE_TOKEN"):
            AirtableClient()

    def test_the_message_names_the_retired_api_key(self, monkeypatch: Any) -> None:
        """Somebody arriving with an old ``key…`` needs to be told, not left
        to read a 401."""
        monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
        with pytest.raises(AirtableAuthError, match="API keys were retired"):
            AirtableClient()

    @pytest.mark.asyncio()
    async def test_a_missing_base_fails_when_it_is_needed(
        self, monkeypatch: Any, wire: Any
    ) -> None:
        monkeypatch.delenv("AIRTABLE_BASE_ID", raising=False)
        with pytest.raises(AirtableAuthError, match="base id"):
            await AirtableClient(token=TOKEN).list_records("Leads")

    @pytest.mark.asyncio()
    async def test_a_table_name_with_a_space_is_encoded(self, wire: Any) -> None:
        """An unencoded space is a different path, and a 404 that reads as a
        missing table."""
        wire.reply({"records": []})
        await client().list_records("Sales Leads")
        assert "Sales%20Leads" in wire.last.url


# ---------------------------------------------------------------------------
# Field names — the trap that returns None rather than an error
# ---------------------------------------------------------------------------


class TestFieldNames:
    def test_an_absent_field_reads_as_empty(self) -> None:
        """Airtable omits an empty field rather than nulling it, so a missing
        key means "empty" — and "no such column" looks identical."""
        row = AirtableRecord.from_api({"id": "rec1", "fields": {"Name": "Ada"}})
        assert row.get("Name") == "Ada"
        assert row.get("Status") is None
        assert row.get("Status", "unset") == "unset"

    @pytest.mark.asyncio()
    async def test_field_ids_can_be_asked_for_instead(self, wire: Any) -> None:
        """The stable key, for a workflow that must survive a rename."""
        wire.reply({"records": []})
        await client().list_records("Leads", return_field_ids=True)
        assert wire.last.params["returnFieldsByFieldId"] == "true"

    @pytest.mark.asyncio()
    async def test_the_field_list_carries_both_halves(self, wire: Any) -> None:
        wire.reply(
            {
                "tables": [
                    {
                        "id": "tbl1",
                        "name": "Leads",
                        "fields": [{"id": "fld1", "name": "Email", "type": "email"}],
                    }
                ]
            }
        )
        fields = await client().list_fields("Leads")
        assert [(f.id, f.name) for f in fields] == [("fld1", "Email")]

    @pytest.mark.asyncio()
    async def test_an_unknown_table_names_the_rename(self, wire: Any) -> None:
        wire.reply({"tables": [{"id": "tbl1", "name": "Other", "fields": []}]})
        with pytest.raises(AirtableNotFound, match="renamed"):
            await client().list_fields("Leads")


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------


class TestFormulas:
    def test_apostrophes_are_escaped(self) -> None:
        """``O'Brien`` terminates the literal, and the formula then either
        errors or evaluates something that matches nothing."""
        assert escape_formula("O'Brien") == "O\\'Brien"

    @pytest.mark.asyncio()
    async def test_a_lookup_builds_a_braced_field_reference(self, wire: Any) -> None:
        wire.reply({"records": []})
        await client().find_records("Leads", "Email", "o'brien@example.com")
        assert (
            wire.last.params["filterByFormula"]
            == "{Email} = 'o\\'brien@example.com'"
        )


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


class TestPaging:
    @pytest.mark.asyncio()
    async def test_it_follows_the_offset_token(self, wire: Any) -> None:
        wire.reply({"records": [{"id": "rec1"}], "offset": "itr123/rec1"})
        wire.reply({"records": [{"id": "rec2"}]})
        found = await client().list_records("Leads", limit=10)
        assert [r.id for r in found] == ["rec1", "rec2"]
        assert wire.calls[1].params["offset"] == "itr123/rec1"
        assert found.complete is True

    @pytest.mark.asyncio()
    async def test_no_offset_ends_the_walk(self, wire: Any) -> None:
        wire.reply({"records": [{"id": "rec1"}]})
        found = await client().list_records("Leads", limit=10)
        assert len(wire.calls) == 1
        assert found.complete is True

    @pytest.mark.asyncio()
    async def test_coverage_survives_the_mapping(self, wire: Any) -> None:
        wire.reply({"records": [{"id": "rec1"}], "offset": "more"})
        found = await client().list_records("Leads", limit=1)
        assert found.complete is False


# ---------------------------------------------------------------------------
# Batching — the cap is Airtable's, and there is no transaction
# ---------------------------------------------------------------------------


class TestBatching:
    @pytest.mark.asyncio()
    async def test_creates_are_split_at_ten(self, wire: Any) -> None:
        wire.reply({"records": [{"id": f"rec{n}"} for n in range(10)]})
        wire.reply({"records": [{"id": "rec10"}, {"id": "rec11"}]})

        rows = [{"Name": f"n{n}"} for n in range(12)]
        created = await client().create_records("Leads", rows)

        assert len(wire.calls) == 2
        assert len(wire.calls[0].payload["records"]) == AIRTABLE_MAX_BATCH
        assert len(wire.calls[1].payload["records"]) == 2
        assert len(created) == 12

    @pytest.mark.asyncio()
    async def test_a_failing_second_batch_leaves_the_first_written(
        self, wire: Any
    ) -> None:
        """Airtable has no transaction across requests, and pretending
        otherwise would be worse than saying so."""
        wire.reply({"records": [{"id": f"rec{n}"} for n in range(10)]})
        wire.reply({"error": {"type": "INVALID_REQUEST"}}, status=422)

        rows = [{"Name": f"n{n}"} for n in range(12)]
        with pytest.raises(AirtableInvalidRequest):
            await client().create_records("Leads", rows)
        assert len(wire.calls) == 2

    @pytest.mark.asyncio()
    async def test_an_update_is_a_patch_not_a_replace(self, wire: Any) -> None:
        """Airtable's PUT clears every field not sent — data loss that returns
        200 — so only PATCH is exposed."""
        wire.reply({"records": [{"id": "rec1"}]})
        await client().update_records("Leads", [("rec1", {"Status": "Synced"})])
        assert wire.last.method == "PATCH"

    @pytest.mark.asyncio()
    async def test_typecast_is_off_by_default(self, wire: Any) -> None:
        """Coercion turns a typo into a new select option rather than an error."""
        wire.reply({"records": []})
        await client().create_records("Leads", [{"Name": "Ada"}])
        assert wire.last.payload["typecast"] is False

    @pytest.mark.asyncio()
    async def test_deletes_are_indexed_query_parameters(self, wire: Any) -> None:
        wire.reply({"records": [{"id": "rec1", "deleted": True}]})
        deleted = await client().delete_records("Leads", ["rec1"])
        assert wire.last.params == {"records[0]": "rec1"}
        assert deleted == ["rec1"]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_a_403_names_the_per_base_grant(self) -> None:
        """A valid token for a base it was never granted is this, and it reads
        as a permissions bug otherwise."""
        raised = _classify(403, {"error": {"type": "INVALID_PERMISSIONS"}})
        assert isinstance(raised, AirtableAuthError)
        assert "granted per base" in str(raised)

    def test_a_429_names_the_thirty_second_penalty(self) -> None:
        raised = _classify(429, {})
        assert isinstance(raised, AirtableRateLimited)
        assert not isinstance(raised, NonRetryableError)
        assert "30 seconds" in str(raised)

    def test_a_422_is_not_retryable(self) -> None:
        raised = _classify(422, {"error": {"type": "INVALID_REQUEST"}})
        assert isinstance(raised, AirtableInvalidRequest)
        assert isinstance(raised, NonRetryableError)

    def test_a_string_error_body_is_handled(self) -> None:
        """Airtable sends ``{"error": "NOT_FOUND"}`` on some routes and a
        nested object on others."""
        raised = _classify(404, {"error": "NOT_FOUND"})
        assert isinstance(raised, AirtableNotFound)

    def test_a_server_error_is_retryable(self) -> None:
        assert not isinstance(_classify(500, {}), NonRetryableError)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_effects_are_consistent_with_the_client(self) -> None:
        from pathlib import Path

        from loom.toolsets.airtable import tools

        source = Path("src/loom/toolsets/airtable/client.py").read_text(
            encoding="utf-8"
        )
        verify_effect_profile(
            AIRTABLE_MANIFEST, tools_module=tools, client_source=source
        )

    def test_creating_is_declared_non_idempotent(self) -> None:
        by_id = {op.id: op for op in AIRTABLE_MANIFEST.all_operations()}
        assert by_id["records.create"].idempotent is False

    def test_deleting_is_declared_destructive_and_irreversible(self) -> None:
        by_id = {op.id: op for op in AIRTABLE_MANIFEST.all_operations()}
        assert by_id["records.delete"].effect.value == "destructive"
        assert by_id["records.delete"].reversible is False

    def test_both_resolvers_are_declared(self) -> None:
        """A row by field value, and a field by name — both needed because a
        write takes an id and a read is keyed by a renameable name."""
        assert set(AIRTABLE_MANIFEST.resolvers()) >= {"record", "field"}

    def test_every_operation_declares_an_effect(self) -> None:
        for op in AIRTABLE_MANIFEST.all_operations():
            assert "effect" in op.model_fields_set, f"{op.id} did not declare one"
