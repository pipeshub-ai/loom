"""An oversized tool result must not silently become a prefix.

The failure being prevented: a search returns four megabytes, the provider
truncates it, and the model answers confidently about data it never saw. What
replaces it has to say three things — that something was omitted, what shape
the whole thing has, and how to read the rest.
"""

from __future__ import annotations

import json

import pytest

from loom.agents.bounds import (
    BlobSpillStore,
    NullSpillStore,
    ResultBounds,
    SpillRef,
    bound_result,
    coverage_of,
)
from loom.agents.shape import describe_format, describe_shape
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.toolsets.pagination import Results

REF = SpillRef(
    locator="blob:" + "a" * 64,
    bytes=1_000_000,
    retrieval_hint="Call read_spill(ref, offset, limit) to page it.",
)


def rows(count: int) -> str:
    return json.dumps(
        {"issues": [{"key": f"ENG-{i}", "summary": "x" * 80} for i in range(count)]}
    )


class TestBounding:
    def test_a_small_result_is_untouched(self) -> None:
        bounds = ResultBounds()
        text = rows(3)
        assert bounds.within(text)
        assert bounds.apply(text, None) is not None

    @pytest.mark.parametrize("cap", [600, 900, 4_096, 32_768])
    @pytest.mark.parametrize("count", [10, 400, 5_000])
    def test_the_replacement_never_exceeds_the_cap(self, cap: int, count: int) -> None:
        """The property the notice's own byte cost is reserved for.

        Truncate-then-append always violates this by the length of the notice,
        which is the obvious implementation and the wrong one.
        """
        bounds = ResultBounds(max_bytes=cap)
        text = rows(count)
        if bounds.within(text):
            pytest.skip("not oversized at this cap")
        bounded = bounds.apply(text, REF, coverage="Showing 40 of 312 rows.")
        assert len(bounded.encode("utf-8")) <= cap

    def test_it_never_adds_bytes(self) -> None:
        bounds = ResultBounds(max_bytes=2_000)
        text = rows(500)
        bounded = bounds.apply(text, REF)
        assert len(bounded.encode("utf-8")) < len(text.encode("utf-8"))

    def test_the_notice_names_the_locator_and_how_to_read_it(self) -> None:
        bounded = ResultBounds(max_bytes=2_000).apply(rows(500), REF)
        assert REF.locator in bounded
        assert "read_spill" in bounded
        assert "Do not re-call the tool" in bounded

    def test_without_a_store_it_says_so(self) -> None:
        bounded = ResultBounds(max_bytes=2_000).apply(rows(500), None)
        assert "was not stored" in bounded
        assert "blob:" not in bounded

    def test_it_reports_format_and_shape(self) -> None:
        bounded = ResultBounds(max_bytes=2_000).apply(rows(500), REF)
        assert "Format: JSON" in bounded
        assert "issues: array(500)" in bounded

    def test_a_cap_smaller_than_its_own_notice_keeps_the_original(self) -> None:
        """Returning something over the cap would break the one promise here."""
        text = rows(500)
        assert ResultBounds(max_bytes=50).apply(text, REF) == text

    def test_multibyte_text_survives_the_split(self) -> None:
        text = "日本語のテキスト" * 5_000
        bounded = ResultBounds(max_bytes=1_200).apply(text, REF)
        bounded.encode("utf-8").decode("utf-8")  # no partial characters
        assert len(bounded.encode("utf-8")) <= 1_200

    def test_line_count_alone_triggers_bounding(self) -> None:
        text = "\n".join(f"line {i}" for i in range(5_000))
        bounds = ResultBounds(max_bytes=10_000_000, max_lines=100)
        assert not bounds.within(text)


