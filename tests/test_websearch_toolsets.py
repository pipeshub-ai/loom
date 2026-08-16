"""The three web-search toolsets: Exa, Tavily, DuckDuckGo.

They exist to answer the same question and answer it with three different
contracts, so what is worth testing is the places those contracts differ and
the places this code deliberately departs from the API underneath.

Four themes run through the file:

**A ceiling is refused, never hidden.** Neither Exa nor Tavily paginates, so a
request for more results than one call returns cannot be satisfied at all.
Silently capping is the tempting behaviour and produces a workflow that asked
for 500, received 100, and reports 100 as the total.

**Partial success is carried, not dropped.** Exa's `/contents` and Tavily's
`/extract` both answer 200 for a request in which some URLs failed. A caller
reading only the results list sees a short list and no reason to doubt it.

**A block is an error, not an empty list.** DuckDuckGo is scraped, so being
turned away is routine; reported as zero results it becomes "nothing matched",
and the workflow acts on it.

**Quota is not rate limiting.** Tavily's 432/433 clear when somebody changes a
billing plan, so retrying spends the budget to learn nothing — they are
classified apart from 429, which does clear by waiting.

No network and no credentials: the HTTP clients are driven through a fake
transport, and the DuckDuckGo package is faked at the seam where it is
imported.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from loom.core.exceptions import ConfigurationError, NonRetryableError
from loom.toolsets.duckduckgo import client as ddg_client
from loom.toolsets.duckduckgo.client import (
    DDG_MAX_PAGES,
    DDG_PAGE_SIZE,
    DuckDuckGoClient,
    DuckDuckGoError,
    DuckDuckGoPermanentError,
    DuckDuckGoRateLimited,
)
from loom.toolsets.duckduckgo.manifest import DUCKDUCKGO_MANIFEST
from loom.toolsets.exa.client import (
    ExaAuthError,
    ExaClient,
    ExaCreditsExhausted,
    ExaPermanentError,
    ExaRateLimited,
)
from loom.toolsets.exa.manifest import EXA_MANIFEST
from loom.toolsets.tavily.client import (
    TavilyAuthError,
    TavilyClient,
    TavilyPermanentError,
    TavilyQuotaExhausted,
    TavilyRateLimited,
)
from loom.toolsets.tavily.manifest import TAVILY_MANIFEST

# ---------------------------------------------------------------------------
# A fake HTTP transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, payload: Any, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"x" if payload is not None else b""

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, Exception):
            return "<html>502 Bad Gateway</html>"
        return json.dumps(self._payload) if self._payload is not None else ""


class FakeHttp:
    """Stands in for ``httpx.AsyncClient``, recording what was sent."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> FakeHttp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.calls.append((url, headers, json))
        return self.response

    @property
    def body(self) -> dict:
        """The JSON body of the last request."""
        return self.calls[-1][2]

    @property
    def headers(self) -> dict:
        return self.calls[-1][1]


@pytest.fixture
def http(monkeypatch):
    """Route both clients' httpx through one fake, set per test."""
    import httpx

    holder: dict[str, FakeHttp] = {}

    def install(status: int = 200, payload: Any = None, headers: dict | None = None):
        fake = FakeHttp(FakeResponse(status, payload if payload is not None else {}, headers))
        holder["fake"] = fake
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
        return fake

    return install


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------


SEARCH_PAYLOAD = {
    "requestId": "req-1",
    "results": [
        {
            "id": "doc-1",
            "url": "https://example.com/a",
            "title": "A",
            "publishedDate": "2026-01-02T00:00:00.000Z",
            "author": "Ada",
            "score": 0.91,
            "highlights": ["a snippet"],
        }
    ],
    "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
}


