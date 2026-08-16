"""GitHub and GitLab: header paging, the id traps, and error classification.

No network. The transport stubs return the ``{"items", "headers"}`` envelope
the clients really produce, so the header-driven paging dialect is exercised
for real rather than approximated.

Both services share a failure mode worth stating once: **they answer with a
partial or differently-shaped result rather than an error.** GitHub returns
pull requests from an issues endpoint and caps search at a thousand; GitLab
takes a per-project number where a global id looks equally plausible and drops
its total header exactly when the total matters. None of those raise.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from loom.core.exceptions import NonRetryableError
from loom.toolsets.github.client import (
    _GITHUB_PAGING,
    SEARCH_TOTAL_CAP,
    GitHubAuthError,
    GitHubClient,
    GitHubNotFound,
    GitHubPermanentError,
    GitHubRateLimited,
)
from loom.toolsets.github.client import (
    _classify as github_classify,
)
from loom.toolsets.github.models import GitHubIssue, GitHubPullRequest, GitHubRepo
from loom.toolsets.gitlab.client import (
    _GITLAB_PAGING,
    GitLabAuthError,
    GitLabClient,
    GitLabNotFound,
    GitLabRateLimited,
)
from loom.toolsets.gitlab.client import (
    _classify as gitlab_classify,
)
from loom.toolsets.gitlab.models import GitLabIssue, GitLabNote
from loom.toolsets.pagination import _link_page, page_through


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


class TestGitHubAuth:
    def test_a_token_is_all_it_takes(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

        assert GitHubClient()._token == "ghp_x"

    def test_no_token_fails_at_construction(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(GitHubAuthError, match="GITHUB_TOKEN"):
            GitHubClient()

    def test_enterprise_server_changes_only_the_base_url(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_API_URL", "https://ghe.internal/api/v3/")

        assert GitHubClient()._base_url == "https://ghe.internal/api/v3"


class TestGitLabAuth:
    def test_an_access_token_uses_the_private_token_header(self, monkeypatch) -> None:
        """Different header *names*, so a token in the wrong slot fails loudly
        rather than looking almost right."""
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")
        monkeypatch.delenv("GITLAB_OAUTH_TOKEN", raising=False)

        assert GitLabClient()._headers() == {"PRIVATE-TOKEN": "glpat-x"}

    def test_an_oauth_token_uses_bearer(self, monkeypatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.setenv("GITLAB_OAUTH_TOKEN", "oauth-x")

        assert GitLabClient()._headers() == {"Authorization": "Bearer oauth-x"}

    def test_the_instance_defaults_to_gitlab_com(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "x")
        monkeypatch.delenv("GITLAB_URL", raising=False)

        assert GitLabClient()._base_url == "https://gitlab.com"

    def test_a_self_managed_instance_is_configuration(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "x")
        monkeypatch.setenv("GITLAB_URL", "https://git.internal/")

        assert GitLabClient()._base_url == "https://git.internal"

    def test_no_token_fails_at_construction(self, monkeypatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_OAUTH_TOKEN", raising=False)

        with pytest.raises(GitLabAuthError, match="GITLAB_TOKEN"):
            GitLabClient()

    def test_a_project_path_is_url_encoded(self, monkeypatch) -> None:
        """An unencoded slash makes it a different route and returns 404,
        which reads as a missing project rather than a missing ``%2F``."""
        monkeypatch.setenv("GITLAB_TOKEN", "x")

        assert GitLabClient()._project("group/app") == "group%2Fapp"

    def test_a_numeric_id_is_left_alone(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "x")

        assert GitLabClient()._project("12345") == "12345"


# -- header paging ----------------------------------------------------------


class TestGitHubLinkHeaderPaging:
    """GitHub signals the next page in a Link header; no rel="next" is the end."""

    async def test_it_follows_rel_next_until_the_header_stops_offering_one(
        self,
    ) -> None:
        pages = {
            1: {
                "items": [{"number": 1, "title": "one"}],
                "headers": {
                    "link": '<https://api.github.com/x?page=2>; rel="next", '
                    '<https://api.github.com/x?page=2>; rel="last"'
                },
            },
            2: {"items": [{"number": 2, "title": "two"}], "headers": {}},
        }
        asked: list[int] = []

        async def request(params):
            asked.append(params["page"])
            return pages[params["page"]]

        rows = await page_through(
            request,
            style=_GITHUB_PAGING,
            limit=10,
            page_size=100,
            row=GitHubIssue.from_api,
        )

        assert [i.number for i in rows] == [1, 2]
        assert asked == [1, 2]
        assert rows.complete

    async def test_a_full_page_with_no_next_link_still_ends(self) -> None:
        """The Link header is authoritative, so a full final page is not
        mistaken for more data the way a short-page heuristic would be."""

        async def request(params):
            return {
                "items": [{"number": 1}, {"number": 2}],
                "headers": {"link": '<https://api.github.com/x?page=1>; rel="prev"'},
            }

        rows = await page_through(
            request,
            style=_GITHUB_PAGING,
            limit=10,
            page_size=2,
            row=GitHubIssue.from_api,
        )

        assert len(rows) == 2
        assert rows.complete

    def test_the_link_parser_finds_only_the_next_relation(self) -> None:
        header = (
            '<https://api.github.com/x?page=4>; rel="prev", '
            '<https://api.github.com/x?page=6>; rel="next", '
            '<https://api.github.com/x?page=9>; rel="last"'
        )

        assert _link_page(header, "page") == "6"

    def test_a_header_with_no_next_yields_nothing(self) -> None:
        assert _link_page('<https://api.github.com/x?page=1>; rel="first"', "page") is None

    def test_an_empty_header_yields_nothing(self) -> None:
        assert _link_page("", "page") is None


class TestGitLabHeaderPaging:
    """GitLab names the next page directly, and empty means the last one."""

    async def test_it_follows_x_next_page(self) -> None:
        pages = {
            1: {
                "items": [{"iid": 1}],
                "headers": {"x-next-page": "2", "x-total": "2"},
            },
            2: {"items": [{"iid": 2}], "headers": {"x-next-page": "", "x-total": "2"}},
        }
        asked: list[int] = []

        async def request(params):
            asked.append(params["page"])
            return pages[params["page"]]

        rows = await page_through(
            request,
            style=_GITLAB_PAGING,
            limit=10,
            page_size=100,
            row=GitLabIssue.from_api,
        )

        assert [i.iid for i in rows] == [1, 2]
        assert asked == [1, 2]
        assert rows.total == 2

    async def test_an_empty_next_header_means_the_last_page(self) -> None:
        """Empty is a different thing from absent, and both mean stop."""

        async def request(params):
            return {"items": [{"iid": 1}], "headers": {"x-next-page": ""}}

        rows = await page_through(
            request,
            style=_GITLAB_PAGING,
            limit=10,
            page_size=100,
            row=GitLabIssue.from_api,
        )

        assert len(rows) == 1 and rows.complete

    async def test_a_missing_total_header_reads_as_unknown_not_zero(self) -> None:
        """GitLab omits x-total past 10,000 records — exactly when the total
        matters — so reading it as zero would report the largest result sets
        as empty."""

        async def request(params):
            return {"items": [{"iid": 1}], "headers": {"x-next-page": ""}}

        rows = await page_through(
            request,
            style=_GITLAB_PAGING,
            limit=10,
            page_size=100,
            row=GitLabIssue.from_api,
        )

        assert rows.total is None
        assert len(rows) == 1


# -- the id traps -----------------------------------------------------------


class TestGitHubIssuesContainPullRequests:
    """GitHub's model, not a bug: every pull request is an issue."""

    def test_a_pull_request_row_is_flagged(self) -> None:
        row = GitHubIssue.from_api({"number": 7, "pull_request": {"url": "…"}})

        assert row.is_pull_request

    def test_a_plain_issue_is_not(self) -> None:
        assert not GitHubIssue.from_api({"number": 8}).is_pull_request

    def test_labels_arrive_as_objects_and_are_flattened(self) -> None:
        row = GitHubIssue.from_api({"number": 1, "labels": [{"name": "bug"}]})

        assert row.labels == ["bug"]

    def test_the_number_is_kept_separate_from_the_id(self) -> None:
        """The number is what a URL uses; the id is global and addresses
        something else on the pull request endpoints."""
        row = GitHubIssue.from_api({"number": 412, "id": 999888})

        assert row.number == 412
        assert row.id == "999888"

    def test_a_pull_request_model_carries_its_branches(self) -> None:
        pr = GitHubPullRequest.from_api(
            {
                "number": 7,
                "head": {"ref": "feature/login"},
                "base": {"ref": "main"},
                "draft": True,
            }
        )

        assert pr.head == "feature/login" and pr.base == "main" and pr.draft