class TestPaginationCoverage:
    """A paged read must not be summarized into looking complete.

    ``Results.__wire__`` puts ``items`` first and ``complete``/``total`` last,
    so a head-and-tail cut is exactly where the coverage disappears. That is
    the failure the whole ``Results`` type exists to prevent, and the bounding
    layer is in a position to reintroduce it.
    """

    def test_an_incomplete_read_says_so_in_the_notice(self) -> None:
        page = Results([{"key": f"ENG-{i}"} for i in range(40)], complete=False, total=312)
        assert coverage_of(page) == "Showing 40 of 312 rows — this read was NOT complete."

    def test_a_complete_read_says_that_too(self) -> None:
        page = Results([1, 2, 3], complete=True)
        assert coverage_of(page) == "Complete: all 3 rows."

    def test_an_unknown_total_still_reports_incompleteness(self) -> None:
        page = Results([1, 2], complete=False)
        assert "NOT complete" in coverage_of(page)

    def test_a_plain_list_has_no_coverage_to_report(self) -> None:
        assert coverage_of([1, 2, 3]) == ""
        assert coverage_of({"a": 1}) == ""

    @pytest.mark.asyncio
    async def test_coverage_survives_bounding(self) -> None:
        page = Results(
            [{"key": f"ENG-{i}", "summary": "x" * 80} for i in range(400)],
            complete=False,
            total=9_000,
        )
        text = json.dumps(list(page))
        bounded = await bound_result(
            text,
            page,
            bounds=ResultBounds(max_bytes=1_500),
            store=None,
            run_id="r1",
            tool="jira_search_issues",
            call_id="c1",
        )
        assert "NOT complete" in bounded
        assert "9,000" in bounded


class TestSpillStores:
    @pytest.mark.asyncio
    async def test_blob_store_round_trips(self, tmp_path) -> None:
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        text = rows(200)
        ref = await store.save(text, run_id="r1", tool="search", call_id="c1")

        assert ref.locator.startswith("blob:")
        assert ref.bytes == len(text.encode("utf-8"))
        assert await store.read(ref.locator, offset=0, limit=20) == text[:20]

    @pytest.mark.asyncio
    async def test_identical_results_share_one_blob(self, tmp_path) -> None:
        """Content addressing is why this reuses BlobService rather than adding a store."""
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        first = await store.save(rows(10), run_id="r1", tool="s", call_id="c1")
        second = await store.save(rows(10), run_id="r2", tool="s", call_id="c2")
        assert first.locator == second.locator

    @pytest.mark.asyncio
    async def test_grep_finds_lines_and_caps_matches(self, tmp_path) -> None:
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        text = "\n".join(f"row {i} status=open" for i in range(200))
        ref = await store.save(text, run_id="r1", tool="s", call_id="c1")

        found = await store.grep(ref.locator, "status=open", max_matches=5)
        assert len(found) == 5

    @pytest.mark.asyncio
    async def test_an_uncompilable_pattern_is_treated_literally(self, tmp_path) -> None:
        """A model-supplied pattern is hostile input; a bad one must not raise."""
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        ref = await store.save("a[b\nplain", run_id="r1", tool="s", call_id="c1")
        assert await store.grep(ref.locator, "a[b") == ["a[b"]

    @pytest.mark.asyncio
    async def test_null_store_refuses_rather_than_pretending(self) -> None:
        from loom.agents.bounds import SpillUnavailable

        with pytest.raises(SpillUnavailable):
            await NullSpillStore().save("x", run_id="r", tool="t", call_id="c")


class TestBestEffort:
    @pytest.mark.asyncio
    async def test_a_failed_save_still_bounds(self) -> None:
        """A spill failure never turns a successful tool call into an error."""

        class Broken:
            async def save(self, text, **kwargs):
                raise RuntimeError("disk full")

            async def read(self, locator, **kwargs):  # pragma: no cover
                raise RuntimeError

            async def grep(self, locator, pattern, **kwargs):  # pragma: no cover
                raise RuntimeError

        bounded = await bound_result(
            rows(500),
            None,
            bounds=ResultBounds(max_bytes=1_000),
            store=Broken(),
            run_id="r1",
            tool="search",
            call_id="c1",
        )
        assert "was not stored" in bounded
        assert len(bounded.encode("utf-8")) <= 1_000

    @pytest.mark.asyncio
    async def test_no_bounds_means_no_change(self) -> None:
        text = rows(5_000)
        assert (
            await bound_result(
                text, None, bounds=None, store=None, run_id="r", tool="t", call_id="c"
            )
            == text
        )


