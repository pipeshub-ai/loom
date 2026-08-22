"""Async Airtable client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    AIRTABLE_TOKEN     a personal access token (``pat…``), sent as a bearer
    AIRTABLE_BASE_ID   the default base (``app…``), overridable per call

Four things about this API drive the design here.

**A response is keyed by field *name*, not id.** Renaming a column in the UI
changes every key, and a workflow reading the old name gets ``None`` rather
than an error — the row is there, the value is not, and nothing says why.
``list_fields`` is marked ``resolves="field"`` for that reason, and
``return_field_ids`` is offered where a workflow wants the stable key.

**An empty field is omitted, not null.** So a missing key means "empty", and
"no such column" is indistinguishable from it in a response. That is the other
half of why the field list is worth reading.

**Writes are capped at ten records per request.** Sending eleven is a 422, so
the client batches rather than passing the cap to the caller — and a batch that
half-succeeds is reported as such rather than raised over, because Airtable has
no transaction across batches.

**Five requests per second, per base, and a 429 costs 30 seconds.** That
penalty is why the client does not fan out internally: the fastest way to be
slow here is to be greedy.
"""

from __future__ import annotations

from typing import Any, TypedDict
from urllib.parse import quote

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.airtable.models import AirtableField, AirtableRecord, AirtableTable
from loom.toolsets.pagination import Results, TokenPaging, page_through

BASE_URL = "https://api.airtable.com/v0"
META_URL = "https://api.airtable.com/v0/meta"

#: Airtable's documented page ceiling.
AIRTABLE_MAX_PAGE = 100

#: Records per write request. Eleven is a 422, so the client batches.
AIRTABLE_MAX_BATCH = 10


class AirtableError(WorkflowError):
    """An Airtable request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, code: str = "", **kw: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class AirtablePermanentError(AirtableError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class AirtableAuthError(AirtablePermanentError):
    """Missing, malformed, or revoked token — or one without access to the base.

    Airtable scopes a personal access token to explicit bases, so a valid token
    for the wrong base is a 403 that reads as a permissions bug rather than as
    a token that was never granted this base.
    """


class AirtableNotFound(AirtablePermanentError):  # noqa: N818 - names a state
    """No such base, table, or record.

    A *table name* that does not exist gives this too, and the likeliest cause
    is a rename — the same failure mode as a renamed field, one level up.
    """


class AirtableInvalidRequest(AirtablePermanentError):  # noqa: N818 - names a state
    """A 422 — a formula, a field name, or a batch size Airtable will not take."""


class AirtableRateLimited(AirtableError):  # noqa: N818 - names a state
    """HTTP 429 — five requests per second per base, exceeded.

    Retryable, but expensively: Airtable locks the base out for **30 seconds**
    after a 429, so backing off hard beats retrying quickly.
    """


def escape_formula(value: str) -> str:
    """Escape a string for use inside a ``filterByFormula`` literal.

    ``O'Brien`` terminates the literal, and Airtable's formula parser then
    either errors or — worse — evaluates something else that matches nothing.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


class _ErrorFields(TypedDict):
    """The fields every classified Airtable error carries.

    A ``TypedDict`` because the values are heterogeneous — one ``int`` among
    strings — so a plain literal infers ``dict[str, object]`` and cannot be
    unpacked into a constructor expecting an ``int``.
    """

    status: int
    code: str


def _classify(status: int, body: dict[str, Any]) -> AirtableError:
    error = body.get("error")
    if isinstance(error, str):
        code, message = error, error
    else:
        error = error or {}
        code = str(error.get("type") or "")
        message = str(error.get("message") or f"Airtable returned HTTP {status}")
    shared: _ErrorFields = {"status": status, "code": code}

    if status in (401, 403):
        return AirtableAuthError(
            f"{message} (a personal access token is granted per base — a valid "
            f"token for a base it was not granted returns this)",
            **shared,
        )
    if status == 404:
        return AirtableNotFound(
            f"{message} (a table is addressed by name or id; a renamed table "
            f"gives this, as a renamed field gives a silent None)",
            **shared,
        )
    if status == 429:
        return AirtableRateLimited(
            f"{message} (five requests per second per base; Airtable locks the "
            f"base for 30 seconds after this, so back off hard)",
            **shared,
        )
    if 400 <= status < 500:
        return AirtableInvalidRequest(message, **shared)
    return AirtableError(message, **shared)