class TestExaAuth:
    def test_a_missing_key_fails_at_construction(self, monkeypatch) -> None:
        """Not on the first request, five frames into a step."""
        monkeypatch.delenv("EXA_API_KEY", raising=False)

        with pytest.raises(ExaAuthError, match="EXA_API_KEY"):
            ExaClient()

    def test_the_key_goes_in_the_header_exa_documents(self, http) -> None:
        """``x-api-key``, not a bearer token."""
        import asyncio

        fake = http(200, SEARCH_PAYLOAD)
        asyncio.run(ExaClient(api_key="k").search("hello"))

        assert fake.headers["x-api-key"] == "k"

    def test_an_explicit_key_beats_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("EXA_API_KEY", "from-env")
        assert ExaClient(api_key="explicit")._key == "explicit"


class TestExaSearch:
    async def test_it_maps_the_camel_case_wire_shape(self, http) -> None:
        http(200, SEARCH_PAYLOAD)

        results = await ExaClient(api_key="k").search("q")

        assert len(results) == 1
        assert results[0].url == "https://example.com/a"
        # publishedDate -> published_date, and only here.
        assert results[0].published_date == "2026-01-02T00:00:00.000Z"
        assert results[0].score == 0.91
        assert results[0].highlights == ["a snippet"]

    async def test_unset_filters_are_omitted_not_sent_as_null(self, http) -> None:
        """Exa rejects nulls for several of these."""
        fake = http(200, SEARCH_PAYLOAD)

        await ExaClient(api_key="k").search("q")

        assert "category" not in fake.body
        assert "includeDomains" not in fake.body
        # An empty contents object costs a crawl for content nobody wanted.
        assert "contents" not in fake.body

    async def test_asking_for_content_builds_the_nested_object(self, http) -> None:
        fake = http(200, SEARCH_PAYLOAD)

        await ExaClient(api_key="k").search(
            "q", include_text=True, max_characters=500, summary_query="what changed?"
        )

        assert fake.body["contents"]["text"] == {"maxCharacters": 500}
        assert fake.body["contents"]["summary"] == {"query": "what changed?"}


class TestExaRefusesToHideItsCeiling:
    """Exa has no cursor, so an over-large request cannot be made whole."""

    async def test_over_the_cap_raises_rather_than_returning_the_cap(
        self, http
    ) -> None:
        http(200, SEARCH_PAYLOAD)

        with pytest.raises(ExaPermanentError, match="does not paginate"):
            await ExaClient(api_key="k").search("q", num_results=500)

    async def test_the_error_says_what_to_do_instead(self, http) -> None:
        http(200, SEARCH_PAYLOAD)

        with pytest.raises(ExaPermanentError, match="100 or fewer"):
            await ExaClient(api_key="k").search("q", num_results=101)

    async def test_exactly_the_cap_is_fine(self, http) -> None:
        fake = http(200, SEARCH_PAYLOAD)

        await ExaClient(api_key="k").search("q", num_results=100)

        assert fake.body["numResults"] == 100

    async def test_it_is_a_permanent_error_so_retry_stops(self, http) -> None:
        """Retrying an impossible request three times is three ways to fail."""
        http(200, SEARCH_PAYLOAD)

        with pytest.raises(NonRetryableError):
            await ExaClient(api_key="k").search("q", num_results=500)


class TestExaFindSimilar:
    async def test_it_excludes_the_source_domain_by_default(self, http) -> None:
        """Unlike the API. 'More like this' means more from elsewhere."""
        fake = http(200, SEARCH_PAYLOAD)

        await ExaClient(api_key="k").find_similar("https://example.com/a")

        assert fake.body["excludeSourceDomain"] is True


