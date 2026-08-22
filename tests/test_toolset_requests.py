"""What each toolset client actually *sends*.

The other toolset suites cover auth, paging dialects, error classification, and
model translation. This one covers the layer between them — how a method turns
its arguments into a request — because that is where the per-vendor traps live
and every one of them fails silently:

- a SOQL ``WHERE`` built wrong returns zero rows and no error
- GitLab's ``assignee`` filter is ``assignee_username``; the obvious name is
  ignored
- GitLab closes with ``state_event``, not ``state``
- HubSpot needs filters wrapped in a ``filterGroups`` list
- ClickUp picks between two endpoints depending on which id you have
- Asana returns almost nothing unless ``opt_fields`` asks

A recording transport captures the request each method makes, so these assert
the shape rather than mocking it away.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import pytest


class Recorder:
    """Stands in for the client's transport and remembers every call."""

    def __init__(self, *responses: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) or [{}]

    def __call__(self, method: str, path: str, **kw: Any):
        self.calls.append({"method": method, "path": path, **kw})
        payload = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )

        async def answer():
            return payload

        return answer()

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]

    @property
    def params(self) -> dict[str, Any]:
        return self.last.get("params") or {}

    @property
    def body(self) -> dict[str, Any]:
        return self.last.get("json") or {}

    def sent(self, key: str) -> Any:
        return self.params.get(key)


