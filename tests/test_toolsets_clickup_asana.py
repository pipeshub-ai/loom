"""ClickUp and Asana: auth shape, paging dialect, and error classification.

No network. Everything here is either pure translation (a wire payload into a
model) or a transport stub, because the three things most likely to be wrong in
a toolset are wrong before any request succeeds: the header format, the paging
loop's stop condition, and whether a 4xx is treated as worth retrying.
"""

from __future__ import annotations

import pytest

from loom.core.exceptions import NonRetryableError
from loom.toolsets.asana.client import (
    AsanaAuthError,
    AsanaClient,
    AsanaPermanentError,
    AsanaPremiumRequired,
    AsanaRateLimited,
)
from loom.toolsets.asana.client import (
    _classify as asana_classify,
)
from loom.toolsets.asana.models import AsanaStory, AsanaTask
from loom.toolsets.clickup.client import (
    ClickUpAuthError,
    ClickUpClient,
    ClickUpPermanentError,
    ClickUpRateLimited,
)
from loom.toolsets.clickup.client import (
    _classify as clickup_classify,
)
from loom.toolsets.clickup.models import ClickUpTask
from loom.toolsets.pagination import PageNumberPaging, page_through


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


# -- auth -------------------------------------------------------------------


class TestClickUpAuth:
    def test_a_personal_token_is_sent_without_a_scheme(self, monkeypatch) -> None:
        """The ClickUp detail that catches people out.

        A personal token goes in Authorization raw. Sending it as
        ``Bearer pk_…`` returns 401 with no hint as to why.
        """
        monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_123")
        monkeypatch.delenv("CLICKUP_OAUTH_TOKEN", raising=False)

        header = ClickUpClient()._headers()["Authorization"]

        assert header == "pk_123"

    def test_an_oauth_token_takes_the_bearer_prefix(self, monkeypatch) -> None:
        monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
        monkeypatch.setenv("CLICKUP_OAUTH_TOKEN", "oauth_abc")

        header = ClickUpClient()._headers()["Authorization"]

        assert header == "Bearer oauth_abc"

    def test_oauth_wins_when_both_are_present(self, monkeypatch) -> None:
        """An app that completed an OAuth flow meant to act as that user."""
        monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_123")
        monkeypatch.setenv("CLICKUP_OAUTH_TOKEN", "oauth_abc")

        assert ClickUpClient()._headers()["Authorization"] == "Bearer oauth_abc"

    def test_no_credentials_fails_at_construction_naming_the_variable(
        self, monkeypatch
    ) -> None:
        """Not on the first request, five frames into a workflow step."""
        monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
        monkeypatch.delenv("CLICKUP_OAUTH_TOKEN", raising=False)

        with pytest.raises(ClickUpAuthError, match="CLICKUP_API_TOKEN"):
            ClickUpClient()

    def test_an_explicit_argument_beats_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_env")

        assert ClickUpClient(api_token="pk_arg")._headers()["Authorization"] == "pk_arg"


class TestAsanaAuth:
    def test_the_token_is_a_bearer(self, monkeypatch) -> None:
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "1/abc")

        client = AsanaClient()

        assert client._token == "1/abc"

    def test_no_credentials_fails_at_construction(self, monkeypatch) -> None:
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)

        with pytest.raises(AsanaAuthError, match="ASANA_ACCESS_TOKEN"):
            AsanaClient()


# -- error classification ---------------------------------------------------


class TestClickUpErrors:
    def test_a_4xx_is_not_retried(self) -> None:
        """A bad list id fails the same way three times. Retrying spends the
        budget to arrive at the same answer."""
        error = clickup_classify(
            FakeResponse(400, {"err": "List not found", "ECODE": "CAT_014"})
        )

        assert isinstance(error, ClickUpPermanentError)
        assert isinstance(error, NonRetryableError)
        assert "CAT_014" in str(error)

    def test_a_429_stays_retryable_and_carries_the_reset(self) -> None:
        """ClickUp allows 100 requests a minute on most plans, which a
        workflow paging a large list reaches easily."""
        error = clickup_classify(
            FakeResponse(429, {"err": "Rate limit"}, headers={"X-RateLimit-Reset": "60"})
        )

        assert isinstance(error, ClickUpRateLimited)
        assert not isinstance(error, NonRetryableError)
        assert error.retry_after == 60.0

    def test_a_401_is_an_auth_error(self) -> None:
        assert isinstance(clickup_classify(FakeResponse(401, {})), ClickUpAuthError)

    def test_a_5xx_stays_retryable(self) -> None:
        assert not isinstance(
            clickup_classify(FakeResponse(503, None, text="upstream")),
            NonRetryableError,
        )

    def test_a_non_json_body_still_classifies(self) -> None:
        """An HTML error page from a proxy is still an error."""
        error = clickup_classify(FakeResponse(502, None, text="<html>bad gateway"))

        assert "502" in str(error)


