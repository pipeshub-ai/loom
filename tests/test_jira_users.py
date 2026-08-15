"""Resolving a person to an accountId before putting them in JQL.

The gap this closes: the toolset could fetch the authenticated user and nothing
else, so a workflow asked about "Vishwjeet" had to put the display name straight
into JQL. That works until two people share a name or somebody is renamed, and
then it matches nothing rather than failing — the worst way to be wrong.
"""

from __future__ import annotations

import httpx
import pytest

from loom.toolsets.jira.client import JiraClient


def client(handler) -> JiraClient:
    jira = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")
    jira._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    return jira


class TestSearchUsers:
    async def test_it_returns_typed_users(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        async def fake_get(self, path, **params):
            captured["path"] = path
            captured["params"] = params
            return [
                {
                    "accountId": "712020:abc",
                    "displayName": "Vishwjeet",
                    "emailAddress": "v@example.com",
                    "active": True,
                }
            ]

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        jira = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

        users = await jira.search_users("Vishwjeet")

        assert captured["path"] == "user/search"
        assert captured["params"]["query"] == "Vishwjeet"
        assert users[0].account_id == "712020:abc"
        assert users[0].display_name == "Vishwjeet"
        assert users[0].active

    async def test_no_match_is_an_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Distinguishable from 'exists but has no matching issues'."""

        async def fake_get(self, path, **params):
            return []

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        jira = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

        assert await jira.search_users("Nobody") == []

    async def test_deactivated_users_are_flagged_not_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """They still own old issues, so the caller decides whether to skip them."""

        async def fake_get(self, path, **params):
            return [{"accountId": "1", "displayName": "Gone", "active": False}]

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        jira = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

        assert (await jira.search_users("Gone"))[0].active is False


class TestDiscoverability:
    def test_the_manifest_exposes_it(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        op = JIRA_MANIFEST.find_operation("users.search")
        assert op is not None
        assert op.function == "jira_search_users"

    def test_the_tool_docs_show_the_account_id_pattern(self) -> None:
        """The docs must teach the resolve-then-query shape, not just the call."""
        from loom.toolsets.jira.tools import JIRA_TOOL_DOCS

        assert "jira_search_users" in JIRA_TOOL_DOCS
        assert "account_id" in JIRA_TOOL_DOCS

    def test_the_docs_warn_about_project_specific_values(self) -> None:
        """The trap that made a correct query look broken."""
        from loom.toolsets.jira.tools import JIRA_TOOL_DOCS

        assert "In Progress" in JIRA_TOOL_DOCS
        assert "report the values that do" in JIRA_TOOL_DOCS


class TestResolveUserHandlesTypos:
    """One wrong letter should produce a suggestion, not a dead end.

    Jira's user search is a substring match. "Viswajeet" misses "Vishwjeet"
    entirely, and the empty list reads as "no such person" — so a workflow
    reports nothing found when the person is right there.
    """

    def _client(self, monkeypatch, responses):
        """A client whose user search replays *responses* keyed by query."""
        from loom.toolsets.jira.client import JiraClient

        async def fake_get(self, path, **params):
            return responses.get(params.get("query"), [])

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        return JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

    async def test_a_typo_resolves_to_the_closest_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jira = self._client(
            monkeypatch,
            {"Vis": [{"accountId": "1", "displayName": "Vishwjeet", "active": True}]},
        )

        found = await jira.resolve_user("Viswajeet")

        assert [u.display_name for u in found.matches] == ["Vishwjeet"]
        assert not found.exact, "a typo match must not claim to be exact"
        assert "suggestion" in found.note

    async def test_an_exact_hit_is_marked_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jira = self._client(
            monkeypatch,
            {"Vishwjeet": [{"accountId": "1", "displayName": "Vishwjeet", "active": True}]},
        )

        found = await jira.resolve_user("Vishwjeet")

        assert found.exact
        assert found.note == ""

    async def test_a_genuine_miss_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        jira = self._client(monkeypatch, {})

        found = await jira.resolve_user("Zzzznobody")

        assert found.matches == []
        assert not found.exact
        assert "no near match" in found.note

    async def test_a_distant_name_is_offered_but_not_endorsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returning candidates beats returning nothing; claiming a match does not."""
        jira = self._client(
            monkeypatch,
            {"Ali": [{"accountId": "9", "displayName": "Alison Fitzgerald-Smythe"}]},
        )

        found = await jira.resolve_user("Alibaba")

        assert not found.exact
        assert "none close enough" in found.note


class TestProjectMetadata:
    async def test_it_reports_the_names_a_query_may_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gap that made a correct query look broken."""
        from loom.toolsets.jira.client import JiraClient

        async def fake_get(self, path, **params):
            if path.endswith("/statuses"):
                return [
                    {"name": "Bug", "statuses": [{"name": "To Do"}, {"name": "Done"}]},
                    {"name": "Story", "statuses": [{"name": "In Progress"}]},
                ]
            return [{"name": "Highest"}, {"name": "High"}, {"name": "Medium"}]

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        jira = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")

        meta = await jira.get_metadata("QUES")

        assert meta.statuses == ["Done", "In Progress", "To Do"]
        assert meta.priorities == ["Highest", "High", "Medium"]
        assert meta.issue_types == ["Bug", "Story"]


class TestCoverage:
    def test_every_client_capability_is_exposed_as_a_tool(self) -> None:
        """Built-but-unreachable is the recurring defect in this codebase."""
        import inspect

        from loom.toolsets.jira.client import JiraClient
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        capabilities = {
            name
            for name, _ in inspect.getmembers(JiraClient, inspect.isfunction)
            if not name.startswith("_")
        }
        # Every public client method should be reachable from a documented tool.
        import loom.toolsets.jira.tools as tools

        source = inspect.getsource(tools)
        unreachable = [c for c in capabilities if f".{c}(" not in source]
        assert not unreachable, f"client methods with no tool: {sorted(unreachable)}"

        assert len(JIRA_MANIFEST.all_operations()) >= 16