def attach(client: Any, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> Recorder:
    """Replace ``_request`` and record what the client would have sent."""
    recorder = Recorder(*responses)
    monkeypatch.setattr(client, "_request", recorder)
    return recorder


def attach_envelope(
    client: Any, monkeypatch: pytest.MonkeyPatch, rows: Any, headers: Any = None
) -> Recorder:
    """Same, for the clients whose paging reads an ``{items, headers}`` envelope."""
    recorder = Recorder({"items": rows, "headers": headers or {}})
    monkeypatch.setattr(client, "_envelope", recorder)
    return recorder


# -- Salesforce --------------------------------------------------------------


@pytest.fixture
def salesforce(monkeypatch):
    from loom.toolsets.salesforce.client import SalesforceClient

    return SalesforceClient(instance_url="https://acme.my.salesforce.com",
                            access_token="tok")


class TestSalesforceBuildsSafeSoql:
    async def test_a_name_filter_becomes_a_like_clause(
        self, salesforce, monkeypatch
    ) -> None:
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_accounts("ACME")

        soql = rec.sent("q")
        assert "FROM Account" in soql
        assert "Name LIKE '%ACME%'" in soql

    async def test_an_empty_query_omits_the_where_clause(
        self, salesforce, monkeypatch
    ) -> None:
        """Not ``WHERE Name LIKE '%%'`` — a stray WHERE is a syntax error, and
        an empty LIKE is a full scan wearing a filter's clothes."""
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_accounts("")

        assert "WHERE" not in rec.sent("q")

    async def test_an_apostrophe_is_escaped_into_the_query(
        self, salesforce, monkeypatch
    ) -> None:
        """O'Brien unescaped terminates the literal and changes the query."""
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_contacts("O'Brien")

        assert r"O\'Brien" in rec.sent("q")

    async def test_filters_compose_with_and(self, salesforce, monkeypatch) -> None:
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_opportunities(
            "renewal", account_id="001x", stage="Negotiation", open_only=True
        )

        soql = rec.sent("q")
        assert soql.count(" AND ") == 3
        assert "IsClosed = false" in soql
        assert "StageName = 'Negotiation'" in soql

    async def test_the_opportunity_query_selects_stagename(
        self, salesforce, monkeypatch
    ) -> None:
        """`Stage` silently returns nothing; the field is `StageName`."""
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_opportunities()

        assert "StageName" in rec.sent("q")

    async def test_a_limit_reaches_the_query(self, salesforce, monkeypatch) -> None:
        rec = attach(salesforce, monkeypatch, {"done": True, "records": []})

        await salesforce.find_accounts("x", limit=7)

        assert "LIMIT 7" in rec.sent("q")


class TestSalesforceWrites:
    async def test_create_posts_to_the_object_collection(
        self, salesforce, monkeypatch
    ) -> None:
        rec = attach(salesforce, monkeypatch, {"id": "001", "success": True})

        await salesforce.create_record("Lead", {"LastName": "Chen"})

        assert rec.last["method"] == "POST"
        assert rec.last["path"].endswith("/sobjects/Lead")
        assert rec.last["json"] == {"LastName": "Chen"}

    async def test_update_patches_and_reports_success_despite_no_body(
        self, salesforce, monkeypatch
    ) -> None:
        """Salesforce answers a PATCH with 204 and nothing, so the result is
        constructed rather than leaving a caller to tell empty from failed."""
        rec = attach(salesforce, monkeypatch, {})

        result = await salesforce.update_record("Account", "001x", {"Name": "New"})

        assert rec.last["method"] == "PATCH"
        assert result.success and result.id == "001x"

    async def test_delete_uses_the_record_path(self, salesforce, monkeypatch) -> None:
        rec = attach(salesforce, monkeypatch, {})

        await salesforce.delete_record("Account", "001x")

        assert rec.last["method"] == "DELETE"
        assert rec.last["path"].endswith("/sobjects/Account/001x")

    async def test_get_record_asks_only_for_requested_fields(
        self, salesforce, monkeypatch
    ) -> None:
        rec = attach(salesforce, monkeypatch, {"Id": "001"})

        await salesforce.get_record("Account", "001x", fields=["Name", "Phone"])

        assert rec.sent("fields") == "Name,Phone"


# -- HubSpot -----------------------------------------------------------------


@pytest.fixture
def hubspot(monkeypatch):
    from loom.toolsets.hubspot.client import HubSpotClient

    return HubSpotClient(access_token="pat-x")


class TestHubSpotRequests:
    async def test_filters_are_wrapped_in_a_filter_group(
        self, hubspot, monkeypatch
    ) -> None:
        """HubSpot ANDs within a group and ORs between them, so a flat list
        from a caller means 'all of these' and belongs in one group."""
        rec = attach(hubspot, monkeypatch, {"results": []})

        await hubspot.search_objects(
            "contacts",
            filters=[{"propertyName": "email", "operator": "EQ", "value": "a@b.c"}],
        )

        assert rec.body["filterGroups"] == [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": "a@b.c"}]}
        ]

    async def test_search_posts_to_the_search_path(self, hubspot, monkeypatch) -> None:
        rec = attach(hubspot, monkeypatch, {"results": []})

        await hubspot.search_objects("deals", query="acme")

        assert rec.last["method"] == "POST"
        assert rec.last["path"] == "/crm/v3/objects/deals/search"
        assert rec.body["query"] == "acme"

    async def test_search_limit_is_capped_at_two_hundred(
        self, hubspot, monkeypatch
    ) -> None:
        rec = attach(hubspot, monkeypatch, {"results": []})

        await hubspot.search_objects("contacts", limit=5000)

        assert rec.body["limit"] == 200

    async def test_default_properties_are_requested_per_object_type(
        self, hubspot, monkeypatch
    ) -> None:
        """Omitting `properties` returns HubSpot's sparse default, which reads
        as a contact with no company rather than as an unasked question."""
        rec = attach(hubspot, monkeypatch, {"results": []})

        await hubspot.list_objects("deals")

        asked = rec.sent("properties")
        assert "dealname" in asked and "amount" in asked

    async def test_explicit_properties_win(self, hubspot, monkeypatch) -> None:
        rec = attach(hubspot, monkeypatch, {"results": []})

        await hubspot.list_objects("contacts", properties=["email"])

        assert rec.sent("properties") == "email"

    async def test_lookup_by_email_uses_the_id_property_route(
        self, hubspot, monkeypatch
    ) -> None:
        rec = attach(hubspot, monkeypatch, {"id": "1", "properties": {}})

        await hubspot.get_contact_by_email("ada@example.com")

        assert rec.last["path"].endswith("/contacts/ada@example.com")
        assert rec.sent("idProperty") == "email"

    async def test_create_wraps_properties(self, hubspot, monkeypatch) -> None:
        rec = attach(hubspot, monkeypatch, {"id": "1", "properties": {}})

        await hubspot.create_object("contacts", {"email": "a@b.c"})

        assert rec.body == {"properties": {"email": "a@b.c"}}

    async def test_archive_deletes(self, hubspot, monkeypatch) -> None:
        rec = attach(hubspot, monkeypatch, {})

        await hubspot.archive_object("deals", "42")

        assert rec.last["method"] == "DELETE"
        assert rec.last["path"].endswith("/deals/42")

    async def test_associations_use_the_association_path(
        self, hubspot, monkeypatch
    ) -> None:
        rec = attach(hubspot, monkeypatch, {"results": [{"id": "9"}]})

        found = await hubspot.get_associations("contacts", "1", "deals")

        assert rec.last["path"].endswith("/contacts/1/associations/deals")
        assert found == ["9"]


# -- GitHub ------------------------------------------------------------------


@pytest.fixture
def github(monkeypatch):
    from loom.toolsets.github.client import GitHubClient

    return GitHubClient(token="ghp_x")


class TestGitHubRequests:
    async def test_issue_filters_reach_the_query(self, github, monkeypatch) -> None:
        rec = attach_envelope(github, monkeypatch, [])

        await github.list_issues(
            "octocat/hello", state="closed", labels="bug,ui", assignee="ada"
        )

        assert rec.last["path"] == "/repos/octocat/hello/issues"
        assert rec.sent("state") == "closed"
        assert rec.sent("labels") == "bug,ui"
        assert rec.sent("assignee") == "ada"

    async def test_unset_filters_never_reach_the_wire(
        self, github, monkeypatch
    ) -> None:
        """httpx encodes None as the string "None", which GitHub then matches
        against nothing — so an omitted filter would silently return no issues.

        Asserted through the client's own ``_clean``: the stub replaces
        ``_envelope``, which is where cleaning happens, so checking the
        recorder alone would be checking the stub.
        """
        from loom.toolsets.github.client import _clean

        rec = attach_envelope(github, monkeypatch, [])

        await github.list_issues("octocat/hello")

        assert rec.params["labels"] is None
        assert _clean(rec.params).keys() == {"state", "page", "per_page"}

    async def test_update_sends_only_what_was_passed(
        self, github, monkeypatch
    ) -> None:
        """A blanket payload would clear the title and body of any issue
        someone only meant to close."""
        rec = attach(github, monkeypatch, {"number": 1})

        await github.update_issue("octocat/hello", 1, state="closed")

        assert rec.body == {"state": "closed"}

    async def test_labels_can_be_cleared_explicitly(self, github, monkeypatch) -> None:
        """An empty list means "remove them all" and must not be confused with
        "leave them alone", which is what omitting the argument means."""
        rec = attach(github, monkeypatch, {"number": 1})

        await github.update_issue("octocat/hello", 1, labels=[])

        assert rec.body == {"labels": []}

    async def test_creating_a_pull_request_sends_head_and_base(
        self, github, monkeypatch
    ) -> None:
        rec = attach(github, monkeypatch, {"number": 5})

        await github.create_pull_request(
            "octocat/hello", "Fix", head="feature/x", base="main", draft=True
        )

        assert rec.last["path"] == "/repos/octocat/hello/pulls"
        assert rec.body["head"] == "feature/x"
        assert rec.body["base"] == "main"
        assert rec.body["draft"] is True

    async def test_a_pull_request_is_fetched_from_the_pulls_path(
        self, github, monkeypatch
    ) -> None:
        """Not from /issues, where the id means something else."""
        rec = attach(github, monkeypatch, {"number": 5})

        await github.get_pull_request("octocat/hello", 5)

        assert rec.last["path"] == "/repos/octocat/hello/pulls/5"

    async def test_search_sends_the_query_as_q(self, github, monkeypatch) -> None:
        rec = attach_envelope(github, monkeypatch, {"items": [], "total_count": 0})

        await github.search_issues("repo:octocat/hello is:open")

        assert rec.last["path"] == "/search/issues"
        assert rec.sent("q") == "repo:octocat/hello is:open"

    async def test_listing_your_own_repos_uses_the_user_path(
        self, github, monkeypatch
    ) -> None:
        rec = attach_envelope(github, monkeypatch, [])

        await github.list_repos()

        assert rec.last["path"] == "/user/repos"

    async def test_listing_someone_elses_repos_names_them(
        self, github, monkeypatch
    ) -> None:
        rec = attach_envelope(github, monkeypatch, [])

        await github.list_repos("octocat")

        assert rec.last["path"] == "/users/octocat/repos"


# -- GitLab ------------------------------------------------------------------


@pytest.fixture
def gitlab(monkeypatch):
    monkeypatch.delenv("GITLAB_OAUTH_TOKEN", raising=False)
    from loom.toolsets.gitlab.client import GitLabClient

    return GitLabClient(token="glpat-x")


class TestGitLabRequests:
    async def test_a_project_path_is_encoded_into_the_url(
        self, gitlab, monkeypatch
    ) -> None:
        rec = attach_envelope(gitlab, monkeypatch, [])

        await gitlab.list_issues("group/app")

        assert rec.last["path"] == "/projects/group%2Fapp/issues"

    async def test_the_assignee_filter_uses_gitlabs_own_name(
        self, gitlab, monkeypatch
    ) -> None:
        """``assignee`` is ignored; the parameter is ``assignee_username``, and
        the wrong one returns every issue rather than an error."""
        rec = attach_envelope(gitlab, monkeypatch, [])

        await gitlab.list_issues("7", assignee="ada")

        assert rec.sent("assignee_username") == "ada"
        assert "assignee" not in rec.params

    async def test_closing_sends_a_state_event_not_a_state(
        self, gitlab, monkeypatch
    ) -> None:
        """GitLab takes the transition. Sending ``state="closed"`` changes
        nothing and reports success."""
        rec = attach(gitlab, monkeypatch, {"iid": 1})

        await gitlab.update_issue("7", 1, state_event="close")

        assert rec.body == {"state_event": "close"}
        assert "state" not in rec.body

    async def test_labels_are_sent_comma_joined(self, gitlab, monkeypatch) -> None:
        """GitLab takes a string here where GitHub takes a list."""
        rec = attach(gitlab, monkeypatch, {"iid": 1})

        await gitlab.create_issue("7", "Bug", labels=["bug", "ui"])

        assert rec.body["labels"] == "bug,ui"

    async def test_assignee_ids_are_sent_as_integers(
        self, gitlab, monkeypatch
    ) -> None:
        rec = attach(gitlab, monkeypatch, {"iid": 1})

        await gitlab.create_issue("7", "Bug", assignee_ids=["42", "not-an-id"])

        assert rec.body["assignee_ids"] == [42]

    async def test_a_merge_request_marks_a_draft_in_its_title(
        self, gitlab, monkeypatch
    ) -> None:
        """GitLab has no draft flag on create; the prefix is the mechanism."""
        rec = attach(gitlab, monkeypatch, {"iid": 1})

        await gitlab.create_merge_request(
            "7", "Add login", source_branch="f", target_branch="main", draft=True
        )

        assert rec.body["title"] == "Draft: Add login"

    async def test_a_non_draft_title_is_untouched(self, gitlab, monkeypatch) -> None:
        rec = attach(gitlab, monkeypatch, {"iid": 1})

        await gitlab.create_merge_request(
            "7", "Add login", source_branch="f", target_branch="main"
        )

        assert rec.body["title"] == "Add login"

    async def test_notes_are_posted_to_the_issue_note_path(
        self, gitlab, monkeypatch
    ) -> None:
        rec = attach(gitlab, monkeypatch, {"id": 1})

        await gitlab.add_issue_note("group/app", 42, "looks good")

        assert rec.last["path"] == "/projects/group%2Fapp/issues/42/notes"
        assert rec.body == {"body": "looks good"}


# -- Asana -------------------------------------------------------------------


@pytest.fixture
def asana(monkeypatch):
    from loom.toolsets.asana.client import AsanaClient

    return AsanaClient(access_token="1/x")


class TestAsanaRequests:
    async def test_opt_fields_are_always_requested(self, asana, monkeypatch) -> None:
        """Without them Asana returns a gid and a name, which reads as a task
        with no assignee rather than as an under-specified request."""
        rec = attach(asana, monkeypatch, {"data": [], "next_page": None})

        await asana.list_tasks("120")

        assert "assignee.name" in rec.sent("opt_fields")

    async def test_a_task_created_with_no_home_falls_back_to_the_workspace(
        self, asana, monkeypatch
    ) -> None:
        """A task needs a project, a parent, or a workspace; sending none is a
        400 that names no field."""
        rec = attach(asana, monkeypatch, {"data": {"gid": "1"}})

        await asana.create_task("999", name="Fix")

        assert rec.body["data"]["workspace"] == "999"

    async def test_a_project_makes_the_workspace_unnecessary(
        self, asana, monkeypatch
    ) -> None:
        rec = attach(asana, monkeypatch, {"data": {"gid": "1"}})

        await asana.create_task("999", name="Fix", projects=["120"])

        assert rec.body["data"]["projects"] == ["120"]
        assert "workspace" not in rec.body["data"]

    async def test_search_uses_asanas_dotted_filter_names(
        self, asana, monkeypatch
    ) -> None:
        rec = attach(asana, monkeypatch, {"data": []})

        await asana.search_tasks("999", text="login", assignee_gid="42",
                                 project_gids=["1", "2"])

        assert rec.last["path"] == "/workspaces/999/tasks/search"
        assert rec.sent("assignee.any") == "42"
        assert rec.sent("projects.any") == "1,2"

    async def test_comments_are_filtered_from_the_story_feed(
        self, asana, monkeypatch
    ) -> None:
        """The feed also records every field change, and "show me the
        comments" does not mean that."""
        attach(
            asana,
            monkeypatch,
            {
                "data": [
                    {"gid": "1", "text": "hi", "type": "comment"},
                    {"gid": "2", "text": "changed due date", "type": "system"},
                ]
            },
        )

        notes = await asana.list_comments("1")

        assert [n.gid for n in notes] == ["1"]


# -- ClickUp -----------------------------------------------------------------


@pytest.fixture
def clickup(monkeypatch):
    monkeypatch.delenv("CLICKUP_OAUTH_TOKEN", raising=False)
    from loom.toolsets.clickup.client import ClickUpClient

    return ClickUpClient(api_token="pk_x")


class TestClickUpRequests:
    async def test_a_folder_id_picks_the_folder_endpoint(
        self, clickup, monkeypatch
    ) -> None:
        rec = attach(clickup, monkeypatch, {"lists": []})

        await clickup.list_lists(folder_id="55")

        assert rec.last["path"] == "/folder/55/list"

    async def test_a_space_id_picks_the_folderless_endpoint(
        self, clickup, monkeypatch
    ) -> None:
        """Folderless lists are invisible through the folder route, which is
        why both exist."""
        rec = attach(clickup, monkeypatch, {"lists": []})

        await clickup.list_lists(space_id="99")

        assert rec.last["path"] == "/space/99/list"

    async def test_neither_id_is_refused_rather_than_guessed(self, clickup) -> None:
        from loom.toolsets.clickup.client import ClickUpPermanentError

        with pytest.raises(ClickUpPermanentError, match="space_id or folder_id"):
            await clickup.list_lists()

    async def test_assignees_are_sent_as_integers(self, clickup, monkeypatch) -> None:
        """ClickUp rejects them as strings."""
        rec = attach(clickup, monkeypatch, {"id": "1"})

        await clickup.create_task("901", "Fix", assignees=["42", "bad"])

        assert rec.body["assignees"] == [42]

    async def test_unset_task_fields_are_omitted_entirely(
        self, clickup, monkeypatch
    ) -> None:
        rec = attach(clickup, monkeypatch, {"id": "1"})

        await clickup.create_task("901", "Fix")

        assert rec.body == {"name": "Fix"}

    async def test_a_comment_echoes_its_text_because_clickup_does_not(
        self, clickup, monkeypatch
    ) -> None:
        """The create response carries an id and little else, so the text is
        echoed from the request rather than reported as empty."""
        attach(clickup, monkeypatch, {"id": "7"})

        made = await clickup.create_comment("86a1", "looks good")

        assert made.id == "7" and made.text == "looks good"

    async def test_members_are_filtered_by_the_query(
        self, clickup, monkeypatch
    ) -> None:
        """ClickUp has no member-search endpoint, so the narrowing happens
        here — and it is a substring match, not a ranked search."""
        attach(
            clickup,
            monkeypatch,
            {
                "teams": [
                    {
                        "id": "7",
                        "members": [
                            {"user": {"id": 1, "username": "priya", "email": "p@x.io"}},
                            {"user": {"id": 2, "username": "sam", "email": "s@x.io"}},
                        ],
                    }
                ]
            },
        )

        found = await clickup.find_members("7", "priya")

        assert [u.username for u in found] == ["priya"]

    async def test_a_member_search_ignores_other_workspaces(
        self, clickup, monkeypatch
    ) -> None:
        attach(
            clickup,
            monkeypatch,
            {
                "teams": [
                    {"id": "8", "members": [{"user": {"id": 1, "username": "priya"}}]}
                ]
            },
        )

        assert await clickup.find_members("7", "") == []


def test_the_recorder_captures_what_it_claims() -> None:
    """Guards the harness itself: a recorder that dropped its arguments would
    make every assertion above pass vacuously."""
    rec = Recorder({"ok": True})
    rec("GET", "/x", params={"a": 1}, json={"b": 2})

    assert rec.last["method"] == "GET"
    assert rec.sent("a") == 1
    assert rec.body == {"b": 2}
    assert jsonlib.dumps(rec.body)


class TestEveryToolsetCanBeRepointed:
    """Configuration a host must be able to change without editing the library.

    Each of these was a literal until an audit asked the question. The pattern
    is the same one the store layer follows: what varies per deployment is a
    constructor argument, not a constant.
    """

    def test_github_sends_a_configurable_api_version(self, github) -> None:
        """GitHub pins behaviour to the dated contract, so an Enterprise Server
        on an older one — or a host wanting a newer one — must be able to say."""
        from loom.toolsets.github.client import DEFAULT_API_VERSION, GitHubClient

        assert github._api_version == DEFAULT_API_VERSION
        assert GitHubClient(token="x", api_version="2099-01-01")._api_version == (
            "2099-01-01"
        )

    async def test_the_github_version_reaches_the_request(self, monkeypatch) -> None:
        """A parameter nothing sends is not configuration."""
        import inspect

        from loom.toolsets.github.client import GitHubClient

        source = inspect.getsource(GitHubClient._envelope)

        assert '"X-GitHub-Api-Version": self._api_version' in source

    def test_gitlab_takes_an_api_generation(self, gitlab) -> None:
        from loom.toolsets.gitlab.client import DEFAULT_API_VERSION, GitLabClient

        assert gitlab._api_version == DEFAULT_API_VERSION
        assert GitLabClient(token="x", api_version="v5")._api_version == "v5"

    async def test_the_gitlab_version_reaches_the_path(self, monkeypatch) -> None:
        import inspect

        from loom.toolsets.gitlab.client import GitLabClient

        source = inspect.getsource(GitLabClient._envelope)

        assert "/api/{self._api_version}" in source

    def test_clickup_and_asana_repoint_through_base_url(self, monkeypatch) -> None:
        """Their version lives in the base URL, so overriding that is the whole
        knob — no separate parameter needed, and one would be a second way to
        say the same thing."""
        from loom.toolsets.asana.client import AsanaClient
        from loom.toolsets.clickup.client import ClickUpClient

        repointed = ClickUpClient(api_token="x", base_url="https://x/api/v3")
        assert repointed._base_url.endswith("v3")
        assert AsanaClient(access_token="y",
                           base_url="https://x/api/2.0")._base_url.endswith("2.0")

    def test_every_client_takes_a_timeout(self, monkeypatch) -> None:
        """A default that cannot be raised is a workflow that cannot talk to a
        slow instance."""
        import importlib
        import inspect

        for toolset, klass in (
            ("salesforce", "SalesforceClient"),
            ("hubspot", "HubSpotClient"),
            ("github", "GitHubClient"),
            ("gitlab", "GitLabClient"),
            ("asana", "AsanaClient"),
            ("clickup", "ClickUpClient"),
        ):
            module = importlib.import_module(f"loom.toolsets.{toolset}.client")
            params = inspect.signature(getattr(module, klass).__init__).parameters
            assert "timeout" in params, f"{toolset} has no timeout"


class TestHubSpotHasAnIdentityCheck:
    """The one gap the consistency audit found: five toolsets had a whoami and
    HubSpot had none, so there was no way to answer "is this token live?"."""

    async def test_it_reads_the_account_details_endpoint(
        self, hubspot, monkeypatch
    ) -> None:
        rec = attach(hubspot, monkeypatch, {"portalId": 12345, "accountType": "STANDARD"})

        account = await hubspot.account_info()

        assert rec.last["path"] == "/account-info/v3/details"
        assert account.portal_id == "12345"
        assert account.account_type == "STANDARD"

    def test_it_is_named_for_what_it_returns(self) -> None:
        """Not `whoami`: a private app token authenticates an app against a
        portal, not a person, so there is no user to name."""
        from loom.toolsets.hubspot.manifest import HUBSPOT_MANIFEST

        ops = {o.function for o in HUBSPOT_MANIFEST.all_operations()}

        assert "hubspot_account_info" in ops
        assert "hubspot_whoami" not in ops
