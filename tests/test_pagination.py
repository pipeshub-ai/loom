"""Following every page, and admitting when you did not.

The bug these cover is the quietest kind. Ask Jira for 500 issues and it
returns 100, with a 200 OK and no field saying it truncated. The workflow then
reports on a fifth of the data and reads exactly as if that were all of it —
and it passes every test written against a single-page fixture, because a
fixture never has a second page.

So every test here serves **more rows than one page holds**. A test that does
not cannot tell a paging loop from a single request.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from loom.toolsets.confluence.client import ConfluenceClient
from loom.toolsets.jira.client import JiraClient
from loom.toolsets.pagination import Page, Results, collect

# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _source(total: int, *, page_size: int, cap: int | None = None):
    """A paged source of ``range(total)``, recording what it was asked."""
    calls: list[tuple[str | None, int]] = []

    async def fetch(cursor: str | None, size: int) -> Page:
        calls.append((cursor, size))
        start = int(cursor or 0)
        served = min(size, page_size if cap is None else cap)
        rows = list(range(start, min(start + served, total)))
        nxt = start + len(rows)
        return Page(rows, str(nxt) if nxt < total else None, total=total)

    return fetch, calls


async def test_it_follows_pages_until_the_source_runs_out() -> None:
    fetch, calls = _source(7, page_size=3)

    found = await collect(fetch, limit=100, page_size=3)

    assert list(found) == list(range(7))
    assert found.complete
    assert len(calls) == 3


async def test_a_short_answer_is_marked_incomplete() -> None:
    """The half that matters: the caller can tell it did not see everything."""
    fetch, _ = _source(500, page_size=100)

    found = await collect(fetch, limit=50, page_size=100)

    assert len(found) == 50
    assert not found.complete
    assert found.truncated
    assert found.summary() == "50 of 500"


async def test_exhausting_the_source_reports_complete() -> None:
    fetch, _ = _source(4, page_size=100)

    found = await collect(fetch, limit=50, page_size=100)

    assert found.complete
    assert found.summary() == "4 results"


async def test_a_server_page_cap_does_not_shorten_the_answer() -> None:
    """The actual bug: asking for 250 when the server gives 100 a time."""
    fetch, calls = _source(1000, page_size=100, cap=100)

    found = await collect(fetch, limit=250, page_size=100)

    assert len(found) == 250
    assert not found.complete
    assert len(calls) == 3


async def test_a_cursor_with_no_rows_behind_it_ends_the_loop() -> None:
    """A server that always says "more" would otherwise spin forever."""
    calls = 0

    async def fetch(cursor: str | None, size: int) -> Page:
        nonlocal calls
        calls += 1
        return Page([], cursor="always-more")

    found = await collect(fetch, limit=100, page_size=10)

    assert list(found) == []
    assert found.complete
    assert calls == 1


async def test_the_page_ceiling_stops_a_runaway_and_says_so() -> None:
    async def fetch(cursor: str | None, size: int) -> Page:
        return Page([1], cursor="more")

    found = await collect(fetch, limit=10_000, page_size=1, max_pages=5)

    assert len(found) == 5
    assert not found.complete, "a capped collection must not claim completeness"


async def test_asking_for_nothing_costs_no_request() -> None:
    fetch, calls = _source(100, page_size=10)

    assert list(await collect(fetch, limit=0, page_size=10)) == []
    assert calls == []


async def test_results_is_a_list_everywhere_it_needs_to_be() -> None:
    """Existing callers must not notice. That is why it subclasses list."""
    found = Results([1, 2, 3], complete=False, total=9)

    assert isinstance(found, list)
    assert len(found) == 3
    assert found[0] == 1
    assert [x for x in found] == [1, 2, 3]
    assert json.loads(json.dumps(found)) == [1, 2, 3]


def test_summary_distinguishes_all_of_them_from_some_of_them() -> None:
    assert Results([1]).summary() == "1 result"
    assert Results([1, 2]).summary() == "2 results"
    assert Results([1, 2], complete=False, total=99).summary() == "2 of 99"
    assert Results([1, 2], complete=False).summary() == "first 2 (more available)"


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


@pytest.fixture
def jira_search(monkeypatch: pytest.MonkeyPatch):
    """A Jira whose /search/jql caps every page at 100, as the real one does."""
    requests: list[dict[str, Any]] = []
    matching = 250

    async def fake_post(self, path: str, json: dict[str, Any]) -> Any:
        requests.append(json)
        start = int(json.get("nextPageToken") or 0)
        size = min(json.get("maxResults", 50), 100)
        rows = [
            {"key": f"PA-{i}", "fields": {"summary": f"issue {i}"}}
            for i in range(start, min(start + size, matching))
        ]
        nxt = start + len(rows)
        return {
            "issues": rows,
            "total": matching,
            "isLast": nxt >= matching,
            "nextPageToken": str(nxt),
        }

    monkeypatch.setattr(JiraClient, "_post", fake_post)
    return requests


async def test_jira_search_returns_everything_that_was_asked_for(jira_search) -> None:
    """Before this, asking for 250 returned 100 and said nothing."""
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    issues = await client.search_issues("project = PA", max_results=250)

    assert len(issues) == 250
    assert issues.complete
    assert len(jira_search) == 3
    assert issues[0].key == "PA-0"
    assert issues[-1].key == "PA-249"


async def test_jira_search_passes_the_token_back(jira_search) -> None:
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    await client.search_issues("project = PA", max_results=250)

    assert "nextPageToken" not in jira_search[0]
    assert jira_search[1]["nextPageToken"] == "100"
    assert jira_search[2]["nextPageToken"] == "200"


async def test_jira_search_says_when_it_stopped_short(jira_search) -> None:
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    issues = await client.search_issues("project = PA", max_results=20)

    assert len(issues) == 20
    assert not issues.complete
    assert issues.total == 250


async def test_jira_search_stops_when_the_server_says_it_is_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``isLast`` outranks a token the server sends anyway."""
    calls = 0

    async def fake_post(self, path: str, json: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return {
            "issues": [{"key": "PA-1", "fields": {"summary": "only"}}],
            "isLast": True,
            "nextPageToken": "still-here",
        }

    monkeypatch.setattr(JiraClient, "_post", fake_post)
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    issues = await client.search_issues("project = PA", max_results=100)

    assert calls == 1
    assert issues.complete


async def test_jira_comments_page_by_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different dialect on the same API — offsets, not tokens."""
    seen: list[dict[str, Any]] = []
    total = 120

    async def fake_get(self, path: str, **params: Any) -> Any:
        seen.append(params)
        start = params.get("startAt", 0)
        size = min(params.get("maxResults", 50), 100)
        return {
            "comments": [
                {"id": str(i), "author": {"displayName": "A"}, "body": f"c{i}"}
                for i in range(start, min(start + size, total))
            ],
            "total": total,
        }

    monkeypatch.setattr(JiraClient, "_get", fake_get)
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    comments = await client.get_comments("PA-1", max_results=120)

    assert len(comments) == 120
    assert comments.complete
    assert [call["startAt"] for call in seen] == [0, 100]


async def test_jira_user_search_pages_a_bare_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No envelope at all, so a short page is the only end-of-data signal.

    Which means the endpoint cannot always answer "was that everything?". When
    the last page exactly fills the request, 150 existing and 150-of-more look
    identical from here. It reports ``complete=False`` — refusing to claim a
    completeness it cannot verify — and a caller that needs certainty asks for
    one more than it wants.
    """
    everyone = 150

    async def fake_get(self, path: str, **params: Any) -> Any:
        start = params.get("startAt", 0)
        size = min(params.get("maxResults", 50), 100)
        return [
            {"accountId": f"a{i}", "displayName": f"User {i}"}
            for i in range(start, min(start + size, everyone))
        ]

    monkeypatch.setattr(JiraClient, "_get", fake_get)
    client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    users = await client.search_users("User", max_results=150)
    assert len(users) == 150
    assert not users.complete, "a full last page is not evidence of the end"

    # One more than exists: the short page settles it.
    fewer = await client.search_users("User", max_results=200)
    assert len(fewer) == 150
    assert fewer.complete


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


def _confluence() -> ConfluenceClient:
    return ConfluenceClient(
        base_url="https://x.atlassian.net", email="a@b.c", api_token="t"
    )


async def test_confluence_search_pages_by_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = 90

    async def fake_get_v1(self, path: str, **params: Any) -> Any:
        start = params.get("start", 0)
        size = min(params.get("limit", 25), 50)
        rows = [
            {"content": {"id": str(i), "title": f"Page {i}"}, "excerpt": ""}
            for i in range(start, min(start + size, total))
        ]
        return {
            "results": rows,
            "totalSize": total,
            "_links": {"next": "/rest/api/search?start=50"} if start + len(rows) < total else {},
        }

    monkeypatch.setattr(ConfluenceClient, "_get_v1", fake_get_v1)

    pages = await _confluence().search_pages("type = page", limit=90)

    assert len(pages) == 90
    assert pages.complete
    assert pages[0].title == "Page 0"


async def test_confluence_v2_cursor_is_extracted_from_the_next_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 hands back a whole URL; sending it as the cursor returns nothing.

    That failure looks exactly like reaching the end of the data, which is why
    it is worth a test of its own rather than trusting the round trip.
    """
    seen: list[Any] = []
    total = 60

    async def fake_get(self, path: str, **params: Any) -> Any:
        seen.append(params.get("cursor"))
        start = int(params.get("cursor") or 0)
        size = min(params.get("limit", 25), 50)
        rows = [{"id": str(i), "key": f"S{i}", "name": f"Space {i}"}
                for i in range(start, min(start + size, total))]
        nxt = start + len(rows)
        links = (
            {"next": f"/wiki/api/v2/spaces?limit={size}&cursor={nxt}"}
            if nxt < total
            else {}
        )
        return {"results": rows, "_links": links}

    monkeypatch.setattr(ConfluenceClient, "_get", fake_get)

    spaces = await _confluence().list_spaces(limit=60)

    assert len(spaces) == 60
    assert spaces.complete
    assert seen == [None, "50"], "the cursor must be the parameter, not the URL"


async def test_confluence_comments_follow_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = 70

    async def fake_get(self, path: str, **params: Any) -> Any:
        start = int(params.get("cursor") or 0)
        size = min(params.get("limit", 25), 50)
        rows = [
            {"id": str(i), "body": {"storage": {"value": f"c{i}"}}}
            for i in range(start, min(start + size, total))
        ]
        nxt = start + len(rows)
        return {
            "results": rows,
            "_links": {"next": f"?cursor={nxt}"} if nxt < total else {},
        }

    monkeypatch.setattr(ConfluenceClient, "_get", fake_get)

    comments = await _confluence().get_page_comments("123", limit=70)

    assert len(comments) == 70
    assert comments.complete


async def test_confluence_search_says_when_it_stopped_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_v1(self, path: str, **params: Any) -> Any:
        start = params.get("start", 0)
        size = params.get("limit", 25)
        return {
            "results": [
                {"content": {"id": str(i), "title": f"P{i}"}, "excerpt": ""}
                for i in range(start, start + size)
            ],
            "totalSize": 1000,
            "_links": {"next": "more"},
        }

    monkeypatch.setattr(ConfluenceClient, "_get_v1", fake_get_v1)

    pages = await _confluence().search_pages("type = page", limit=10)

    assert len(pages) == 10
    assert not pages.complete
    assert pages.summary() == "10 of 1000"


# ---------------------------------------------------------------------------
# What the coding agent is told
# ---------------------------------------------------------------------------


class TestTheCodingAgentKnows:
    """Paginating correctly is half of it; generated code has to use the fact.

    A workflow that fetches 100 of 312 issues and reports "here are the open
    issues" is wrong in exactly the way that survives review — the number is
    real, the framing is not.
    """

    def test_the_prompt_says_a_list_result_may_be_capped(self) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "at most its `max_results`" in DEFAULT_SYSTEM_PROMPT
        assert ".complete" in DEFAULT_SYSTEM_PROMPT

    def test_the_prompt_says_all_means_all(self) -> None:
        """The observed failure: "show all the stories" became max_results=100."""
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "all / every / the full list" in DEFAULT_SYSTEM_PROMPT
        assert "raise the limit" in DEFAULT_SYSTEM_PROMPT

    def test_the_rule_no_longer_carries_a_boundary_caveat(self) -> None:
        """The caveat is gone because the trap is gone.

        It used to read "check .complete, but only inside the step, because
        reshaping loses it" — and a rule with a caveat is a rule a small model
        violates, which is exactly what happened. Now the flag survives the
        journal, so the rule is one line. This asserts the caveat did not creep
        back in alongside the fix.
        """
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "only **inside the step**" not in DEFAULT_SYSTEM_PROMPT
        assert "survives being returned from a step" in DEFAULT_SYSTEM_PROMPT

    def test_the_unbounded_case_is_not_solved_by_a_bigger_limit(self) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "no natural bound" in DEFAULT_SYSTEM_PROMPT
        assert "bound the *window*" in DEFAULT_SYSTEM_PROMPT

    def test_the_prompt_does_not_teach_a_parameter_no_tool_accepts(self) -> None:
        """It told the model to page with ``cursor=``. Nothing accepts one.

        Introspecting every shipped toolset finds 82 paged reads and 82 without
        a ``cursor`` parameter, so code written exactly as instructed raised
        ``TypeError`` — and `CodeValidator` does no signature checking, so it
        survived to the smoke stage before anything noticed.
        """
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT
        from loom.agents.tool_registry import PAGING_HOWTO

        assert "cursor=cursor" not in DEFAULT_SYSTEM_PROMPT
        assert "cursor=cursor" not in "\n".join(PAGING_HOWTO)

    def test_no_shipped_paged_read_takes_a_cursor(self) -> None:
        """The fact the advice above is grounded in, asserted rather than recalled.

        If a toolset ever gains real resumable paging, this fails and the prompt
        should be changed back — which is the point of pinning it.
        """
        import importlib
        import inspect

        from loom.toolsets.pagination import paginates
        from test_manifest_imports import FIRST_PARTY

        with_cursor: list[str] = []
        paged = 0
        for manifest in FIRST_PARTY:
            module = importlib.import_module(manifest.tools_module)
            for op in manifest.all_operations():
                fn = getattr(module, op.function, None) if op.function else None
                if fn is None or not paginates(fn):
                    continue
                paged += 1
                target = getattr(fn, "fn", fn)
                if "cursor" in inspect.signature(target).parameters:
                    with_cursor.append(op.function)

        assert paged > 50, "expected the shipped toolsets to have many paged reads"
        assert not with_cursor, (
            "these reads now accept a cursor, so the prompt can teach resumable "
            f"paging again: {with_cursor}"
        )

    def test_the_prompt_asks_for_the_coverage_to_be_reported(self) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        flat = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "raise the limit and report the coverage" in flat
        assert "looks complete and is not" in DEFAULT_SYSTEM_PROMPT

    def test_the_example_names_no_particular_toolset(self) -> None:
        """The prompt costs the same for every user; it stays generic."""
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        capped = DEFAULT_SYSTEM_PROMPT[
            DEFAULT_SYSTEM_PROMPT.index("What a toolset call hands back"):
        ][:700]
        for named in ("jira_", "gmail_", "confluence_", "slack_"):
            assert named not in capped, named


class TestCoverageStage:
    """Instructions in a prompt are advice; a check is what makes it stick.

    Reconstructed from a real generation. Asked to "show all the stories in sas
    work", the agent wrote ``max_results=100`` and reported ``f"({len(issues)}
    found)"`` — no error, clean validation, and a report that says "42 found"
    when 312 matched.
    """

    CAPPED = (
        "from loom import Context, step, workflow\n"
        "from loom.toolsets.jira.tools import jira_search_issues\n"
        "\n"
        "@step\n"
        "async def fetch() -> list:\n"
        "    issues = await jira_search_issues('project = PA', max_results=100)\n"
        '    return [{"key": i.key} for i in issues]\n'
        "\n"
        '@workflow(name="show")\n'
        "async def show(ctx: Context, data) -> str:\n"
        "    issues = await ctx.step(fetch)\n"
        '    return f"({len(issues)} found)"\n'
    )

    async def _run(self, code: str, spec: str):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import CoverageStage

        return await CoverageStage().run(code, CheckContext(spec=spec))

    async def test_it_catches_the_generation_that_prompted_it(self) -> None:
        result = await self._run(self.CAPPED, "show all the stories in sas work")

        assert len(result.issues) == 1
        message = result.issues[0].message
        assert "max_results=100" in message
        assert result.issues[0].severity == "warning"
        assert ".complete" in message, "the fix has to be in the message"

    async def test_a_narrow_spec_is_left_alone(self) -> None:
        """Capping is usually right. This must not fire on every workflow."""
        for spec in ("show the 5 most recent stories", "get the newest bug"):
            assert not (await self._run(self.CAPPED, spec)).issues, spec

    async def test_checking_coverage_satisfies_it(self) -> None:
        checked = self.CAPPED.replace(
            '    return [{"key": i.key} for i in issues]',
            '    return {"rows": [i.key for i in issues], "of": issues.summary()}',
        )
        assert not (await self._run(checked, "show all the stories")).issues

    async def test_a_cap_in_a_comment_is_not_a_cap(self) -> None:
        """Parsed, not pattern-matched — a docstring is not code."""
        commented = (
            '"""Fetches with max_results=100 by default."""\n'
            "async def fetch():\n"
            "    return await search('q')\n"
        )
        assert not (await self._run(commented, "show all of them")).issues

    async def test_unparseable_code_is_not_this_stage_s_problem(self) -> None:
        """Compile runs first and blocks; this must not double-report."""
        assert not (await self._run("def (", "show all of them")).issues

    async def test_it_is_a_warning_not_a_blocker(self) -> None:
        """Deciding 100 is enough is legitimate; not noticing is the defect."""
        from loom.agents.stages import CoverageStage

        assert CoverageStage().blocking is False

    async def test_it_runs_before_the_expensive_stages(self) -> None:
        from loom.agents.stages import default_stages

        names = [stage.name for stage in default_stages()]
        assert names.index("coverage") < names.index("smoke")


class TestAThirdPartyToolsetGetsTheSameView:
    """The claim that this scales to toolsets outside this repo.

    Asserted by building one here — a toolset defined entirely in the test,
    with an API dialect none of the shipped ones use — and checking it reaches
    the coding agent identically. Testing only the four bundled toolsets would
    prove the four bundled toolsets work.
    """

    def _toolset(self):
        from loom import step
        from loom.agents.tool_registry import Toolset
        from loom.toolsets.pagination import Page, collect

        rows = [f"row-{i}" for i in range(250)]

        @step(name="widgets_search")
        async def widgets_search(query: str, max_results: int = 20) -> Results[str]:
            """Search widgets.

            Args:
                query: What to look for.
                max_results: Most rows to return.
            """

            async def fetch(cursor: str | None, size: int) -> Page:
                # A dialect none of the shipped toolsets use: a page number.
                page_no = int(cursor or 0)
                start = page_no * 40
                batch = rows[start : start + min(size, 40)]
                more = start + len(batch) < len(rows)
                return Page(batch, str(page_no + 1) if more else None, total=len(rows))

            return await collect(fetch, limit=max_results, page_size=40)

        @step(name="widgets_get")
        async def widgets_get(widget_id: str) -> str:
            """Fetch one widget.

            Args:
                widget_id: Which one.
            """
            return widget_id

        return Toolset.from_steps("widgets", [widgets_search, widgets_get])

    def test_declaring_paging_is_only_the_return_type(self) -> None:
        """No manifest edit, no registration argument, no per-op declaration."""
        manifest = self._toolset().manifest
        declared = {op.id: op.pagination for op in manifest.all_operations()}

        assert declared == {"widgets_search": True, "widgets_get": False}
        assert [op.id for op in manifest.paginated()] == ["widgets_search"]

    def test_it_appears_in_the_docs_the_agent_reads(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry

        registry = ToolsetRegistry()
        registry.register(self._toolset())
        docs = registry.describe(["widgets"], detail="index")

        assert "Paged: widgets_search" in docs
        assert "widgets_get" not in docs.split("Paged:")[1].split("\n")[0]

    def test_the_how_to_is_printed_once_for_the_whole_catalog(self) -> None:
        """Otherwise the index grows with integrations installed, not with the task.

        Twelve lines of pattern per toolset is fine at four and ruinous at a
        thousand — it is the exact cost the three-tier catalog exists to avoid.
        """
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        registry.register(self._toolset())
        registry.register(JIRA_MANIFEST)
        docs = registry.describe(detail="index")

        assert docs.count("Bounded set — one call") == 1
        assert docs.count("Paged: ") == 2

    async def test_its_paging_actually_works(self) -> None:
        """A declaration nobody ran is a claim, not a fact."""
        from loom.agents.tools import ToolContext

        tool = self._toolset().resolve("widgets_search")
        ctx = ToolContext(agent_name="test")

        found = await tool.invoke({"query": "x", "max_results": 100}, ctx)
        assert len(found) == 100
        assert not found.complete
        assert found.summary() == "100 of 250"

        everything = await tool.invoke({"query": "x", "max_results": 250}, ctx)
        assert len(everything) == 250
        assert everything.complete

    async def test_its_results_survive_a_journal(self) -> None:
        """The property the whole design rests on, for a toolset we do not own."""
        from loom.agents.tools import ToolContext
        from loom.core.serde import decode, encode

        tool = self._toolset().resolve("widgets_search")
        found = await tool.invoke(
            {"query": "x", "max_results": 50}, ToolContext(agent_name="test")
        )

        restored = decode(encode(found))
        assert restored.complete is False
        assert restored.total == 250
        assert restored.summary() == "50 of 250"


# ---------------------------------------------------------------------------
# Slack's nested cursor, through the existing token dialect
# ---------------------------------------------------------------------------


class TestSlacksNestedCursor:
    """Slack puts its cursor at ``response_metadata.next_cursor``.

    Deliberately *not* a fourth dialect: ``TokenPaging`` already addresses a
    nested field through a tuple ``token_field`` (HubSpot's is at
    ``paging.next.after``), and already treats an empty token as exhausted. A
    new class here would have been the same dialect written twice.
    """

    def _style(self):
        from loom.toolsets.pagination import TokenPaging

        return TokenPaging(
            items="channels",
            size_param="limit",
            token_param="cursor",
            token_field=("response_metadata", "next_cursor"),
        )

    def test_the_first_request_asks_for_a_size_and_no_cursor(self) -> None:
        assert self._style().params(None, 200) == {"limit": 200}

    def test_a_later_request_carries_the_cursor(self) -> None:
        assert self._style().params("abc=", 50) == {"limit": 50, "cursor": "abc="}

    def test_it_reads_a_cursor_out_of_the_envelope(self) -> None:
        page = self._style().read(
            {"channels": [1, 2], "response_metadata": {"next_cursor": "n1"}}, None, 2
        )
        assert page.items == [1, 2]
        assert page.cursor == "n1"

    def test_an_empty_string_cursor_is_the_end(self) -> None:
        """Slack sends "" as often as it omits the field; reading only for
        absence would loop to the page ceiling against a real workspace."""
        page = self._style().read(
            {"channels": [1], "response_metadata": {"next_cursor": ""}}, None, 1
        )
        assert page.cursor is None

    def test_an_absent_envelope_is_the_end(self) -> None:
        assert self._style().read({"channels": [1]}, None, 1).cursor is None

    def test_a_null_envelope_does_not_crash(self) -> None:
        page = self._style().read(
            {"channels": [], "response_metadata": None}, None, 1
        )
        assert page.cursor is None

    def test_a_missing_rows_key_is_an_empty_page(self) -> None:
        assert self._style().read({"ok": True}, None, 5).items == []

    async def test_it_drives_collect_to_exhaustion(self) -> None:
        from loom.toolsets.pagination import page_through

        pages = [
            {"channels": [1, 2], "response_metadata": {"next_cursor": "c2"}},
            {"channels": [3], "response_metadata": {"next_cursor": ""}},
        ]
        seen: list[dict] = []

        async def request(asked):
            seen.append(asked)
            return pages[len(seen) - 1]

        found = await page_through(
            request, style=self._style(), limit=10, page_size=2
        )

        assert list(found) == [1, 2, 3]
        assert found.complete is True
        assert seen[1]["cursor"] == "c2"


class TestFiltered:
    """The counterpart to `.mapped()`, and why it cannot be built from it."""

    def test_coverage_survives_a_filter(self) -> None:
        from loom.toolsets.pagination import Results

        page = Results([1, 2, 3, 4], complete=False, total=312, cursor="c")

        kept = page.filtered(lambda n: n % 2 == 0)

        assert list(kept) == [2, 4]
        assert kept.complete is False
        assert kept.cursor == "c"

    def test_total_is_dropped_because_it_no_longer_describes_these_rows(self) -> None:
        """`mapped` keeps `total`; a filter must not.

        Keeping it would report a count larger than what is being returned —
        the same "a number that does not describe these rows" failure `Results`
        exists to prevent, arriving from the other direction. This is the exact
        difference that makes `.filtered` un-fakeable with `.mapped`.
        """
        from loom.toolsets.pagination import Results

        page = Results([1, 2, 3], complete=True, total=3)

        assert page.mapped(str).total == 3
        assert page.filtered(lambda n: n > 1).total is None

    def test_a_comprehension_still_loses_everything(self) -> None:
        """Stated as a test because it is the thing `filtered` exists to replace."""
        from loom.toolsets.pagination import Results

        page = Results([1, 2, 3], complete=False, total=312)

        assert not hasattr([n for n in page if n > 1], "complete")

    def test_it_is_a_results_and_not_a_list(self) -> None:
        from loom.toolsets.pagination import Results

        assert isinstance(Results([1]).filtered(lambda n: True), Results)


class TestResumingFromACursor:
    """`Results.cursor` documented a pattern the plumbing could not perform."""

    async def test_collect_can_start_from_a_cursor(self) -> None:
        from loom.toolsets.pagination import Page, collect

        seen: list[str | None] = []

        async def fetch(cursor: str | None, size: int) -> Page:
            seen.append(cursor)
            if cursor == "p2":
                return Page(items=["c", "d"], cursor=None)
            return Page(items=["a", "b"], cursor="p2")

        rows = await collect(fetch, limit=10, page_size=2, start="p2")

        assert seen[0] == "p2", "paging must begin where the caller said"
        assert list(rows) == ["c", "d"]
        assert rows.complete is True

    async def test_starting_from_none_is_unchanged(self) -> None:
        from loom.toolsets.pagination import Page, collect

        async def fetch(cursor: str | None, size: int) -> Page:
            return Page(items=["a"], cursor=None)

        rows = await collect(fetch, limit=10, page_size=2)

        assert list(rows) == ["a"]


class TestCoverageStageBlindSpots:
    """Each case silenced the whole stage while saying nothing about coverage."""

    SPEC = "Show all the stories in project PA"

    async def _issues(self, code: str):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import CoverageStage

        return (await CoverageStage().run(code, CheckContext(spec=self.SPEC))).issues

    async def test_an_unrelated_total_seconds_no_longer_disarms_it(self) -> None:
        """`(end - start).total_seconds()` contains the substring `.total`."""
        code = (
            "async def f(ctx):\n"
            "    r = await ctx.step(jira_search_issues, 'x', max_results=100)\n"
            "    d = (b - a).total_seconds()\n"
            "    return len(r)\n"
        )
        assert await self._issues(code)

    async def test_a_completed_field_no_longer_disarms_it(self) -> None:
        """`task.completed` is a real field on Asana and ClickUp rows."""
        code = (
            "async def f(ctx):\n"
            "    r = await ctx.step(jira_search_issues, 'x', max_results=100)\n"
            "    return [t for t in r if t.completed]\n"
        )
        assert await self._issues(code)

    async def test_a_cap_bound_to_a_name_is_seen(self) -> None:
        """Lifting the number into a constant is ordinary tidying, not a fix."""
        code = (
            "CAP = 100\n"
            "async def f(ctx):\n"
            "    r = await ctx.step(jira_search_issues, 'x', max_results=CAP)\n"
            "    return len(r)\n"
        )
        assert await self._issues(code)

    async def test_other_cap_keywords_are_seen(self) -> None:
        """`num_results` is Exa's spelling and was invisible."""
        for kwarg in ("page_size", "per_page", "num_results", "top"):
            code = (
                "async def f(ctx):\n"
                f"    r = await ctx.step(some_search, 'x', {kwarg}=10)\n"
                "    return len(r)\n"
            )
            assert await self._issues(code), kwarg

    async def test_a_genuine_coverage_read_is_still_silent(self) -> None:
        """The negative control — a stage that flags everything is not a check."""
        code = (
            "async def f(ctx):\n"
            "    r = await ctx.step(jira_search_issues, 'x', max_results=500)\n"
            "    return r.summary()\n"
        )
        assert not await self._issues(code)

    async def test_a_destructured_coverage_read_is_silent(self) -> None:
        code = (
            "async def f(ctx):\n"
            "    r = await ctx.step(jira_search_issues, 'x', max_results=500)\n"
            "    complete = r.complete\n"
            "    return complete\n"
        )
        assert not await self._issues(code)


class TestPlacementStage:
    """Fetch-everything-then-filter passed all ten stages before this existed."""

    async def _issues(self, code: str):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import PlacementStage

        return (await PlacementStage().run(code, CheckContext())).issues

    async def test_a_filtered_comprehension_over_a_service_read_is_flagged(self) -> None:
        code = (
            "async def f(ctx):\n"
            "    issues = await ctx.step(jira_search_issues, 'project = PA')\n"
            "    return [i for i in issues if i.priority == 'High']\n"
        )
        issues = await self._issues(code)
        assert issues
        assert "issues" in issues[0].message

    async def test_a_direct_toolset_call_counts_too(self) -> None:
        """`ctx.step(op, ...)` and `await op(...)` are both sanctioned forms."""
        code = (
            "async def f(ctx):\n"
            "    rows = await gmail_search_messages(q='')\n"
            "    return [r for r in rows if r.unread]\n"
        )
        assert await self._issues(code)

    async def test_a_generator_expression_counts_too(self) -> None:
        code = (
            "async def f(ctx):\n"
            "    rows = await ctx.step(hubspot_find_contacts, '')\n"
            "    return sum(1 for r in rows if r.active)\n"
        )
        assert await self._issues(code)

    async def test_a_server_side_filter_is_silent(self) -> None:
        code = (
            "async def f(ctx):\n"
            "    issues = await ctx.step(jira_search_issues, 'project = PA AND priority = High')\n"
            "    return [i.key for i in issues]\n"
        )
        assert not await self._issues(code)

    async def test_a_local_list_is_not_a_service_read(self) -> None:
        """The false positive that would make the stage noise."""
        code = (
            "async def f(ctx):\n"
            "    xs = [1, 2, 3]\n"
            "    return [x for x in xs if x > 1]\n"
        )
        assert not await self._issues(code)

    async def test_using_filtered_is_silent(self) -> None:
        """The suggested fix must not itself trip the check."""
        code = (
            "async def f(ctx):\n"
            "    issues = await ctx.step(jira_search_issues, 'project = PA')\n"
            "    return issues.filtered(lambda i: i.priority == 'High')\n"
        )
        assert not await self._issues(code)

    async def test_it_is_in_the_default_pipeline(self) -> None:
        from loom.agents.stages import default_stages

        assert "placement" in {s.name for s in default_stages(smoke=False)}


class TestFakesKeepTheCoverageWrapper:
    """A fake that fails validation must not hand back a bare list.

    `_coerce` failed open, so a paged operation whose generated fake did not
    validate returned a plain list — and generated code correctly calling
    `.summary()` then died with `AttributeError` inside the *blocking* smoke
    stage. `AttributeError` is explicitly classified as a code fault rather than
    an environment one, so the repair loop's cheapest fix was to delete the
    coverage check: the pipeline taught the model to remove the safety property.
    """

    def test_an_unvalidatable_payload_still_yields_results(self) -> None:
        from pydantic import BaseModel

        from loom.agents.fakes import _coerce
        from loom.toolsets.pagination import Results

        class Row(BaseModel):
            id: str

        out = _coerce([{"id": None}], Results[Row])

        assert isinstance(out, Results)
        assert out.summary()

    def test_a_non_paged_type_is_unaffected(self) -> None:
        from pydantic import BaseModel

        from loom.agents.fakes import _coerce

        class Row(BaseModel):
            id: str

        assert _coerce([{"id": None}], list[Row]) == [{"id": None}]