class TestShape:
    def test_it_names_the_format(self) -> None:
        assert describe_format('{"a": 1}') == "JSON dict"
        assert describe_format('{"a":1}\n{"a":2}\n{"a":3}') == "JSONL (3 records)"
        assert describe_format("just words") == "text"

    def test_a_pretty_printed_document_is_not_jsonl(self) -> None:
        assert describe_format(json.dumps({"a": [1, 2]}, indent=2)) == "JSON dict"

    def test_it_summarizes_structure(self) -> None:
        shape = describe_shape({"issues": [{"key": "A", "fields": {"x": 1}}], "total": 9})
        assert "issues: array(1)" in shape
        assert "total: int" in shape

    def test_depth_and_width_are_bounded(self) -> None:
        deep: dict = {"a": 1}
        for _ in range(50):
            deep = {"nested": deep}
        wide = {f"key_{i}": i for i in range(500)}
        assert len(describe_shape(deep)) <= 500
        assert "+480 keys" in describe_shape(wide)

    def test_empty_and_scalar_values(self) -> None:
        assert describe_shape([]) == "array(0)"
        assert describe_shape(None) == "null"
        assert describe_shape(True) == "boolean"

    def test_a_non_identifier_key_is_quoted(self) -> None:
        assert '"a-b"' in describe_shape({"a-b": 1})


class TestThroughTheAgentLoop:
    """The runner is where this has to hold; the unit tests only prove the parts."""

    @pytest.mark.asyncio
    async def test_a_huge_tool_result_reaches_the_model_bounded(self, tmp_path) -> None:
        from loom.agents.agent import Agent
        from loom.agents.bounds import ResultBounds
        from loom.agents.executor import AgentContext
        from loom.agents.messages import ToolCall
        from loom.agents.tools import tool
        from loom.testing.mock import MockModelProvider, mock_response

        payload = rows(20_000)

        @tool
        async def search_issues(query: str) -> str:
            """Search issues.

            Args:
                query: What to search for.
            """
            return payload

        model = MockModelProvider(
            responses=[
                mock_response(
                    tool_calls=[
                        ToolCall(name="search_issues", arguments={"query": "bug"})
                    ]
                ),
                mock_response("done"),
            ]
        )
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        agent = Agent(
            name="searcher",
            model=model,
            tools=[search_issues],
            bounds=ResultBounds(max_bytes=4_000),
        )

        result = await agent("find bugs", context=AgentContext(run_id="r1", spill=store))

        # The second request is the one carrying the tool result back.
        sent = model.requests[-1]
        tool_messages = [
            m for m in sent.messages if getattr(m, "role", "") == "tool"
        ]
        assert tool_messages, "the tool result should have reached the model"
        text = str(tool_messages[-1].content)
        assert len(text.encode("utf-8")) <= 4_000
        assert "read_spill" in text
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_the_retrieval_tools_are_mounted_up_front(self, tmp_path) -> None:
        """Before the overflow, not after — the model plans from the tool list."""
        from loom.agents.agent import Agent
        from loom.agents.bounds import ResultBounds
        from loom.agents.executor import AgentContext
        from loom.testing.mock import MockModelProvider, mock_response

        model = MockModelProvider(responses=[mock_response("ok")])
        store = BlobSpillStore(BlobService(LocalBlobBackend(tmp_path)))
        agent = Agent(name="a", model=model, bounds=ResultBounds())

        await agent("hello", context=AgentContext(run_id="r1", spill=store))

        offered = {t.name for t in (model.last_request().tools or [])}
        assert {"read_spill", "grep_spill"} <= offered

    @pytest.mark.asyncio
    async def test_no_bounds_configured_changes_nothing(self) -> None:
        from loom.agents.agent import Agent
        from loom.agents.executor import AgentContext
        from loom.testing.mock import MockModelProvider, mock_response

        model = MockModelProvider(responses=[mock_response("ok")])
        agent = Agent(name="a", model=model)

        await agent("hello", context=AgentContext(run_id="r1"))

        offered = {t.name for t in (model.last_request().tools or [])}
        assert "read_spill" not in offered
