"""OneDrive and SharePoint Online — one API, two toolsets.

These drive a real ``httpx`` transport rather than patching client internals, so
what is asserted is the request that would go on the wire: URL, query, headers
and body. That matters more here than for most toolsets, because Graph's traps
are all in the URL — a colon in the wrong place, a query string replaced by an
empty one, a header that must *not* be sent.

The named failures each test pins are in the docstrings. Every one is a case
where Graph answers something plausible rather than an error.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx
import pytest

from loom.toolsets.microsoft.addressing import child_address, item_address
from loom.toolsets.microsoft.auth import MicrosoftAuth, MicrosoftCredentials
from loom.toolsets.microsoft.errors import (
    GraphAPIError,
    GraphAuthError,
    GraphPermanentError,
    GraphThrottled,
    classify,
)
from loom.toolsets.microsoft.models import (
    DriveItem,
    Permission,
    SharingLink,
)
from loom.toolsets.microsoft.onedrive.client import OneDriveClient
from loom.toolsets.microsoft.sharepoint.client import SharePointClient
from loom.toolsets.microsoft.sharepoint.models import ListColumn, ListItem, Site


class Wire:
    """Records every request and answers from a scripted queue.

    A callable may be queued instead of a body, so a test can branch on the
    request — which is how the paging and delta chains are expressed.
    """

    def __init__(self, *responses: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses) or [{}]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        entry = (
            self._responses.pop(0)
            if len(self._responses) > 1
            else self._responses[0]
        )
        if callable(entry):
            entry = entry(request)
        if isinstance(entry, httpx.Response):
            return entry
        return httpx.Response(200, json=entry)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def url(self, index: int = -1) -> str:
        return str(self.requests[index].url)

    def path(self, index: int = -1) -> str:
        """The decoded path — what a human reads."""
        return self.requests[index].url.path

    def raw(self, index: int = -1) -> str:
        """The path as it goes on the wire, percent-encoding intact.

        Distinct from :meth:`path` because httpx decodes characters that are
        legal in a path when rendering it, so ``path`` cannot tell you whether
        an ``@`` or a ``!`` in an id was escaped. Where the escaping is the
        thing under test, assert on this.
        """
        return self.requests[index].url.raw_path.decode().split("?")[0]

    def query(self, key: str, index: int = -1) -> str | None:
        return self.requests[index].url.params.get(key)

    def body(self, index: int = -1) -> Any:
        import json as jsonlib

        raw = self.requests[index].content
        return jsonlib.loads(raw) if raw else None


def token_auth() -> MicrosoftAuth:
    return MicrosoftAuth(MicrosoftCredentials(access_token="tkn"))


def app_only_auth() -> MicrosoftAuth:
    """Client-credentials auth whose token endpoint is stubbed.

    The transport matters: without it these tests reach
    ``login.microsoftonline.com`` for real, because the token endpoint is the
    one call that does not go through the client's injected transport.
    """

    def mint(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "app", "expires_in": 3599})

    return MicrosoftAuth(
        MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s"),
        transport=httpx.MockTransport(mint),
    )


def onedrive(wire: Wire, **kw: Any) -> OneDriveClient:
    return OneDriveClient(kw.pop("auth", None) or token_auth(),
                          transport=wire.transport, **kw)


def sharepoint(wire: Wire, **kw: Any) -> SharePointClient:
    return SharePointClient(kw.pop("auth", None) or token_auth(),
                            transport=wire.transport, **kw)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentialResolution:
    def test_the_durable_credential_outranks_the_ready_made_one(self) -> None:
        """An access token lives an hour; a refresh token mints them forever.

        Preferring the access token when both are configured means everything
        works until it silently does not, while the credential that would have
        kept working sat unused beside it. A workflow that sleeps outlives an
        access token by design.
        """
        both = MicrosoftCredentials(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            refresh_token="r",
            access_token="a",
        )
        assert both.mode == "refresh_token"

    def test_a_client_secret_alone_is_app_only(self) -> None:
        creds = MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s")
        assert creds.mode == "client_credentials"

    def test_an_access_token_alone_is_enough(self) -> None:
        assert MicrosoftCredentials(access_token="a").mode == "token"

    def test_nothing_configured_selects_nothing(self) -> None:
        assert MicrosoftCredentials().mode == ""

    def test_a_partial_confidential_client_is_not_a_mode(self) -> None:
        """Two of the three is not "nearly working" — it is unusable.

        Reporting it as client_credentials would send a token request with an
        empty secret and surface as an authentication failure, which reads as
        a wrong secret rather than a missing one.
        """
        assert MicrosoftCredentials(tenant_id="t", client_id="c").mode == ""

    def test_the_azure_sdk_variables_are_accepted(self) -> None:
        """AZURE_* is the trio the Azure SDKs already put in an environment.

        Demanding a second copy under MS_* is a configuration bug waiting to
        happen, and one nobody would think to look for.
        """
        creds = MicrosoftCredentials.from_env(
            {
                "AZURE_TENANT_ID": "t",
                "AZURE_CLIENT_ID": "c",
                "AZURE_CLIENT_SECRET": "s",
            }
        )
        assert creds.mode == "client_credentials"
        assert creds.tenant_id == "t"

    def test_the_ms_prefix_wins_over_the_azure_one(self) -> None:
        creds = MicrosoftCredentials.from_env(
            {"MS_TENANT_ID": "mine", "AZURE_TENANT_ID": "theirs"}
        )
        assert creds.tenant_id == "mine"

    def test_the_token_url_names_the_tenant(self) -> None:
        creds = MicrosoftCredentials(tenant_id="contoso.onmicrosoft.com")
        assert creds.token_url == (
            "https://login.microsoftonline.com/"
            "contoso.onmicrosoft.com/oauth2/v2.0/token"
        )

    def test_a_national_cloud_authority_is_configurable(self) -> None:
        creds = MicrosoftCredentials.from_env(
            {"MS_TENANT_ID": "t", "MS_AUTHORITY_HOST": "https://login.microsoftonline.us"}
        )
        assert creds.token_url.startswith("https://login.microsoftonline.us/t/")

    def test_no_credentials_at_all_raises_on_construction(self) -> None:
        with pytest.raises(GraphAuthError) as caught:
            MicrosoftAuth(MicrosoftCredentials())
        assert "MS_TENANT_ID" in str(caught.value)


class TokenEndpoint:
    """Stands in for ``login.microsoftonline.com`` and records the form posted."""

    def __init__(self) -> None:
        self.bodies: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content.decode())
        return httpx.Response(
            200, json={"access_token": f"tok{len(self.bodies)}", "expires_in": 3599}
        )


class TestTokenMinting:
    @pytest.mark.asyncio
    async def test_client_credentials_asks_for_the_default_scope(self) -> None:
        """``/.default`` is not a scope list.

        It means "every application permission already granted to this app".
        Naming individual scopes beside it is an ``invalid_scope`` 400.
        """
        endpoint = TokenEndpoint()
        auth = MicrosoftAuth(
            MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s"),
            transport=endpoint.transport,
        )
        assert await auth.token() == "tok1"

        body = endpoint.bodies[0]
        assert "grant_type=client_credentials" in body
        assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body
        assert "offline_access" not in body

    @pytest.mark.asyncio
    async def test_the_refresh_flow_asks_for_offline_access(self) -> None:
        """Without it the response carries no replacement refresh token.

        The grant then expires and the workflow stops working weeks later for
        no visible reason.
        """
        endpoint = TokenEndpoint()
        auth = MicrosoftAuth(
            MicrosoftCredentials(
                tenant_id="t", client_id="c", client_secret="s", refresh_token="r"
            ),
            transport=endpoint.transport,
        )
        assert await auth.token() == "tok1"

        body = endpoint.bodies[0]
        assert "grant_type=refresh_token" in body
        assert "offline_access" in body

    @pytest.mark.asyncio
    async def test_a_token_is_minted_once_and_reused(self) -> None:
        """Ten fanned-out steps should mint one token, not ten."""
        endpoint = TokenEndpoint()
        auth = MicrosoftAuth(
            MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s"),
            transport=endpoint.transport,
        )
        for _ in range(5):
            await auth.token()
        assert len(endpoint.bodies) == 1

    @pytest.mark.asyncio
    async def test_invalidate_forces_a_fresh_mint(self) -> None:
        """For a revoked grant, or a clock further out than the skew allows."""
        endpoint = TokenEndpoint()
        auth = MicrosoftAuth(
            MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s"),
            transport=endpoint.transport,
        )
        await auth.token()
        auth.invalidate()
        assert await auth.token() == "tok2"
        assert len(endpoint.bodies) == 2

    @pytest.mark.asyncio
    async def test_a_ready_made_token_mints_nothing(self) -> None:
        endpoint = TokenEndpoint()
        auth = MicrosoftAuth(
            MicrosoftCredentials(access_token="given"), transport=endpoint.transport
        )
        assert await auth.token() == "given"
        assert endpoint.bodies == []

    @pytest.mark.asyncio
    async def test_a_rejected_token_request_classifies_like_any_other(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_client", "error_description": "bad secret"}
            )

        auth = MicrosoftAuth(
            MicrosoftCredentials(tenant_id="t", client_id="c", client_secret="s"),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(GraphPermanentError) as caught:
            await auth.token()
        assert "bad secret" in str(caught.value)

    def test_app_only_is_flagged_and_a_person_is_not(self) -> None:
        assert app_only_auth().is_app_only is True
        assert token_auth().is_app_only is False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (429, "TooManyRequests", GraphThrottled),
            (503, "activityLimitReached", GraphThrottled),
            (503, "serviceNotAvailable", GraphThrottled),
            (500, "", GraphAPIError),
            (502, "", GraphAPIError),
            (504, "", GraphAPIError),
            (509, "", GraphAPIError),
            (401, "InvalidAuthenticationToken", GraphAuthError),
            (403, "accessDenied", GraphPermanentError),
            (404, "itemNotFound", GraphPermanentError),
            (400, "invalidRequest", GraphPermanentError),
            (409, "nameAlreadyExists", GraphPermanentError),
            (410, "resyncRequired", GraphPermanentError),
            (423, "resourceLocked", GraphPermanentError),
            (507, "quotaLimitReached", GraphPermanentError),
        ],
    )
    def test_each_failure_gets_the_right_retryability(
        self, status: int, code: str, expected: type
    ) -> None:
        error = classify(status, {"error": {"code": code, "message": "m"}})
        assert type(error) is expected

    def test_a_retryable_code_beats_a_permanent_looking_status(self) -> None:
        """OneDrive reports load shedding as a 503 ``activityLimitReached``.

        Classifying on the status alone would call it a plain server error;
        classifying on the code gets the back-off and the Retry-After.
        """
        error = classify(
            503,
            {"error": {"code": "activityLimitReached", "message": "slow"}},
            headers={"Retry-After": "12"},
        )
        assert isinstance(error, GraphThrottled)
        assert error.retry_after == 12.0

    def test_a_permanent_code_beats_a_retryable_status(self) -> None:
        """507 with quotaLimitReached is out of storage. Waiting adds no disk."""
        error = classify(507, {"error": {"code": "quotaLimitReached", "message": "m"}})
        assert isinstance(error, GraphPermanentError)

    def test_a_conflict_is_retryable_only_when_it_says_so(self) -> None:
        """The 409 split is the documented one, and it is the header that tells.

        A concurrency-violation 409 "can be repeated after some delay […] if a
        Retry-After header is present"; a driveItem 409 is nameAlreadyExists
        and will never succeed on repeat.
        """
        transient = classify(
            409,
            {"error": {"code": "Directory_ConcurrencyViolation", "message": "m"}},
            headers={"Retry-After": "3"},
        )
        permanent = classify(409, {"error": {"code": "nameAlreadyExists", "message": "m"}})
        assert isinstance(transient, GraphThrottled)
        assert isinstance(permanent, GraphPermanentError)

    def test_the_nested_code_is_read_too(self) -> None:
        """The docs say to use the most detailed nested code understood."""
        error = classify(
            400, {"error": {"code": "badRequest", "message": "m",
                            "innerError": {"code": "resourceLocked"}}}
        )
        assert isinstance(error, GraphPermanentError)

    def test_the_request_id_is_carried(self) -> None:
        """It is what makes a support ticket answerable."""
        error = classify(
            500,
            {"error": {"code": "x", "message": "m",
                       "innerError": {"request-id": "abc-123"}}},
        )
        assert error.request_id == "abc-123"

    def test_a_permanent_error_stops_an_ordinary_retry_policy(self) -> None:
        """The whole point of classifying: no per-step configuration needed."""
        from loom.core.exceptions import NonRetryableError

        assert isinstance(classify(404, {"error": {"code": "itemNotFound"}}),
                          NonRetryableError)
        assert not isinstance(classify(429, {"error": {"code": "x"}}),
                              NonRetryableError)


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


class TestAddressing:
    """Graph's colon escape, which is wrong in a way that is hard to see."""

    def test_a_path_addressed_folder_takes_a_second_colon_before_children(self) -> None:
        assert (
            item_address("/me/drive", path="Reports/2024", suffix="children")
            == "/me/drive/root:/Reports/2024:/children"
        )

    def test_an_id_addressed_item_takes_no_colons(self) -> None:
        assert (
            item_address("/me/drive", item_id="01ABC", suffix="children")
            == "/me/drive/items/01ABC/children"
        )

    def test_the_root_needs_no_escape_at_all(self) -> None:
        assert item_address("/me/drive", suffix="children") == "/me/drive/root/children"

    def test_spaces_in_a_path_are_encoded_but_separators_are_not(self) -> None:
        assert (
            item_address("/me/drive", path="My Files/Q3 2024.xlsx")
            == "/me/drive/root:/My%20Files/Q3%202024.xlsx"
        )

    def test_an_id_wins_over_a_path(self) -> None:
        """An id is unambiguous and a path is not, so the id decides."""
        assert (
            item_address("/me/drive", item_id="01X", path="ignored")
            == "/me/drive/items/01X"
        )

    def test_creating_a_child_under_a_parent_id_needs_the_extra_colon(self) -> None:
        """The difference between naming an item and creating one there.

        ``/items/{id}/content`` writes the parent's own content stream, which
        for a folder is a 400 mentioning neither the parent nor the name.
        """
        assert (
            child_address("/me/drive", "a.txt", parent_id="01P", suffix="content")
            == "/me/drive/items/01P:/a.txt:/content"
        )

    def test_creating_a_child_under_a_path_joins_the_two(self) -> None:
        assert (
            child_address("/me/drive", "a.txt", parent_path="Reports", suffix="content")
            == "/me/drive/root:/Reports/a.txt:/content"
        )

    def test_creating_a_child_at_the_root(self) -> None:
        assert (
            child_address("/me/drive", "a.txt", suffix="content")
            == "/me/drive/root:/a.txt:/content"
        )

    def test_the_same_rules_serve_a_sharepoint_library(self) -> None:
        """A document library is a drive, so there is one rule, not two."""
        assert (
            item_address("/sites/s1/drive", path="Policies", suffix="children")
            == "/sites/s1/drive/root:/Policies:/children"
        )


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