class TestExaContentsCarriesPartialFailure:
    """200 with some URLs missing is the shape this has to survive."""

    PAYLOAD: ClassVar[dict] = {
        "requestId": "req-2",
        "results": [{"id": "1", "url": "https://ok.com", "text": "hello"}],
        "statuses": [
            {"id": "https://ok.com", "status": "success", "source": "cached"},
            {
                "id": "https://gone.com",
                "status": "error",
                "error": {"tag": "CRAWL_NOT_FOUND", "httpStatusCode": 404},
            },
        ],
    }

    async def test_the_failures_survive_to_the_caller(self, http) -> None:
        http(200, self.PAYLOAD)

        contents = await ExaClient(api_key="k").get_contents(
            ["https://ok.com", "https://gone.com"]
        )

        assert len(contents.results) == 1
        assert [f.id for f in contents.failed] == ["https://gone.com"]
        assert contents.failed[0].error_tag == "CRAWL_NOT_FOUND"
        assert contents.failed[0].http_status == 404

    async def test_a_successful_status_is_not_reported_as_failed(self, http) -> None:
        http(200, self.PAYLOAD)

        contents = await ExaClient(api_key="k").get_contents(["https://ok.com"])

        assert [s.id for s in contents.statuses if s.ok] == ["https://ok.com"]

    async def test_too_many_urls_is_refused_with_the_fix(self, http) -> None:
        http(200, self.PAYLOAD)

        with pytest.raises(ExaPermanentError, match="Split the list"):
            await ExaClient(api_key="k").get_contents(
                [f"https://e.com/{i}" for i in range(101)]
            )

    async def test_no_urls_is_refused(self, http) -> None:
        http(200, self.PAYLOAD)

        with pytest.raises(ExaPermanentError):
            await ExaClient(api_key="k").get_contents([])


class TestExaErrorsAreClassified:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, ExaAuthError),
            (403, ExaAuthError),
            (400, ExaPermanentError),
            (429, ExaRateLimited),
            (402, ExaCreditsExhausted),
        ],
    )
    async def test_each_status_gets_the_narrowest_error(
        self, http, status, expected
    ) -> None:
        http(status, {"error": "nope"})

        with pytest.raises(expected):
            await ExaClient(api_key="k").search("q")

    async def test_running_out_of_credits_is_permanent(self, http) -> None:
        """It clears when somebody tops up an account, not by asking again."""
        http(402, {"error": "insufficient credits"})

        with pytest.raises(NonRetryableError):
            await ExaClient(api_key="k").search("q")

    async def test_a_rate_limit_stays_retryable(self, http) -> None:
        """The opposite call from 402, and the reason they are separate."""
        http(429, {"error": "slow down"}, {"Retry-After": "7"})

        with pytest.raises(ExaRateLimited) as caught:
            await ExaClient(api_key="k").search("q")

        assert not isinstance(caught.value, NonRetryableError)
        assert caught.value.retry_after == 7.0

    async def test_a_5xx_stays_retryable(self, http) -> None:
        http(503, {"error": "later"})

        with pytest.raises(Exception) as caught:
            await ExaClient(api_key="k").search("q")

        assert not isinstance(caught.value, NonRetryableError)

    async def test_an_unparseable_body_still_produces_a_useful_message(
        self, http
    ) -> None:
        """A gateway's HTML 502 must not become a JSONDecodeError."""
        http(502, ValueError("not json"))

        with pytest.raises(Exception, match="Exa 502"):
            await ExaClient(api_key="k").search("q")


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


TAVILY_PAYLOAD = {
    "query": "who bought Wiz",
    "answer": "Google.",
    "results": [
        {
            "title": "T",
            "url": "https://news.example/1",
            "content": "an excerpt",
            "score": 0.8,
            "published_date": "2026-03-01",
        }
    ],
    "images": ["https://img.example/1.png", {"url": "https://img.example/2.png"}],
    "response_time": 1.5,
    "request_id": "req-3",
}