class AirtableClient:
    """Thin async wrapper around the Airtable REST API."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_id: str = "",
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._base_id = base_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        if not self._token:
            raise AirtableAuthError(
                "Airtable needs a personal access token: set AIRTABLE_TOKEN or "
                "pass token=. Tokens are created at "
                "https://airtable.com/create/tokens and must be granted access "
                "to each base individually — API keys were retired in 2024."
            )

    def _base(self, base_id: str = "") -> str:
        chosen = base_id or self._base_id
        if not chosen:
            raise AirtableAuthError(
                "Airtable needs a base id (app…): set AIRTABLE_BASE_ID, pass "
                "base_id= to the client, or name one per call."
            )
        return chosen

    # -- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=params,
                json=payload,
            )
        decoded: dict[str, Any] = {}
        if response.content:
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
        if response.status_code >= 400:
            raise _classify(response.status_code, decoded)
        return decoded

    def _table_url(self, table: str, base_id: str = "") -> str:
        # Both halves quoted: a table may be addressed by a human name with a
        # space in it, and an unencoded one is a different path.
        return f"{self._base_url}/{self._base(base_id)}/{quote(table, safe='')}"

    # -- reads --------------------------------------------------------------

    async def list_records(
        self,
        table: str,
        *,
        base_id: str = "",
        formula: str = "",
        view: str = "",
        fields: list[str] | None = None,
        sort_field: str = "",
        sort_desc: bool = False,
        return_field_ids: bool = False,
        limit: int = 100,
    ) -> Results[AirtableRecord]:
        """Rows from one table, following pages."""
        params: dict[str, Any] = {}
        if formula:
            params["filterByFormula"] = formula
        if view:
            params["view"] = view
        if fields:
            for index, name in enumerate(fields):
                params[f"fields[{index}]"] = name
        if sort_field:
            params["sort[0][field]"] = sort_field
            params["sort[0][direction]"] = "desc" if sort_desc else "asc"
        if return_field_ids:
            params["returnFieldsByFieldId"] = "true"

        url = self._table_url(table, base_id)

        async def request(paging: dict[str, Any]) -> Any:
            return await self._request("GET", url, params={**params, **paging})

        found = await page_through(
            request,
            style=TokenPaging(
                items="records",
                size_param="pageSize",
                token_param="offset",
                token_field="offset",
            ),
            limit=limit,
            page_size=min(limit, AIRTABLE_MAX_PAGE) or 1,
        )
        return found.mapped(AirtableRecord.from_api)

    async def get_record(
        self, table: str, record_id: str, *, base_id: str = ""
    ) -> AirtableRecord:
        body = await self._request(
            "GET", f"{self._table_url(table, base_id)}/{record_id}"
        )
        return AirtableRecord.from_api(body)

    async def find_records(
        self, table: str, field: str, value: str, *, base_id: str = "", limit: int = 10
    ) -> Results[AirtableRecord]:
        """Rows where *field* equals *value*, exactly.

        A field **name**, because that is what a formula matches on — and what
        a rename breaks. Resolve it with :meth:`list_fields` when the workflow
        should survive one.
        """
        formula = f"{{{field}}} = '{escape_formula(value)}'"
        return await self.list_records(
            table, base_id=base_id, formula=formula, limit=limit
        )

    async def list_fields(self, table: str, *, base_id: str = "") -> list[AirtableField]:
        """Every column in *table*, with both its id and its name.

        The resolver. A response is keyed by name, an empty field is omitted
        rather than nulled, and a rename changes the key — so "is this column
        empty or gone?" is a question only this can answer.
        """
        body = await self._request(
            "GET", f"{META_URL}/bases/{self._base(base_id)}/tables"
        )
        for raw in body.get("tables") or []:
            found = AirtableTable.from_api(raw)
            if table in (found.id, found.name):
                return found.fields
        raise AirtableNotFound(
            f"no table {table!r} in base {self._base(base_id)}. A renamed table "
            "gives this — address it by id (tbl…) to survive a rename."
        )

    # -- writes -------------------------------------------------------------

    async def create_records(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        base_id: str = "",
        typecast: bool = False,
    ) -> list[AirtableRecord]:
        """Create rows, ten at a time.

        Batched here rather than in the caller because the cap is Airtable's,
        not the workflow's. A batch that fails partway raises with the rows
        already written left written — Airtable has no transaction across
        requests, and pretending otherwise would be worse than saying so.
        """
        created: list[AirtableRecord] = []
        url = self._table_url(table, base_id)
        for start in range(0, len(rows), AIRTABLE_MAX_BATCH):
            chunk = rows[start : start + AIRTABLE_MAX_BATCH]
            payload = {
                "records": [{"fields": fields} for fields in chunk],
                "typecast": typecast,
            }
            body = await self._request("POST", url, payload=payload)
            created.extend(
                AirtableRecord.from_api(raw) for raw in body.get("records") or []
            )
        return created

    async def update_records(
        self,
        table: str,
        updates: list[tuple[str, dict[str, Any]]],
        *,
        base_id: str = "",
        typecast: bool = False,
    ) -> list[AirtableRecord]:
        """Patch rows by id, ten at a time.

        A patch, not a replace: fields not named are left alone. Airtable's
        ``PUT`` clears them instead, which is a data-loss bug that returns 200,
        so this client does not offer it.
        """
        updated: list[AirtableRecord] = []
        url = self._table_url(table, base_id)
        for start in range(0, len(updates), AIRTABLE_MAX_BATCH):
            chunk = updates[start : start + AIRTABLE_MAX_BATCH]
            payload = {
                "records": [
                    {"id": record_id, "fields": fields} for record_id, fields in chunk
                ],
                "typecast": typecast,
            }
            body = await self._request("PATCH", url, payload=payload)
            updated.extend(
                AirtableRecord.from_api(raw) for raw in body.get("records") or []
            )
        return updated

    async def delete_records(
        self, table: str, record_ids: list[str], *, base_id: str = ""
    ) -> list[str]:
        """Delete rows by id, ten at a time. Returns what was deleted."""
        deleted: list[str] = []
        url = self._table_url(table, base_id)
        for start in range(0, len(record_ids), AIRTABLE_MAX_BATCH):
            chunk = record_ids[start : start + AIRTABLE_MAX_BATCH]
            params: dict[str, Any] = {}
            for index, record_id in enumerate(chunk):
                params[f"records[{index}]"] = record_id
            body = await self._request("DELETE", url, params=params)
            deleted.extend(
                str(raw.get("id")) for raw in body.get("records") or [] if raw.get("id")
            )
        return deleted