class TestPaging:
    @pytest.mark.asyncio
    async def test_the_next_link_is_followed_exactly_as_given(self) -> None:
        """The bug this pins cost a fifty-request loop returning page one.

        ``@odata.nextLink`` is an absolute URL whose whole meaning is in its
        ``$skiptoken``. httpx *replaces* a URL's query string whenever ``params``
        is supplied — an empty dict clears it — so passing ``{}`` on the follow-up
        silently re-fetched the first page until the MAX_PAGES backstop cut it
        off, with a result full of duplicates and no error anywhere.
        """
        wire = Wire(
            {
                "value": [{"id": "1", "name": "a"}],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/drive/root/children"
                    "?%24skiptoken=TOK"
                ),
            },
            {"value": [{"id": "2", "name": "b"}]},
        )
        items = await onedrive(wire).list_children()

        assert [i.name for i in items] == ["a", "b"]
        assert len(wire.requests) == 2
        assert wire.query("$skiptoken", index=1) == "TOK"

    @pytest.mark.asyncio
    async def test_the_follow_up_does_not_repeat_top(self) -> None:
        """The link already encodes every original parameter, ``$top`` included.

        Sending ours again duplicates the query parameter on a URL that already
        carries it — and the reference says to use the entire URL as given.
        """
        wire = Wire(
            {
                "value": [{"id": "1"}],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/drive/root/children"
                    "?%24top=200&%24skiptoken=TOK"
                ),
            },
            {"value": []},
        )
        await onedrive(wire).list_children()
        assert str(wire.url(index=1)).count("top") == 1

    @pytest.mark.asyncio
    async def test_paging_stops_when_the_link_is_absent(self) -> None:
        """Graph signals the end by omitting the link, not by a done flag."""
        wire = Wire({"value": [{"id": "1"}]})
        items = await onedrive(wire).list_children()
        assert len(wire.requests) == 1
        assert items.complete is True

    @pytest.mark.asyncio
    async def test_the_first_request_asks_for_a_page_size(self) -> None:
        wire = Wire({"value": []})
        await onedrive(wire).list_children()
        assert wire.query("$top") == "200"

    @pytest.mark.asyncio
    async def test_a_truncated_read_says_so(self) -> None:
        """``Results.complete`` is the guard against reporting a page as a total."""
        wire = Wire(
            lambda request: {
                "value": [{"id": str(n)} for n in range(200)],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?%24skiptoken=T",
            }
        )
        items = await onedrive(wire).list_children(limit=10)
        assert items.complete is False