class TestAsanaErrors:
    def test_the_message_is_read_out_of_the_errors_envelope(self) -> None:
        error = asana_classify(
            FakeResponse(400, {"errors": [{"message": "project: Not a valid gid"}]})
        )

        assert "Not a valid gid" in str(error)
        assert isinstance(error, AsanaPermanentError)

    def test_a_premium_only_endpoint_is_its_own_error(self) -> None:
        """Worth separating: a workflow can act on it without a code change,
        by falling back to listing a project instead of searching."""
        error = asana_classify(
            FakeResponse(
                402, {"errors": [{"message": "Search requires premium access"}]}
            )
        )

        assert isinstance(error, AsanaPremiumRequired)
        assert isinstance(error, NonRetryableError)

    def test_a_403_naming_premium_is_not_read_as_a_scope_problem(self) -> None:
        error = asana_classify(
            FakeResponse(403, {"errors": [{"message": "This is a premium feature"}]})
        )

        assert isinstance(error, AsanaPremiumRequired)

    def test_a_plain_403_is_an_auth_error(self) -> None:
        error = asana_classify(
            FakeResponse(403, {"errors": [{"message": "Not authorized"}]})
        )

        assert isinstance(error, AsanaAuthError)
        assert not isinstance(error, AsanaPremiumRequired)

    def test_a_429_stays_retryable(self) -> None:
        error = asana_classify(FakeResponse(429, {}, headers={"Retry-After": "30"}))

        assert isinstance(error, AsanaRateLimited)
        assert not isinstance(error, NonRetryableError)
        assert error.retry_after == 30.0


# -- paging -----------------------------------------------------------------


class TestPageNumberPaging:
    """ClickUp's dialect: ordinal pages, with a flag for the last one."""

    async def test_it_follows_last_page_rather_than_counting_rows(self) -> None:
        pages = [
            {"tasks": [{"id": "1"}, {"id": "2"}], "last_page": False},
            {"tasks": [{"id": "3"}], "last_page": True},
        ]
        asked: list[dict] = []

        async def request(params):
            asked.append(params)
            return pages[int(params["page"])]

        rows = await page_through(
            request,
            style=PageNumberPaging(items="tasks"),
            limit=10,
            page_size=2,
            row=ClickUpTask.from_api,
        )

        assert [t.id for t in rows] == ["1", "2", "3"]
        assert [p["page"] for p in asked] == [0, 1]
        assert rows.complete

    async def test_a_full_final_page_still_stops_when_last_page_says_so(self) -> None:
        """The reason ``last_page`` is preferred over a short-page heuristic:
        a full last page is otherwise indistinguishable from more data."""
        page = {"tasks": [{"id": "1"}, {"id": "2"}], "last_page": True}

        async def request(params):
            return page

        rows = await page_through(
            request,
            style=PageNumberPaging(items="tasks"),
            limit=10,
            page_size=2,
            row=ClickUpTask.from_api,
        )

        assert len(rows) == 2
        assert rows.complete

    async def test_it_starts_at_page_zero(self) -> None:
        """ClickUp counts pages from 0, and page 1 silently skips the first
        hundred tasks rather than erroring."""
        seen: list[int] = []

        async def request(params):
            seen.append(params["page"])
            return {"tasks": [], "last_page": True}

        await page_through(
            request,
            style=PageNumberPaging(items="tasks"),
            limit=10,
            page_size=100,
            row=ClickUpTask.from_api,
        )

        assert seen == [0]