class TestGitLabIidIsNotId:
    def test_both_numbers_are_carried_with_gitlab_names(self) -> None:
        """Passing an id where an iid belongs addresses a different issue in
        the same project, or none, and reports no error either way."""
        issue = GitLabIssue.from_api({"iid": 42, "id": 9001, "project_id": 7})

        assert issue.iid == 42
        assert issue.id == "9001"
        assert issue.project_id == "7"

    def test_labels_arrive_as_plain_strings_unlike_github(self) -> None:
        assert GitLabIssue.from_api({"iid": 1, "labels": ["bug"]}).labels == ["bug"]

    def test_a_system_note_is_flagged(self) -> None:
        """"Show me the comments" does not mean "changed the milestone"."""
        assert GitLabNote.from_api({"id": 1, "system": True}).system
        assert not GitLabNote.from_api({"id": 2}).system


# -- error classification ---------------------------------------------------


class TestGitHubErrors:
    def test_a_403_with_no_quota_left_is_retryable(self) -> None:
        """GitHub uses one status for 'wait' and 'never'; the remaining-quota
        header is what separates them."""
        error = github_classify(
            FakeResponse(
                403,
                {"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "retry-after": "60"},
            )
        )

        assert isinstance(error, GitHubRateLimited)
        assert not isinstance(error, NonRetryableError)
        assert error.retry_after == 60.0

    def test_a_403_with_quota_left_is_permanent(self) -> None:
        error = github_classify(
            FakeResponse(
                403,
                {"message": "Resource not accessible by personal access token"},
                headers={"x-ratelimit-remaining": "4999"},
            )
        )

        assert isinstance(error, GitHubPermanentError)
        assert not isinstance(error, GitHubRateLimited)

    def test_a_429_is_retryable(self) -> None:
        assert not isinstance(
            github_classify(FakeResponse(429, {"message": "slow down"})),
            NonRetryableError,
        )

    def test_a_404_says_it_might_be_a_permissions_problem(self) -> None:
        """GitHub returns 404 for a private resource, so a message that only
        says 'not found' sends someone hunting for a typo."""
        error = github_classify(FakeResponse(404, {"message": "Not Found"}))

        assert isinstance(error, GitHubNotFound)
        assert "cannot see" in str(error)

    def test_a_5xx_stays_retryable(self) -> None:
        assert not isinstance(
            github_classify(FakeResponse(502, None, text="bad gateway")),
            NonRetryableError,
        )

    def test_the_search_cap_is_one_thousand(self) -> None:
        assert SEARCH_TOTAL_CAP == 1_000


class TestGitLabErrors:
    def test_the_message_key_is_read(self) -> None:
        error = gitlab_classify(FakeResponse(400, {"message": "title is missing"}))

        assert "title is missing" in str(error)

    def test_the_error_key_is_read_too(self) -> None:
        """GitLab uses `message` on some endpoints and `error` on others."""
        error = gitlab_classify(FakeResponse(400, {"error": "invalid scope"}))

        assert "invalid scope" in str(error)

    def test_a_404_says_it_might_be_a_permissions_problem(self) -> None:
        error = gitlab_classify(FakeResponse(404, {"message": "404 Project Not Found"}))

        assert isinstance(error, GitLabNotFound)
        assert "cannot see" in str(error)

    def test_a_429_is_retryable_and_carries_retry_after(self) -> None:
        error = gitlab_classify(
            FakeResponse(429, {"message": "throttled"}, headers={"retry-after": "5"})
        )

        assert isinstance(error, GitLabRateLimited)
        assert not isinstance(error, NonRetryableError)
        assert error.retry_after == 5.0

    def test_a_401_is_an_auth_error(self) -> None:
        assert isinstance(
            gitlab_classify(FakeResponse(401, {"message": "401 Unauthorized"})),
            GitLabAuthError,
        )


# -- repo translation -------------------------------------------------------


class TestGitHubRepo:
    def test_full_name_is_the_handle_every_path_takes(self) -> None:
        repo = GitHubRepo.from_api(
            {"id": 1, "name": "hello", "full_name": "octocat/hello"}
        )

        assert repo.full_name == "octocat/hello"

    def test_a_null_description_does_not_raise(self) -> None:
        assert GitHubRepo.from_api({"id": 1, "description": None}).description == ""


class TestTheIssueListingFiltersPullRequests:
    """The headline gotcha, tested through the client rather than the model.

    Flagging a row as a pull request is not the same as leaving it out, and it
    is the leaving-out that makes "how many open issues" a right answer.
    """

    def _client(self, monkeypatch, rows):
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        client = GitHubClient()

        async def envelope(method, path, *, params=None, json=None):
            return {"items": rows, "headers": {}}

        monkeypatch.setattr(client, "_envelope", envelope)
        return client

    ROWS: ClassVar[list[dict]] = [
        {"number": 1, "title": "a bug"},
        {"number": 2, "title": "a PR", "pull_request": {"url": "…"}},
        {"number": 3, "title": "another bug"},
    ]

    async def test_pull_requests_are_left_out_by_default(self, monkeypatch) -> None:
        client = self._client(monkeypatch, self.ROWS)

        issues = await client.list_issues("octocat/hello")

        assert [i.number for i in issues] == [1, 3]
        assert not any(i.is_pull_request for i in issues)

    async def test_the_count_is_the_thing_that_would_have_been_wrong(
        self, monkeypatch
    ) -> None:
        """Three rows come back from GitHub; two of them are issues."""
        client = self._client(monkeypatch, self.ROWS)

        assert len(await client.list_issues("octocat/hello")) == 2

    async def test_they_can_be_asked_for(self, monkeypatch) -> None:
        client = self._client(monkeypatch, self.ROWS)

        both = await client.list_issues("octocat/hello", include_pull_requests=True)

        assert [i.number for i in both] == [1, 2, 3]

    async def test_filtering_keeps_the_coverage_flag(self, monkeypatch) -> None:
        """A plain comprehension would return a list and drop `.complete`,
        which is the exact loss `Results` exists to prevent."""
        client = self._client(monkeypatch, self.ROWS)

        issues = await client.list_issues("octocat/hello", limit=2)

        assert hasattr(issues, "complete")

    async def test_filtering_drops_the_total_rather_than_overstating_it(
        self, monkeypatch
    ) -> None:
        """The total counted pull requests too, so keeping it would report a
        number larger than the rows being returned."""
        client = self._client(monkeypatch, self.ROWS)

        issues = await client.list_issues("octocat/hello")

        assert issues.total is None


class TestGitLabNotesFilterSystemRecords:
    async def test_system_notes_are_left_out_by_default(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "x")
        client = GitLabClient()

        async def envelope(method, path, *, params=None, json=None):
            return {
                "items": [
                    {"id": 1, "body": "looks good", "system": False},
                    {"id": 2, "body": "changed the milestone", "system": True},
                ],
                "headers": {"x-next-page": ""},
            }

        monkeypatch.setattr(client, "_envelope", envelope)

        notes = await client.list_issue_notes("group/app", 42)

        assert [n.id for n in notes] == ["1"]
        assert hasattr(notes, "complete")