class TestRequestPlumbing:
    @pytest.mark.asyncio
    async def test_a_401_remints_and_retries_exactly_once(self) -> None:
        """The one case where the identical request legitimately succeeds."""
        answers = [httpx.Response(401, json={"error": {"code": "x"}}),
                   httpx.Response(200, json={"id": "1"})]
        wire = Wire(*answers)
        item = await onedrive(wire).get_item("01X")
        assert len(wire.requests) == 2
        assert item.id == "1"

    @pytest.mark.asyncio
    async def test_a_persistent_401_surfaces_as_an_auth_error(self) -> None:
        wire = Wire(httpx.Response(401, json={"error": {"code": "x", "message": "no"}}))
        with pytest.raises(GraphAuthError):
            await onedrive(wire).get_item("01X")
        assert len(wire.requests) == 2

    @pytest.mark.asyncio
    async def test_every_request_carries_the_bearer_token(self) -> None:
        wire = Wire({"id": "1"})
        await onedrive(wire).get_item("01X")
        assert wire.last.headers["authorization"] == "Bearer tkn"


# ---------------------------------------------------------------------------
# OneDrive
# ---------------------------------------------------------------------------


class TestAppOnlyDriveScope:
    """The Microsoft-specific trap worth engineering against.

    An app-only token has no user attached, so ``/me/drive`` does not exist.
    Graph answers a 400 whose message reads as a broken toolset rather than a
    missing argument, and it arrives from inside whichever step ran first.
    """

    @pytest.mark.asyncio
    async def test_app_only_without_a_drive_scope_refuses_before_the_request(
        self,
    ) -> None:
        wire = Wire({"id": "1"})
        client = onedrive(wire, auth=app_only_auth())
        with pytest.raises(GraphPermanentError) as caught:
            await client.get_drive()
        assert wire.requests == [], "it should not have spent a round trip"
        assert "user_id=" in str(caught.value)
        assert "drive_id=" in str(caught.value)
        assert "MS_REFRESH_TOKEN" in str(caught.value)

    @pytest.mark.asyncio
    async def test_app_only_with_a_user_is_fine(self) -> None:
        wire = Wire({"id": "d1", "name": "OneDrive"})
        client = onedrive(wire, auth=app_only_auth(), user_id="a@b.com")
        await client.get_drive()
        assert wire.raw() == "/v1.0/users/a%40b.com/drive"

    @pytest.mark.asyncio
    async def test_app_only_with_a_drive_id_is_fine(self) -> None:
        wire = Wire({"id": "d1"})
        client = onedrive(wire, auth=app_only_auth(), drive_id="b!xyz")
        await client.get_drive()
        assert wire.raw() == "/v1.0/drives/b%21xyz"

    @pytest.mark.asyncio
    async def test_whoami_refuses_under_app_only(self) -> None:
        """There is no signed-in user to report, so there is no honest answer."""
        wire = Wire({"id": "u"})
        with pytest.raises(GraphPermanentError) as caught:
            await onedrive(wire, auth=app_only_auth()).whoami()
        assert wire.requests == []
        assert "MS_REFRESH_TOKEN" in str(caught.value)

    @pytest.mark.asyncio
    async def test_delegated_credentials_use_me(self) -> None:
        wire = Wire({"id": "d"})
        await onedrive(wire).get_drive()
        assert wire.path() == "/v1.0/me/drive"