class TestAsanaPaging:
    """Asana's dialect: an opaque offset carried inside a next-page URI."""

    async def test_the_offset_is_parsed_out_of_the_next_page_uri(self) -> None:
        from loom.toolsets.asana.client import _ASANA_PAGING

        pages = [
            {
                "data": [{"gid": "1", "name": "one"}],
                "next_page": {
                    "offset": "eyJ0e",
                    "uri": "https://app.asana.com/api/1.0/tasks?offset=eyJ0e&limit=1",
                },
            },
            {"data": [{"gid": "2", "name": "two"}], "next_page": None},
        ]
        asked: list[dict] = []

        async def request(params):
            asked.append(params)
            return pages[len(asked) - 1]

        rows = await page_through(
            request,
            style=_ASANA_PAGING,
            limit=10,
            page_size=1,
            row=AsanaTask.from_api,
        )

        assert [t.gid for t in rows] == ["1", "2"]
        assert asked[1]["offset"] == "eyJ0e", "the offset was not carried forward"

    async def test_a_null_next_page_ends_the_loop(self) -> None:
        from loom.toolsets.asana.client import _ASANA_PAGING

        async def request(params):
            return {"data": [{"gid": "1"}], "next_page": None}

        rows = await page_through(
            request, style=_ASANA_PAGING, limit=10, page_size=50, row=AsanaTask.from_api
        )

        assert len(rows) == 1
        assert rows.complete


# -- translation ------------------------------------------------------------


class TestClickUpModels:
    def test_a_task_is_flattened_to_what_a_workflow_reasons_about(self) -> None:
        task = ClickUpTask.from_api(
            {
                "id": "86a1",
                "name": "Fix login",
                "status": {"status": "in progress", "type": "custom"},
                "assignees": [{"id": 42, "username": "priya"}],
                "priority": {"priority": "urgent"},
                "tags": [{"name": "bug"}],
                "list": {"id": "901", "name": "Sprint 4"},
                "due_date": "1772668800000",
            }
        )

        assert task.status == "in progress"
        assert task.assignees == ["priya"]
        assert task.assignee_ids == ["42"]
        assert task.priority == "urgent"
        assert task.tags == ["bug"]
        assert task.list_name == "Sprint 4"

    def test_a_millisecond_timestamp_becomes_iso(self) -> None:
        """Every other toolset here returns ISO-8601, and a workflow comparing
        a due date against ctx.now() should not have to know that one vendor
        counts milliseconds."""
        task = ClickUpTask.from_api({"id": "1", "due_date": "1772668800000"})

        assert task.due_date.startswith("2026-")

    def test_absent_optional_fields_do_not_raise(self) -> None:
        """ClickUp omits keys rather than sending nulls, and sends fewer of
        them from a list view than from a task fetched by id."""
        task = ClickUpTask.from_api({"id": "1"})

        assert task.status == ""
        assert task.assignees == []
        assert task.due_date == ""

    def test_a_null_priority_is_not_an_attribute_error(self) -> None:
        assert ClickUpTask.from_api({"id": "1", "priority": None}).priority == ""


class TestAsanaModels:
    def test_a_task_carries_both_the_assignee_name_and_gid(self) -> None:
        """The name is what a person reads; the gid is what a write needs."""
        task = AsanaTask.from_api(
            {
                "gid": "1201",
                "name": "Fix login",
                "assignee": {"gid": "99", "name": "Priya"},
                "projects": [{"name": "Platform"}],
                "due_on": "2026-09-01",
            }
        )

        assert task.assignee == "Priya"
        assert task.assignee_gid == "99"
        assert task.projects == ["Platform"]

    def test_an_unassigned_task_does_not_raise(self) -> None:
        """Asana sends ``assignee: null``, not a missing key."""
        task = AsanaTask.from_api({"gid": "1", "assignee": None})

        assert task.assignee == "" and task.assignee_gid == ""

    def test_a_story_records_its_type(self) -> None:
        """Comments are stories with type="comment"; the rest are field
        changes, which "show me the comments" does not mean."""
        story = AsanaStory.from_api(
            {"gid": "5", "text": "looks good", "type": "comment"}
        )

        assert story.type == "comment"