class TestTavilyAuth:
    def test_a_missing_key_fails_at_construction(self, monkeypatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        with pytest.raises(TavilyAuthError, match="TAVILY_API_KEY"):
            TavilyClient()

    def test_the_key_is_sent_as_a_bearer_token(self, http) -> None:
        import asyncio

        fake = http(200, TAVILY_PAYLOAD)
        asyncio.run(TavilyClient(api_key="tvly-x").search("q"))

        assert fake.headers["Authorization"] == "Bearer tvly-x"


class TestTavilySearch:
    async def test_the_answer_arrives_beside_the_results(self, http) -> None:
        """The reason this returns a wrapper rather than a bare list."""
        http(200, TAVILY_PAYLOAD)

        found = await TavilyClient(api_key="k").search("q", include_answer=True)

        assert found.answer == "Google."
        assert len(found.results) == 1

    async def test_content_is_exposed_as_snippet(self, http) -> None:
        """`content` beside `raw_content` invites the wrong reading."""
        http(200, TAVILY_PAYLOAD)

        found = await TavilyClient(api_key="k").search("q")

        assert found.results[0].snippet == "an excerpt"
        assert found.results[0].raw_content == ""

    async def test_images_normalise_whether_or_not_described(self, http) -> None:
        """Bare strings normally, objects when descriptions were asked for."""
        http(200, TAVILY_PAYLOAD)

        found = await TavilyClient(api_key="k").search("q")

        assert found.images == [
            "https://img.example/1.png",
            "https://img.example/2.png",
        ]

    async def test_falsey_flags_are_omitted_so_tavily_uses_its_defaults(
        self, http
    ) -> None:
        fake = http(200, TAVILY_PAYLOAD)

        await TavilyClient(api_key="k").search("q")

        assert "include_answer" not in fake.body
        assert "time_range" not in fake.body


class TestTavilyRefusesToHideItsCeiling:
    async def test_over_twenty_raises(self, http) -> None:
        http(200, TAVILY_PAYLOAD)

        with pytest.raises(TavilyPermanentError, match="does not paginate"):
            await TavilyClient(api_key="k").search("q", max_results=100)

    async def test_exactly_twenty_is_fine(self, http) -> None:
        fake = http(200, TAVILY_PAYLOAD)

        await TavilyClient(api_key="k").search("q", max_results=20)

        assert fake.body["max_results"] == 20

    async def test_an_over_long_domain_filter_fails_here_not_as_an_opaque_400(
        self, http
    ) -> None:
        http(200, TAVILY_PAYLOAD)

        with pytest.raises(TavilyPermanentError, match="include_domains"):
            await TavilyClient(api_key="k").search(
                "q", include_domains=[f"d{i}.com" for i in range(301)]
            )


class TestTavilyExtract:
    PAYLOAD: ClassVar[dict] = {
        "results": [{"url": "https://ok.com", "raw_content": "hello"}],
        "failed_results": [{"url": "https://gone.com", "error": "timed out"}],
        "response_time": 0.4,
    }

    async def test_failures_survive_to_the_caller(self, http) -> None:
        http(200, self.PAYLOAD)

        got = await TavilyClient(api_key="k").extract(
            ["https://ok.com", "https://gone.com"]
        )

        assert [p.url for p in got.pages] == ["https://ok.com"]
        assert [f.url for f in got.failed] == ["https://gone.com"]
        assert got.failed[0].error == "timed out"

    async def test_read_timeout_is_sent_as_the_api_s_own_timeout(self, http) -> None:
        """Renamed at the tool boundary only — the wire name is unchanged.

        ``ctx.step`` claims ``timeout``, so a tool declaring it is unreachable
        by keyword. The rename must not leak into the request.
        """
        fake = http(200, self.PAYLOAD)

        await TavilyClient(api_key="k").extract(["https://ok.com"], read_timeout=12.0)

        assert fake.body["timeout"] == 12.0

    async def test_too_many_urls_is_refused(self, http) -> None:
        http(200, self.PAYLOAD)

        with pytest.raises(TavilyPermanentError, match="Split the list"):
            await TavilyClient(api_key="k").extract(
                [f"https://e.com/{i}" for i in range(21)]
            )


class TestTavilyMap:
    async def test_it_stays_on_the_site_by_default(self, http) -> None:
        """Unlike the API, whose default wanders off and returns URLs that
        are indistinguishable from the ones that belong."""
        fake = http(200, {"base_url": "https://d.example", "results": []})

        await TavilyClient(api_key="k").map_site("https://d.example")

        assert fake.body["allow_external"] is False

    async def test_the_urls_come_back_as_strings(self, http) -> None:
        http(
            200,
            {
                "base_url": "https://d.example",
                "results": ["https://d.example/a", "https://d.example/b"],
            },
        )

        site = await TavilyClient(api_key="k").map_site("https://d.example")

        assert site.urls == ["https://d.example/a", "https://d.example/b"]


class TestTavilyQuotaIsNotRateLimiting:
    """432 and 433 are not in anybody's default 4xx vocabulary."""

    @pytest.mark.parametrize("status", [432, 433])
    async def test_a_spent_plan_is_permanent(self, http, status) -> None:
        http(status, {"detail": {"error": "usage limit exceeded"}})

        with pytest.raises(TavilyQuotaExhausted) as caught:
            await TavilyClient(api_key="k").search("q")

        # The whole point of the separate class: waiting does not help, so the
        # retry budget should not be spent discovering that.
        assert isinstance(caught.value, NonRetryableError)

    async def test_a_rate_limit_is_not(self, http) -> None:
        http(429, {"detail": {"error": "slow down"}}, {"Retry-After": "3"})

        with pytest.raises(TavilyRateLimited) as caught:
            await TavilyClient(api_key="k").search("q")

        assert not isinstance(caught.value, NonRetryableError)
        assert caught.value.retry_after == 3.0

    async def test_the_nested_detail_shape_reaches_the_message(self, http) -> None:
        http(400, {"detail": {"error": "query is required"}})

        with pytest.raises(TavilyPermanentError, match="query is required"):
            await TavilyClient(api_key="k").search("q")


# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------


class FakeDDGS:
    """Stands in for ``ddgs.DDGS``.

    ``pages`` is a list of the raw row-lists to return, one per call. Anything
    in it that is an exception is raised instead, which is how a rate limit
    mid-walk is expressed.
    """

    def __init__(self, pages: list[Any]) -> None:
        self.pages = list(pages)
        self.calls: list[dict] = []

    def _next(self, query: str, **kwargs: Any) -> list[dict]:
        self.calls.append({"query": query, **kwargs})
        page = self.pages.pop(0) if self.pages else []
        if isinstance(page, Exception):
            raise page
        return page

    text = news = images = _next


def rows(count: int, offset: int = 0) -> list[dict]:
    return [
        {
            "title": f"t{offset + i}",
            "href": f"https://e.com/{offset + i}",
            "body": f"b{offset + i}",
        }
        for i in range(count)
    ]


@pytest.fixture
def ddgs(monkeypatch):
    """Install a fake ``ddgs`` client, returning the recorder."""

    def install(pages: list[Any]) -> FakeDDGS:
        fake = FakeDDGS(pages)
        monkeypatch.setattr(DuckDuckGoClient, "_ddgs", lambda self: fake)
        return fake

    return install


class TestDuckDuckGoPaging:
    """The reason these return ``Results`` at all."""

    async def test_it_follows_pages_to_reach_the_limit(self, ddgs) -> None:
        fake = ddgs([rows(DDG_PAGE_SIZE), rows(DDG_PAGE_SIZE, offset=DDG_PAGE_SIZE)])

        found = await DuckDuckGoClient().search("q", max_results=25)

        assert len(found) == 25
        assert [c["page"] for c in fake.calls] == [1, 2]

    async def test_a_short_page_means_the_source_ran_out(self, ddgs) -> None:
        """``complete=True`` with fewer rows than asked for is the answer
        ``ddgs`` cannot give: 24-of-30 is genuinely all there is."""
        ddgs([rows(DDG_PAGE_SIZE), rows(4, offset=DDG_PAGE_SIZE)])

        found = await DuckDuckGoClient().search("q", max_results=30)

        assert len(found) == 24
        assert found.complete is True

    async def test_more_behind_it_is_reported_with_a_cursor(self, ddgs) -> None:
        ddgs([rows(DDG_PAGE_SIZE), rows(DDG_PAGE_SIZE, offset=DDG_PAGE_SIZE)])

        found = await DuckDuckGoClient().search("q", max_results=25)

        assert found.complete is False
        assert found.cursor == "3"

    async def test_a_fixed_page_size_is_requested_every_time(self, ddgs) -> None:
        """Varying it would shift the upstream window and skip or repeat rows,
        because ``page`` maps onto the engine's own pagination."""
        fake = ddgs([rows(DDG_PAGE_SIZE), rows(DDG_PAGE_SIZE, offset=DDG_PAGE_SIZE)])

        await DuckDuckGoClient().search("q", max_results=22)

        assert {c["max_results"] for c in fake.calls} == {DDG_PAGE_SIZE}

    async def test_repeats_between_pages_are_dropped(self, ddgs) -> None:
        """Ordinary for a scraped source, and not the caller's problem."""
        ddgs([rows(DDG_PAGE_SIZE), rows(DDG_PAGE_SIZE)])

        found = await DuckDuckGoClient().search("q", max_results=40)

        assert len({r.url for r in found}) == len(found) == DDG_PAGE_SIZE

    async def test_a_page_of_pure_repeats_ends_the_walk(self, ddgs) -> None:
        """A full page offering nothing new is how a scraped source says it is
        done. Asking again is far likelier to loop than to find fresh rows, so
        the walk stops there rather than spending the page budget on it."""
        fake = ddgs(
            [rows(DDG_PAGE_SIZE), rows(DDG_PAGE_SIZE), rows(5, offset=DDG_PAGE_SIZE)]
        )

        found = await DuckDuckGoClient().search("q", max_results=40)

        assert [c["page"] for c in fake.calls] == [1, 2]
        assert len(found) == DDG_PAGE_SIZE
        assert found.complete is True

    async def test_the_page_budget_bounds_a_source_that_keeps_answering(
        self, ddgs
    ) -> None:
        """A scraped source that always says 'more' is not evidence of more."""
        ddgs([rows(DDG_PAGE_SIZE, offset=i * DDG_PAGE_SIZE) for i in range(50)])

        found = await DuckDuckGoClient().search("q", max_results=10_000)

        assert len(found) == DDG_MAX_PAGES * DDG_PAGE_SIZE
        assert found.complete is False

    async def test_zero_results_is_complete_and_empty(self, ddgs) -> None:
        ddgs([[]])

        found = await DuckDuckGoClient().search("q", max_results=10)

        assert list(found) == []
        assert found.complete is True


class TestDuckDuckGoMapsItsRows:
    async def test_web_rows_use_href(self, ddgs) -> None:
        ddgs([[{"title": "T", "href": "https://e.com/1", "body": "snip"}]])

        found = await DuckDuckGoClient().search("q", max_results=5)

        assert found[0].url == "https://e.com/1"
        assert found[0].snippet == "snip"

    async def test_news_rows_use_url_and_carry_a_date(self, ddgs) -> None:
        ddgs(
            [
                [
                    {
                        "title": "N",
                        "url": "https://n.com/1",
                        "body": "snip",
                        "date": "2026-08-14T14:42:48+00:00",
                        "source": "Fortune",
                    }
                ]
            ]
        )

        found = await DuckDuckGoClient().news("q", max_results=5)

        assert found[0].url == "https://n.com/1"
        assert found[0].date == "2026-08-14T14:42:48+00:00"
        assert found[0].source == "Fortune"

    async def test_image_dimensions_coerce_from_strings(self, ddgs) -> None:
        ddgs(
            [
                [
                    {
                        "title": "I",
                        "image": "https://i.com/a.png",
                        "url": "https://page.com",
                        "width": "800",
                        "height": "600",
                    }
                ]
            ]
        )

        found = await DuckDuckGoClient().images("q", max_results=5)

        assert found[0].image == "https://i.com/a.png"
        # The page it appears on, not the image. Confusing the two is the
        # usual mistake, so both are asserted.
        assert found[0].url == "https://page.com"
        assert (found[0].width, found[0].height) == (800, 600)


class TestABlockIsAnErrorNotAnEmptyList:
    """The failure that makes a scraped source dangerous rather than flaky."""

    async def test_a_rate_limit_raises(self, ddgs) -> None:
        from ddgs.exceptions import RatelimitException

        ddgs([RatelimitException("blocked")])

        with pytest.raises(DuckDuckGoRateLimited):
            await DuckDuckGoClient().search("q", max_results=5)

    async def test_a_rate_limit_stays_retryable(self, ddgs) -> None:
        """Backing off is usually all it takes, so the retry must survive."""
        from ddgs.exceptions import RatelimitException

        ddgs([RatelimitException("blocked")])

        with pytest.raises(DuckDuckGoRateLimited) as caught:
            await DuckDuckGoClient().search("q", max_results=5)

        assert not isinstance(caught.value, NonRetryableError)

    async def test_the_message_names_the_supported_alternatives(self, ddgs) -> None:
        from ddgs.exceptions import RatelimitException

        ddgs([RatelimitException("blocked")])

        with pytest.raises(DuckDuckGoRateLimited, match="exa or tavily"):
            await DuckDuckGoClient().search("q", max_results=5)

    async def test_a_timeout_is_retryable(self, ddgs) -> None:
        from ddgs.exceptions import TimeoutException

        ddgs([TimeoutException("slow")])

        with pytest.raises(DuckDuckGoError) as caught:
            await DuckDuckGoClient().search("q", max_results=5)

        assert not isinstance(caught.value, NonRetryableError)

    async def test_any_other_ddgs_failure_is_permanent(self, ddgs) -> None:
        from ddgs.exceptions import DDGSException

        ddgs([DDGSException("no such backend")])

        with pytest.raises(DuckDuckGoPermanentError):
            await DuckDuckGoClient().search("q", max_results=5)

    async def test_a_block_partway_through_does_not_silently_truncate(
        self, ddgs
    ) -> None:
        """The subtle version. One good page then a block could plausibly be
        reported as a short-but-complete result; it must not be."""
        from ddgs.exceptions import RatelimitException

        ddgs([rows(DDG_PAGE_SIZE), RatelimitException("blocked")])

        with pytest.raises(DuckDuckGoRateLimited):
            await DuckDuckGoClient().search("q", max_results=40)


class TestDuckDuckGoWithoutThePackage:
    def test_a_missing_dependency_names_the_extra_and_the_tradeoff(
        self, monkeypatch
    ) -> None:
        """It is an optional extra, so this is the common first experience."""
        import builtins

        real = builtins.__import__

        def blocked(name, *args, **kw):
            if name == "ddgs":
                raise ImportError("no module named ddgs")
            return real(name, *args, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked)

        with pytest.raises(ConfigurationError) as caught:
            DuckDuckGoClient()._ddgs()

        assert "loomflow[duckduckgo]" in str(caught.value)
        assert "exa or tavily" in str(caught.value)


class TestDuckDuckGoRunsOffTheEventLoop:
    async def test_the_blocking_package_does_not_stall_the_loop(
        self, monkeypatch
    ) -> None:
        """``ddgs`` spends seconds in synchronous HTTP. Run inline it would
        stall every other step sharing this loop."""
        import asyncio
        import threading

        caller: dict[str, int] = {}

        class Blocking:
            def text(self, query, **kw):
                caller["thread"] = threading.get_ident()
                return []

        monkeypatch.setattr(DuckDuckGoClient, "_ddgs", lambda self: Blocking())

        await DuckDuckGoClient().search("q", max_results=5)

        assert caller["thread"] != threading.get_ident()
        assert asyncio.get_running_loop() is not None


# ---------------------------------------------------------------------------
# What the three declare about themselves
# ---------------------------------------------------------------------------


class TestTheManifestsAreHonest:
    """`test_manifest_imports.py` checks these against the client generically;
    what is asserted here is the *content* of the claims, which is what a
    coding agent reads when choosing between them."""

    @pytest.mark.parametrize(
        "manifest", [EXA_MANIFEST, TAVILY_MANIFEST, DUCKDUCKGO_MANIFEST]
    )
    def test_every_operation_is_a_read(self, manifest) -> None:
        """Search is the canonical taint source: a run that has searched the
        web holds text nobody reviewed. Mislabelled as a write, no read could
        taint and the rule would be unreachable."""
        for spec in manifest.all_operations():
            assert spec.effect.value == "read", spec.id
            assert spec.idempotent, spec.id

    @pytest.mark.parametrize("manifest", [EXA_MANIFEST, TAVILY_MANIFEST])
    def test_the_unpaged_apis_say_so(self, manifest) -> None:
        """Neither has a cursor. Claiming otherwise would tell the agent to
        look for a page that does not exist."""
        assert not any(spec.pagination for spec in manifest.all_operations())

    def test_duckduckgo_declares_that_it_pages(self) -> None:
        assert all(spec.pagination for spec in DUCKDUCKGO_MANIFEST.all_operations())

    def test_duckduckgo_says_plainly_that_it_is_not_an_official_api(self) -> None:
        """The agent picks between integrations from these descriptions, so
        'best-effort, no key' versus 'supported, needs a key' has to be in
        front of it."""
        description = DUCKDUCKGO_MANIFEST.description
        assert "NOT AN OFFICIAL API" in description
        assert "loomflow[duckduckgo]" in description

    @pytest.mark.parametrize(
        ("manifest", "variable"),
        [(EXA_MANIFEST, "EXA_API_KEY"), (TAVILY_MANIFEST, "TAVILY_API_KEY")],
    )
    def test_the_credential_is_named(self, manifest, variable) -> None:
        assert manifest.auth["fields"] == [variable]

    def test_duckduckgo_declares_no_credential(self) -> None:
        assert DUCKDUCKGO_MANIFEST.auth["type"] == "none"

    @pytest.mark.parametrize(
        ("manifest", "host"),
        [
            (EXA_MANIFEST, "api.exa.ai"),
            (TAVILY_MANIFEST, "api.tavily.com"),
            (DUCKDUCKGO_MANIFEST, "duckduckgo.com"),
        ],
    )
    def test_egress_is_declared_for_a_sandboxed_host(self, manifest, host) -> None:
        assert host in manifest.egress_hosts


class TestNoToolTakesAnArgumentCtxStepClaims:
    """``ctx.step`` reserves name/retry/timeout/on_error/fallback.

    Tavily's own parameter is called ``timeout``; a tool exposing it would be
    silently unreachable by keyword, so it is ``read_timeout`` here. This is
    the check that keeps the rename from being undone.
    """

    SHADOWED: ClassVar[frozenset[str]] = frozenset(
        {"name", "retry", "timeout", "on_error", "fallback"}
    )

    @pytest.mark.parametrize(
        "module",
        [
            "loom.toolsets.exa.tools",
            "loom.toolsets.tavily.tools",
            "loom.toolsets.duckduckgo.tools",
        ],
    )
    def test_no_parameter_is_shadowed(self, module) -> None:
        import importlib
        import inspect

        loaded = importlib.import_module(module)
        for name in dir(loaded):
            tool = getattr(loaded, name)
            fn = getattr(tool, "fn", None)
            if fn is None or not callable(fn):
                continue
            taken = set(inspect.signature(fn).parameters) & self.SHADOWED
            assert not taken, f"{name} declares {taken}"


class TestTheDefaultClientIsCachedPerModule:
    def test_it_is_built_once(self, monkeypatch) -> None:
        monkeypatch.setattr(ddg_client, "_default_client", None)

        first = ddg_client.get_default_client()

        assert ddg_client.get_default_client() is first