class TestOneDriveReads:
    @pytest.mark.asyncio
    async def test_search_escapes_a_quote_by_doubling_it(self) -> None:
        """An apostrophe in a filename otherwise breaks the OData literal.

        Graph rejects it with a parse error naming neither the file nor the
        quote.
        """
        wire = Wire({"value": []})
        await onedrive(wire).search("it's here")
        assert "it''s" in wire.path()
        assert "it%27%27s" in wire.raw()

    @pytest.mark.asyncio
    async def test_search_can_be_scoped_to_a_folder(self) -> None:
        wire = Wire({"value": []})
        await onedrive(wire).search("q", path="Reports")
        assert wire.path() == "/v1.0/me/drive/root:/Reports:/search(q='q')"

    @pytest.mark.asyncio
    async def test_downloading_a_folder_is_refused_by_name(self) -> None:
        wire = Wire({"id": "1", "name": "Reports", "folder": {"childCount": 3}})
        with pytest.raises(GraphPermanentError) as caught:
            await onedrive(wire).download_file("1")
        assert "Reports" in str(caught.value)
        assert "list_children" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_download_returns_an_attachment_with_the_real_name(self) -> None:
        wire = Wire(
            {"id": "1", "name": "report.pdf", "file": {"mimeType": "application/pdf"}},
            httpx.Response(200, content=b"%PDF-1.4"),
        )
        attachment = await onedrive(wire).download_file("1")
        assert attachment.filename == "report.pdf"
        assert attachment.mime == "application/pdf"
        assert attachment.data == b"%PDF-1.4"


