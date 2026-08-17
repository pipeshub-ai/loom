"""Async Salesforce REST API client — pure httpx, no vendor SDK.

Two things make Salesforce unlike every toolset shipped here so far, and both
shape this file.

**There is no constant base URL.** Every org has its own host, returned by the
OAuth exchange as ``instance_url``. ``login.salesforce.com`` authenticates; it
does not serve data. A client that hardcodes a base URL works for zero orgs, so
this one refuses to be constructed without either an explicit instance URL or
the credentials that produce one.

**Access tokens expire mid-workflow.** The client owns the refresh, under a
lock, caching the result — the same arrangement ``toolsets/google/auth.py``
uses, and for the same reason: the alternative is a long workflow dying at hour
two with a 401 that reads like a permissions problem.

Credentials resolve from arguments, then the environment:

    SALESFORCE_ACCESS_TOKEN + SALESFORCE_INSTANCE_URL   already-obtained token
    SALESFORCE_CLIENT_ID + SALESFORCE_CLIENT_SECRET
        + SALESFORCE_REFRESH_TOKEN                      the refreshable form
    SALESFORCE_LOGIN_URL                                sandboxes use test.salesforce.com
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.pagination import LinkPaging, Results, page_through
from loom.toolsets.salesforce.models import (
    SalesforceAccount,
    SalesforceContact,
    SalesforceOpportunity,
    SalesforceRecord,
    SalesforceUser,
    SalesforceWriteResult,
)

#: Salesforce caps a query batch here; ``nextRecordsUrl`` carries the rest.
SALESFORCE_BATCH = 2000

DEFAULT_VERSION = "v60.0"
DEFAULT_LOGIN_URL = "https://login.salesforce.com"


class SalesforceError(WorkflowError):
    """A Salesforce request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self, message: str, *, status: int = 0, code: str = "", **kw: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        """Salesforce's own ``errorCode``. It identifies the failure far better
        than the status: ``REQUEST_LIMIT_EXCEEDED`` and ``INSUFFICIENT_ACCESS``
        are both 403 and mean opposite things about whether to try again."""