class TestOneDriveDelta:
    @pytest.mark.asyncio
    async def test_it_chases_next_links_and_returns_the_terminal_delta_link(
        self,
    ) -> None:
        """The link is the load-bearing half — without it the next run
        re-enumerates the whole drive, which is the polling delta replaces."""
        wire = Wire(
            {
                "value": [{"id": "1", "name": "a", "file": {}}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?token=T2",
            },
            {
                "value": [{"id": "2", "name": "b", "deleted": {}}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?token=FINAL",
            },
        )
        page = await onedrive(wire).list_changes()
        assert [i.name for i in page.items] == ["a", "b"]
        assert page.delta_link.endswith("token=FINAL")
        assert page.complete is True

    @pytest.mark.asyncio
    async def test_a_deletion_arrives_as_an_entry_not_an_absence(self) -> None:
        wire = Wire(
            {"value": [{"id": "9", "name": "gone", "deleted": {}}],
             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?token=F"}
        )
        page = await onedrive(wire).list_changes()
        assert page.items[0].deleted is True

    @pytest.mark.asyncio
    async def test_token_latest_starts_watching_without_enumerating(self) -> None:
        wire = Wire(
            {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?t=F"}
        )
        page = await onedrive(wire).list_changes(token="latest")
        assert wire.query("token") == "latest"
        assert page.items == []
        assert page.delta_link

    @pytest.mark.asyncio
    async def test_a_stored_link_is_called_verbatim(self) -> None:
        stored = "https://graph.microsoft.com/v1.0/me/drive/root/delta?token=STORED"
        wire = Wire({"value": [], "@odata.deltaLink": stored})
        await onedrive(wire).list_changes(delta_link=stored)
        assert wire.query("token") == "STORED"


class TestOneDriveUploads:
    @pytest.mark.asyncio
    async def test_a_small_upload_writes_to_the_child_content_path(self) -> None:
        wire = Wire({"id": "1", "name": "a.txt"})
        await onedrive(wire).upload_file("a.txt", b"hi", parent_path="Reports")
        assert wire.path() == "/v1.0/me/drive/root:/Reports/a.txt:/content"
        assert wire.last.method == "PUT"
        assert wire.last.content == b"hi"

    @pytest.mark.asyncio
    async def test_the_conflict_behaviour_is_sent(self) -> None:
        wire = Wire({"id": "1"})
        await onedrive(wire).upload_file("a.txt", b"x", on_conflict="rename")
        assert wire.query("@microsoft.graph.conflictBehavior") == "rename"

    @pytest.mark.asyncio
    async def test_text_is_encoded_as_utf8(self) -> None:
        wire = Wire({"id": "1"})
        await onedrive(wire).upload_file("a.txt", "héllo")
        assert wire.last.content == "héllo".encode()

    @pytest.mark.asyncio
    async def test_an_oversized_simple_upload_is_refused_by_name(self) -> None:
        """Microsoft's own guidance switches to a session above 10 MiB.

        Sending it anyway fails obscurely; refusing names the tool that works.
        """
        wire = Wire({"id": "1"})
        client = onedrive(wire, simple_upload_max=100)
        with pytest.raises(GraphPermanentError) as caught:
            await client.upload_file("big.bin", b"x" * 101)
        assert "upload_large_file" in str(caught.value)
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_a_large_upload_sends_ordered_fragments_with_content_range(
        self,
    ) -> None:
        wire = Wire(
            {"uploadUrl": "https://up.1drv.com/session", "expirationDateTime": "z"},
            lambda request: httpx.Response(202, json={"nextExpectedRanges": ["1-"]}),
            lambda request: httpx.Response(202, json={"nextExpectedRanges": ["2-"]}),
            httpx.Response(201, json={"id": "9", "name": "big.bin", "size": 25}),
        )
        client = onedrive(wire, chunk_size=10)
        item = await client.upload_large_file("big.bin", b"x" * 25)

        fragments = wire.requests[1:]
        assert [f.headers["content-range"] for f in fragments] == [
            "bytes 0-9/25",
            "bytes 10-19/25",
            "bytes 20-24/25",
        ]
        assert item.id == "9"

    @pytest.mark.asyncio
    async def test_a_fragment_carries_no_authorization_header(self) -> None:
        """The documented trap, and the one request that must not be signed.

        "If you include the Authorization header when issuing the PUT call, it
        might result in an HTTP 401 Unauthorized response." The upload URL is
        pre-authenticated.
        """
        wire = Wire(
            {"uploadUrl": "https://up.1drv.com/session"},
            httpx.Response(201, json={"id": "9"}),
        )
        client = onedrive(wire, chunk_size=1024)
        await client.upload_large_file("a.bin", b"x" * 10)

        session_request, fragment = wire.requests
        assert "authorization" in session_request.headers
        assert "authorization" not in fragment.headers

    @pytest.mark.asyncio
    async def test_the_default_chunk_size_is_a_multiple_of_320_kib(self) -> None:
        """Not arithmetic anyone re-checks — a violation fails only at the end.

        "Failing to use a fragment size that is a multiple of 320 KiB can result
        in large file transfers failing after the last byte range is uploaded."
        """
        from loom.toolsets.microsoft.onedrive.client import CHUNK_SIZE

        assert CHUNK_SIZE % (320 * 1024) == 0
        assert 5 * 1024 * 1024 <= CHUNK_SIZE <= 10 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_a_session_without_an_upload_url_fails_loudly(self) -> None:
        wire = Wire({"expirationDateTime": "z"})
        with pytest.raises(GraphPermanentError) as caught:
            await onedrive(wire).upload_large_file("a.bin", b"x")
        assert "uploadUrl" in str(caught.value)


class TestOneDriveWrites:
    @pytest.mark.asyncio
    async def test_a_move_with_nothing_to_do_is_refused(self) -> None:
        """Otherwise it is a no-op PATCH that reads as a successful move."""
        wire = Wire({"id": "1"})
        with pytest.raises(GraphPermanentError):
            await onedrive(wire).move_item("01X")
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_a_move_sends_a_parent_reference(self) -> None:
        wire = Wire({"id": "1", "name": "a"})
        await onedrive(wire).move_item("01X", parent_id="02Y", new_name="b.txt")
        assert wire.last.method == "PATCH"
        assert wire.body() == {"parentReference": {"id": "02Y"}, "name": "b.txt"}

    @pytest.mark.asyncio
    async def test_a_copy_returns_the_monitor_url_not_an_item(self) -> None:
        """Copying is asynchronous; inventing an item id would be a lie."""
        wire = Wire(
            httpx.Response(202, headers={"Location": "https://monitor/x"}, content=b"")
        )
        assert await onedrive(wire).copy_item("01X") == "https://monitor/x"

    @pytest.mark.asyncio
    async def test_a_folder_is_created_with_the_folder_facet(self) -> None:
        wire = Wire({"id": "1", "name": "New", "folder": {}})
        await onedrive(wire).create_folder("New", parent_path="Reports")
        assert wire.path() == "/v1.0/me/drive/root:/Reports:/children"
        assert wire.body() == {
            "name": "New",
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }

    @pytest.mark.asyncio
    async def test_a_delete_returns_true_on_204(self) -> None:
        wire = Wire(httpx.Response(204, content=b""))
        assert await onedrive(wire).delete_item("01X") is True
        assert wire.last.method == "DELETE"


class TestOneDriveSharing:
    @pytest.mark.asyncio
    async def test_a_share_link_defaults_to_the_organization_not_the_internet(
        self,
    ) -> None:
        """A link anyone on the internet can use is not a safe default."""
        wire = Wire({"id": "p", "link": {"webUrl": "https://x", "scope": "organization"}})
        await onedrive(wire).create_share_link("01X")
        assert wire.body() == {"type": "view", "scope": "organization"}

    @pytest.mark.asyncio
    async def test_anonymous_must_be_asked_for(self) -> None:
        wire = Wire({"id": "p", "link": {"webUrl": "https://x"}})
        await onedrive(wire).create_share_link("01X", scope="anonymous")
        assert wire.body()["scope"] == "anonymous"

    @pytest.mark.asyncio
    async def test_an_invite_maps_can_edit_to_a_role(self) -> None:
        wire = Wire({"value": [{"id": "p", "roles": ["write"]}]})
        await onedrive(wire).invite(["a@b.com"], "01X", can_edit=True, message="hi")
        body = wire.body()
        assert body["recipients"] == [{"email": "a@b.com"}]
        assert body["roles"] == ["write"]
        assert body["message"] == "hi"

    @pytest.mark.asyncio
    async def test_an_invite_defaults_to_read(self) -> None:
        wire = Wire({"value": []})
        await onedrive(wire).invite(["a@b.com"], "01X")
        assert wire.body()["roles"] == ["read"]


# ---------------------------------------------------------------------------
# SharePoint
# ---------------------------------------------------------------------------


class TestSiteAddressing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("", "/v1.0/sites/root"),
            ("root", "/v1.0/sites/root"),
            ("contoso.sharepoint.com", "/v1.0/sites/contoso.sharepoint.com"),
            (
                "contoso.sharepoint.com:/teams/hr",
                "/v1.0/sites/contoso.sharepoint.com:/teams/hr",
            ),
            (
                "contoso.sharepoint.com,aaa,bbb",
                "/v1.0/sites/contoso.sharepoint.com,aaa,bbb",
            ),
        ],
    )
    async def test_all_four_forms_pass_through_unencoded(
        self, given: str, expected: str
    ) -> None:
        """The colon and the commas are structural.

        Percent-encoding them turns a valid address into a 404 that reads as a
        missing site — and none of the four can be told apart by shape, which
        is why Graph does the disambiguation rather than this client.
        """
        wire = Wire({"id": "s"})
        await sharepoint(wire).get_site(given)
        assert wire.path() == expected

    @pytest.mark.asyncio
    async def test_a_default_site_is_used_when_none_is_given(self) -> None:
        wire = Wire({"id": "s"})
        await sharepoint(wire, default_site="host:/teams/hr").get_site()
        assert wire.path() == "/v1.0/sites/host:/teams/hr"

    @pytest.mark.asyncio
    async def test_an_explicit_site_overrides_the_default(self) -> None:
        wire = Wire({"id": "s"})
        await sharepoint(wire, default_site="host:/teams/hr").get_site("other.com")
        assert wire.path() == "/v1.0/sites/other.com"

    @pytest.mark.asyncio
    async def test_searching_sites_sends_the_search_parameter(self) -> None:
        wire = Wire({"value": []})
        await sharepoint(wire).search_sites("marketing")
        assert wire.path() == "/v1.0/sites"
        assert wire.query("search") == "marketing"


class TestSharePointLibraries:
    @pytest.mark.asyncio
    async def test_no_drive_id_uses_the_sites_default_library(self) -> None:
        wire = Wire({"value": []})
        await sharepoint(wire).list_drive_items(site="s1")
        assert wire.path() == "/v1.0/sites/s1/drive/root/children"

    @pytest.mark.asyncio
    async def test_a_named_library_is_addressed_by_drive_id(self) -> None:
        wire = Wire({"value": []})
        await sharepoint(wire).list_drive_items(drive_id="b!abc", path="Policies")
        assert wire.raw() == "/v1.0/drives/b%21abc/root:/Policies:/children"

    @pytest.mark.asyncio
    async def test_an_upload_targets_the_child_content_path(self) -> None:
        wire = Wire({"id": "1"})
        await sharepoint(wire).upload_file(
            "a.txt", b"x", drive_id="b!abc", parent_path="Shared"
        )
        assert wire.raw() == "/v1.0/drives/b%21abc/root:/Shared/a.txt:/content"

    @pytest.mark.asyncio
    async def test_a_library_file_comes_back_as_a_driveitem(self) -> None:
        """Same model as OneDrive, because it is the same resource."""
        wire = Wire({"value": [{"id": "1", "name": "a.docx", "file": {}}]})
        items = await sharepoint(wire).list_drive_items(site="s1")
        assert isinstance(items[0], DriveItem)

    @pytest.mark.asyncio
    async def test_a_download_resolves_metadata_then_fetches_by_id(self) -> None:
        wire = Wire(
            {"id": "42", "name": "policy.docx", "file": {"mimeType": "application/msword"}},
            httpx.Response(200, content=b"doc"),
        )
        attachment = await sharepoint(wire).download_file(site="s1", path="P/policy.docx")
        assert wire.path(index=0) == "/v1.0/sites/s1/drive/root:/P/policy.docx"
        assert wire.path(index=1) == "/v1.0/sites/s1/drive/items/42/content"
        assert attachment.filename == "policy.docx"


class TestSharePointLists:
    @pytest.mark.asyncio
    async def test_items_always_expand_their_fields(self) -> None:
        """Graph hides the bag by default.

        Without ``$expand=fields`` every item is ids and timestamps and no data
        — a result that looks like an empty list rather than a missing
        parameter, which is why the client never leaves it to the caller.
        """
        wire = Wire({"value": []})
        await sharepoint(wire).list_items("L1", "s1")
        assert wire.query("$expand") == "fields"

    @pytest.mark.asyncio
    async def test_a_single_item_expands_its_fields_too(self) -> None:
        wire = Wire({"id": "4", "fields": {"Title": "x"}})
        await sharepoint(wire).get_list_item("L1", "4", "s1")
        assert wire.query("$expand") == "fields"

    @pytest.mark.asyncio
    async def test_a_filter_is_passed_through(self) -> None:
        wire = Wire({"value": []})
        await sharepoint(wire).list_items(
            "L1", "s1", filter_query="fields/Status eq 'Open'"
        )
        assert wire.query("$filter") == "fields/Status eq 'Open'"

    @pytest.mark.asyncio
    async def test_creating_an_item_wraps_the_values_in_fields(self) -> None:
        wire = Wire({"id": "5", "fields": {"Title": "Widget"}})
        await sharepoint(wire).create_list_item("L1", {"Title": "Widget"}, "s1")
        assert wire.path() == "/v1.0/sites/s1/lists/L1/items"
        assert wire.body() == {"fields": {"Title": "Widget"}}

    @pytest.mark.asyncio
    async def test_creating_an_item_with_no_fields_is_refused(self) -> None:
        """An empty dict creates a blank row that reads as a successful write."""
        wire = Wire({"id": "5"})
        with pytest.raises(GraphPermanentError):
            await sharepoint(wire).create_list_item("L1", {}, "s1")
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_updating_patches_the_fields_subresource(self) -> None:
        wire = Wire({"Status": "Done"})
        await sharepoint(wire).update_list_item("L1", "7", {"Status": "Done"}, "s1")
        assert wire.last.method == "PATCH"
        assert wire.path() == "/v1.0/sites/s1/lists/L1/items/7/fields"
        assert wire.body() == {"Status": "Done"}

    @pytest.mark.asyncio
    async def test_an_update_hands_back_the_item_it_updated(self) -> None:
        """Graph answers a PATCH of /fields with the bare field set.

        A caller that just updated something reasonably expects the thing it
        updated, so the id is put back on the result.
        """
        wire = Wire({"Status": "Done"})
        item = await sharepoint(wire).update_list_item("L1", "7", {"Status": "Done"})
        assert item.id == "7"
        assert item.fields == {"Status": "Done"}

    @pytest.mark.asyncio
    async def test_hidden_lists_are_dropped_by_default(self) -> None:
        """SharePoint's internal lists are never what a workflow means."""
        wire = Wire(
            {
                "value": [
                    {"id": "1", "name": "Tasks", "list": {"hidden": False}},
                    {"id": "2", "name": "TaxonomyHiddenList", "list": {"hidden": True}},
                ]
            }
        )
        visible = await sharepoint(wire).list_lists("s1")
        assert [entry.name for entry in visible] == ["Tasks"]

    @pytest.mark.asyncio
    async def test_hidden_lists_can_be_asked_for(self) -> None:
        wire = Wire(
            {
                "value": [
                    {"id": "1", "name": "Tasks", "list": {"hidden": False}},
                    {"id": "2", "name": "Hidden", "list": {"hidden": True}},
                ]
            }
        )
        every = await sharepoint(wire).list_lists("s1", include_hidden=True)
        assert len(every) == 2

    @pytest.mark.asyncio
    async def test_columns_are_fetched_from_the_lists_own_path(self) -> None:
        wire = Wire({"value": []})
        await sharepoint(wire).list_columns("L1", "s1")
        assert wire.path() == "/v1.0/sites/s1/lists/L1/columns"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestDriveItemTranslation:
    def test_a_folder_is_recognised_by_its_facet(self) -> None:
        """There is no ``type`` field; the presence of ``folder`` is the answer."""
        assert DriveItem.from_api({"id": "1", "folder": {"childCount": 4}}).is_folder
        assert not DriveItem.from_api({"id": "1", "file": {}}).is_folder

    def test_the_parent_path_loses_its_addressing_prefix(self) -> None:
        item = DriveItem.from_api(
            {"id": "1", "parentReference": {"path": "/drive/root:/Reports/2024"}}
        )
        assert item.folder_path == "/Reports/2024"

    def test_an_item_at_the_root_reports_a_root_path(self) -> None:
        item = DriveItem.from_api(
            {"id": "1", "parentReference": {"path": "/drive/root:"}}
        )
        assert item.folder_path == "/"

    def test_an_actor_falls_through_to_an_application(self) -> None:
        """A file uploaded by a daemon has no ``user`` in its identitySet.

        Reading only ``user`` reports an empty author for every such item.
        """
        item = DriveItem.from_api(
            {"id": "1", "createdBy": {"application": {"displayName": "Sync"}}}
        )
        assert item.created_by == "Sync"

    def test_a_missing_field_does_not_break_the_model(self) -> None:
        """delta omits cTag, a deleted item omits name, $select narrows both."""
        assert DriveItem.from_api({"id": "1"}).name == ""


class TestPermissionTranslation:
    def test_a_single_grantee_is_read_from_the_v2_field(self) -> None:
        permission = Permission.from_api(
            {"id": "p", "roles": ["read"],
             "grantedToV2": {"user": {"displayName": "Ada", "email": "a@b.com"}}}
        )
        assert permission.granted_to == "Ada"
        assert permission.granted_to_email == "a@b.com"

    def test_multiple_grantees_are_not_reported_as_none(self) -> None:
        """Reading only the singular field empties every link shared by name."""
        permission = Permission.from_api(
            {"id": "p",
             "grantedToIdentitiesV2": [{"user": {"displayName": "Ada"}}]}
        )
        assert permission.granted_to == "Ada"

    def test_an_inherited_permission_says_so(self) -> None:
        permission = Permission.from_api(
            {"id": "p", "inheritedFrom": {"driveId": "d", "id": "parent"}}
        )
        assert permission.inherited is True


class TestSharingLinkTranslation:
    def test_the_url_comes_out_of_the_nested_link(self) -> None:
        link = SharingLink.from_api(
            {"id": "1", "roles": ["read"],
             "link": {"type": "view", "scope": "anonymous", "webUrl": "https://x"}}
        )
        assert link.url == "https://x"
        assert link.scope == "anonymous"


class TestSiteTranslation:
    def test_the_server_relative_path_is_derived_from_the_web_url(self) -> None:
        site = Site.from_api(
            {
                "id": "contoso.sharepoint.com,a,b",
                "displayName": "HR",
                "webUrl": "https://contoso.sharepoint.com/teams/hr",
                "siteCollection": {"hostname": "contoso.sharepoint.com"},
            }
        )
        assert site.hostname == "contoso.sharepoint.com"
        assert site.site_path == "/teams/hr"
        assert site.is_site_collection_root is True


class TestListColumnTranslation:
    """The entity-resolution hazard, which produces a wrong answer not an error."""

    def test_both_names_are_kept(self) -> None:
        """A ``fields`` bag is keyed by ``name``; a spec will say ``display_name``.

        Writing the display name is accepted and sets nothing, so the row is
        created and the value is quietly missing.
        """
        column = ListColumn.from_api(
            {"name": "Due_x0020_Date", "displayName": "Due Date", "dateTime": {}}
        )
        assert column.name == "Due_x0020_Date"
        assert column.display_name == "Due Date"
        assert column.type == "dateTime"

    def test_a_choice_column_carries_its_accepted_values(self) -> None:
        """Another vocabulary that has to be matched rather than guessed."""
        column = ListColumn.from_api(
            {"name": "Status", "displayName": "Status",
             "choice": {"choices": ["Open", "In Progress", "Done"]}}
        )
        assert column.type == "choice"
        assert column.choices == ["Open", "In Progress", "Done"]

    def test_a_read_only_column_is_flagged(self) -> None:
        column = ListColumn.from_api({"name": "Created", "readOnly": True, "dateTime": {}})
        assert column.read_only is True

    def test_a_column_with_no_recognised_facet_has_no_type(self) -> None:
        assert ListColumn.from_api({"name": "Odd"}).type == ""


class TestListItemTranslation:
    def test_odata_annotations_are_kept_out_of_the_field_bag(self) -> None:
        """Six annotations before the first real column is not a contract."""
        item = ListItem.from_api(
            {"id": "1",
             "fields": {"@odata.etag": "x", "Title": "Widget", "Color": "Purple"}}
        )
        assert item.fields == {"Title": "Widget", "Color": "Purple"}

    def test_an_unexpanded_item_reports_no_fields_rather_than_guessing(self) -> None:
        assert ListItem.from_api({"id": "1"}).fields == {}


class TestManifestsDeclareWhatTheyRead:
    """A manifest's auth fields are what ``loom toolset`` prints.

    The failure this pins is quiet and one-directional: the shared auth layer
    accepts ``AZURE_TENANT_ID``/``AZURE_CLIENT_ID``/``AZURE_CLIENT_SECRET`` —
    the trio a host running the Azure SDKs already has — and
    ``MS_AUTHORITY_HOST``, the only way to reach a national cloud. All six
    manifests originally listed none of them, so the CLI told a correctly
    configured user to set ``MS_*`` variables they did not need, and gave a US
    Gov tenant no way to discover the authority override.

    Checked as a superset rather than equality: a manifest may reasonably
    declare a variable only its own client reads.
    """

    #: Read by ``MicrosoftCredentials.from_env`` for every Microsoft toolset.
    SHARED: ClassVar[frozenset[str]] = frozenset(
        {
            "MS_TENANT_ID",
            "MS_CLIENT_ID",
            "MS_CLIENT_SECRET",
            "MS_REFRESH_TOKEN",
            "MS_GRAPH_ACCESS_TOKEN",
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "MS_AUTHORITY_HOST",
        }
    )

    def _microsoft_manifests(self):
        import importlib

        for module, attribute in (
            ("loom.toolsets.microsoft.onedrive.manifest", "ONEDRIVE_MANIFEST"),
            ("loom.toolsets.microsoft.sharepoint.manifest", "SHAREPOINT_MANIFEST"),
            ("loom.toolsets.microsoft.teams.manifest", "TEAMS_MANIFEST"),
            ("loom.toolsets.microsoft.onenote.manifest", "ONENOTE_MANIFEST"),
            ("loom.toolsets.microsoft.outlook.mail.manifest", "OUTLOOK_MAIL_MANIFEST"),
            (
                "loom.toolsets.microsoft.outlook.calendar.manifest",
                "OUTLOOK_CALENDAR_MANIFEST",
            ),
        ):
            yield getattr(importlib.import_module(module), attribute)

    def test_every_microsoft_manifest_declares_the_shared_credentials(self) -> None:
        missing = {}
        for manifest in self._microsoft_manifests():
            absent = self.SHARED - set(manifest.auth.get("fields", []))
            if absent:
                missing[manifest.id] = sorted(absent)
        assert not missing, f"manifests omit credentials the auth layer reads: {missing}"

    def test_the_shared_set_matches_what_from_env_actually_reads(self) -> None:
        """Guards the guard: if ``from_env`` learns a variable, this fails.

        Without it the list above becomes a second source of truth that drifts
        from the code the moment a credential shape is added.
        """
        import inspect

        from loom.toolsets.microsoft import auth as auth_module

        source = inspect.getsource(auth_module.MicrosoftCredentials.from_env)
        read = set(re.findall(r'"((?:MS|AZURE)_[A-Z_]+)"', source))
        assert read == self.SHARED, (
            "MicrosoftCredentials.from_env reads a different set than this test "
            f"declares; extra in code: {sorted(read - self.SHARED)}, "
            f"stale here: {sorted(self.SHARED - read)}"
        )