class SalesforcePermanentError(SalesforceError, NonRetryableError):
    """Fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class SalesforceAuthError(SalesforcePermanentError):
    """Credentials missing, malformed, or beyond refresh."""


class SalesforceRateLimited(SalesforceError):  # noqa: N818 - names a state
    """Org API quota exhausted. Retryable, and the caller should back off."""


class SalesforceClient:
    """Thin async wrapper around the Salesforce REST API."""

    def __init__(
        self,
        instance_url: str | None = None,
        access_token: str | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        login_url: str | None = None,
        version: str = DEFAULT_VERSION,
        timeout: float = 60.0,
    ) -> None:
        self._instance = (
            instance_url or os.environ.get("SALESFORCE_INSTANCE_URL", "")
        ).rstrip("/")
        self._token = access_token or os.environ.get("SALESFORCE_ACCESS_TOKEN", "")
        self._client_id = client_id or os.environ.get("SALESFORCE_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get(
            "SALESFORCE_CLIENT_SECRET", ""
        )
        self._refresh_token = refresh_token or os.environ.get(
            "SALESFORCE_REFRESH_TOKEN", ""
        )
        self._login_url = (
            login_url or os.environ.get("SALESFORCE_LOGIN_URL", DEFAULT_LOGIN_URL)
        ).rstrip("/")
        self._version = version
        self._timeout = timeout
        self._lock = asyncio.Lock()

        if not self._can_refresh() and not (self._instance and self._token):
            raise SalesforceAuthError(
                "Salesforce needs either SALESFORCE_INSTANCE_URL + "
                "SALESFORCE_ACCESS_TOKEN, or SALESFORCE_CLIENT_ID + "
                "SALESFORCE_CLIENT_SECRET + SALESFORCE_REFRESH_TOKEN. A "
                "sandbox also needs SALESFORCE_LOGIN_URL="
                "https://test.salesforce.com — pointing a sandbox refresh "
                "token at the production host fails as invalid_grant, which "
                "reads like a bad token rather than a wrong host."
            )

    def _can_refresh(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    # -- auth ---------------------------------------------------------------

    async def _refresh(self) -> None:
        """Exchange the refresh token, under a lock.

        Locked because a workflow running ten steps concurrently would
        otherwise send ten refreshes on the first expiry, and Salesforce counts
        every one against the org's API quota.
        """
        import httpx

        async with self._lock:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(
                    f"{self._login_url}/services/oauth2/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                    },
                )
            if response.status_code >= 400:
                raise SalesforceAuthError(
                    f"Salesforce token refresh failed ({response.status_code}): "
                    f"{response.text[:200]}"
                )
            body = response.json()
            self._token = body.get("access_token") or ""
            # The refresh answers with the instance URL too, so a client given
            # only refresh credentials learns where its org lives here.
            self._instance = (body.get("instance_url") or self._instance).rstrip("/")

    async def _ensure_token(self) -> None:
        if not self._token and self._can_refresh():
            await self._refresh()

    # -- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        _retried: bool = False,
    ) -> Any:
        import httpx

        await self._ensure_token()
        url = path if path.startswith("http") else f"{self._instance}{path}"

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                params=_clean(params),
                json=json,
            )

        if response.status_code == 401 and self._can_refresh() and not _retried:
            # Once. A second 401 after a fresh token is a real authorization
            # problem, and retrying it forever would turn a revoked grant into
            # an infinite loop against the login host.
            await self._refresh()
            return await self._request(
                method, path, params=params, json=json, _retried=True
            )
        if response.status_code >= 400:
            raise _classify(response)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _data(self, path: str) -> str:
        return f"/services/data/{self._version}{path}"

    # -- query --------------------------------------------------------------

    async def query(self, soql: str, *, limit: int = 200) -> Results[SalesforceRecord]:
        """Run SOQL, following ``nextRecordsUrl`` until Salesforce says done."""

        async def request(params: dict[str, Any]) -> Any:
            following = params.pop("__next_path", None)
            if following:
                # A complete path that takes no query parameters. Appending the
                # original SOQL to it restarts the query from the top, which
                # loops forever while looking like slow progress.
                return await self._request("GET", following)
            return await self._request(
                "GET", self._data("/query"), params={"q": soql}
            )

        return await page_through(
            request,
            style=LinkPaging(items="records"),
            limit=limit,
            page_size=SALESFORCE_BATCH,
            row=SalesforceRecord.from_api,
        )

    async def _query_rows(self, soql: str, limit: int) -> Results[dict[str, Any]]:
        # `.mapped`, not a comprehension: `query` pages and computes coverage,
        # and a comprehension over a `Results` yields a plain list — so
        # `.complete` was discarded one line after being computed, on the way to
        # every CRM finder below.
        rows = await self.query(soql, limit=limit)
        return rows.mapped(lambda r: r.fields)

    async def describe_object(self, sobject: str) -> dict[str, Any]:
        """Field names and types for one object.

        What the coding agent needs before writing SOQL against an org whose
        custom fields it cannot guess.
        """
        body = await self._request("GET", self._data(f"/sobjects/{sobject}/describe"))
        return {
            "name": body.get("name", sobject),
            "label": body.get("label", ""),
            "fields": [
                {
                    "name": f.get("name"),
                    "label": f.get("label"),
                    "type": f.get("type"),
                    "updateable": f.get("updateable"),
                }
                for f in (body.get("fields") or [])
            ],
        }

    # -- generic records ----------------------------------------------------

    async def get_record(
        self, sobject: str, record_id: str, *, fields: list[str] | None = None
    ) -> SalesforceRecord:
        params = {"fields": ",".join(fields)} if fields else None
        body = await self._request(
            "GET", self._data(f"/sobjects/{sobject}/{record_id}"), params=params
        )
        return SalesforceRecord.from_api(body)

    async def create_record(
        self, sobject: str, values: dict[str, Any]
    ) -> SalesforceWriteResult:
        body = await self._request(
            "POST", self._data(f"/sobjects/{sobject}"), json=values
        )
        return SalesforceWriteResult.from_api(body)

    async def update_record(
        self, sobject: str, record_id: str, values: dict[str, Any]
    ) -> SalesforceWriteResult:
        # 204 and an empty body on success, so the result is filled in here
        # rather than leaving a caller to tell an empty answer from a failure.
        await self._request(
            "PATCH", self._data(f"/sobjects/{sobject}/{record_id}"), json=values
        )
        return SalesforceWriteResult(id=record_id, success=True)

    async def delete_record(self, sobject: str, record_id: str) -> SalesforceWriteResult:
        await self._request("DELETE", self._data(f"/sobjects/{sobject}/{record_id}"))
        return SalesforceWriteResult(id=record_id, success=True)

    # -- CRM finders --------------------------------------------------------

    async def find_accounts(
        self, name: str = "", *, limit: int = 20
    ) -> Results[SalesforceAccount]:
        where = f"WHERE Name LIKE '%{_soql_escape(name)}%'" if name else ""
        rows = await self._query_rows(
            "SELECT Id, Name, Industry, Website, Phone, CreatedDate, Owner.Name "
            f"FROM Account {where} ORDER BY Name LIMIT {int(limit)}",
            limit,
        )
        return rows.mapped(SalesforceAccount.from_api)

    async def find_contacts(
        self, name: str = "", *, account_id: str = "", limit: int = 20
    ) -> Results[SalesforceContact]:
        clauses = []
        if name:
            escaped = _soql_escape(name)
            clauses.append(f"(Name LIKE '%{escaped}%' OR Email LIKE '%{escaped}%')")
        if account_id:
            clauses.append(f"AccountId = '{_soql_escape(account_id)}'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._query_rows(
            "SELECT Id, Name, Email, Phone, Title, AccountId, Account.Name "
            f"FROM Contact {where} ORDER BY Name LIMIT {int(limit)}",
            limit,
        )
        return rows.mapped(SalesforceContact.from_api)

    async def find_opportunities(
        self,
        name: str = "",
        *,
        account_id: str = "",
        stage: str = "",
        open_only: bool = False,
        limit: int = 20,
    ) -> Results[SalesforceOpportunity]:
        clauses = []
        if name:
            clauses.append(f"Name LIKE '%{_soql_escape(name)}%'")
        if account_id:
            clauses.append(f"AccountId = '{_soql_escape(account_id)}'")
        if stage:
            clauses.append(f"StageName = '{_soql_escape(stage)}'")
        if open_only:
            clauses.append("IsClosed = false")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._query_rows(
            "SELECT Id, Name, StageName, Amount, CloseDate, AccountId, "
            "Account.Name, Owner.Name, IsClosed, IsWon "
            f"FROM Opportunity {where} ORDER BY CloseDate DESC LIMIT {int(limit)}",
            limit,
        )
        return rows.mapped(SalesforceOpportunity.from_api)

    async def find_users(
        self, name: str = "", *, limit: int = 20
    ) -> Results[SalesforceUser]:
        where = ""
        if name:
            escaped = _soql_escape(name)
            where = f"WHERE (Name LIKE '%{escaped}%' OR Email LIKE '%{escaped}%')"
        rows = await self._query_rows(
            "SELECT Id, Name, Email, Username, IsActive "
            f"FROM User {where} ORDER BY Name LIMIT {int(limit)}",
            limit,
        )
        return rows.mapped(SalesforceUser.from_api)

    async def whoami(self) -> SalesforceUser:
        rows = await self._query_rows(
            "SELECT Id, Name, Email, Username, IsActive FROM User "
            "WHERE Id = (SELECT CreatedById FROM User LIMIT 1) LIMIT 1",
            1,
        )
        if rows:
            return SalesforceUser.from_api(rows[0])
        body = await self._request("GET", f"{self._login_url}/services/oauth2/userinfo")
        return SalesforceUser(
            id=str(body.get("user_id", "") or ""),
            name=body.get("name") or "",
            email=body.get("email") or "",
            username=body.get("preferred_username") or "",
        )


def _soql_escape(value: str) -> str:
    """Escape a value going into a SOQL string literal.

    Not decoration: a name containing an apostrophe — O'Brien, the single most
    predictable surname to meet in a CRM — otherwise terminates the literal and
    the query fails as a syntax error, or worse, changes meaning.
    """
    return value.replace("\\", "\\\\").replace("'", r"\'")


def _clean(params: Any) -> Any:
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> SalesforceError:
    """Turn a failed response into the narrowest error that fits.

    The 403 split is the point. ``REQUEST_LIMIT_EXCEEDED`` is the org running
    out of API calls — wait and it clears. Any other 403 is a permission the
    user does not have — wait and it does not. Blanket-retrying spends three
    attempts and some seconds proving the second case.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = None

    first = body[0] if isinstance(body, list) and body else {}
    detail = first.get("message") if isinstance(first, dict) else None
    code = str(first.get("errorCode", "") if isinstance(first, dict) else "")
    message = f"Salesforce {status}: {detail or response.text[:200] or 'request failed'}"
    if code:
        message += f" ({code})"

    if status == 403 and code == "REQUEST_LIMIT_EXCEEDED":
        return SalesforceRateLimited(message, status=status, code=code)
    if status == 429:
        return SalesforceRateLimited(message, status=status, code=code)
    if status == 401:
        return SalesforceAuthError(message, status=status, code=code)
    if 400 <= status < 500:
        return SalesforcePermanentError(message, status=status, code=code)
    return SalesforceError(message, status=status, code=code)


_default_client: SalesforceClient | None = None


def get_default_client() -> SalesforceClient:
    """Return (or create) the module-level client from the environment."""
    global _default_client
    if _default_client is None:
        _default_client = SalesforceClient()
    return _default_client


__all__ = [
    "SalesforceAuthError",
    "SalesforceClient",
    "SalesforceError",
    "SalesforcePermanentError",
    "SalesforceRateLimited",
    "get_default_client",
]
