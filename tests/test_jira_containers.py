"""Resolving a container — a project, an epic — before putting it in JQL.

The gap this closes, observed end to end: asked for "tickets past due date in
the saas epic", the coding agent wrote ``issuetype = Epic AND summary ~ "saas"``
because the toolset declared no way to resolve an epic. That query is correct
Jira and the right shape, and the toolset offered nothing better — so
``ResolutionStage`` flagged it, every repair round rewrote it into another
spelling of itself, and the run burned three rounds and eight minutes on a
finding with no passing state.

Two halves, and both are needed. Here: a declared resolver, so the lookup is a
call rather than a query shape. In ``tests/test_hardening_p2.py``: the check
reading a query's scope, so a namespace search is no longer indistinguishable
from a blind text match.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.toolsets.jira.client import JiraClient, _jql_literal


def _client() -> JiraClient:
    return JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")


def _issue(key: str, summary: str) -> Any:
    """A row shaped as ``search_issues`` returns it — typed, not raw payload.

    Faking the raw shape here would let ``resolve_epic`` pass a test while
    failing on the attribute access it does in production.
    """
    from loom.toolsets.jira.models import JiraIssue

    return JiraIssue(
        key=key, summary=summary, issue_type="Epic", project=key.split("-")[0]
    )


class TestJqlLiteral:
    """A name reaches JQL as a quoted literal, so it has to be escaped.

    Not hypothetical: an epic named ``Q3 "stretch" goals`` is ordinary, and
    unescaped it ends the string early and the remainder parses as JQL.
    """

    def test_a_plain_name_is_quoted(self) -> None:
        assert _jql_literal("saas") == '"saas"'

    def test_a_quote_is_escaped(self) -> None:
        assert _jql_literal('stretch "goals"') == '"stretch \\"goals\\""'

    def test_a_backslash_is_escaped_before_the_quote_rule_runs(self) -> None:
        # Escaping quotes first would turn `\` + `"` into `\\"`, which closes
        # the string: the backslash escapes the backslash and the quote is live.
        assert _jql_literal('a\\"b') == '"a\\\\\\"b"'


class TestResolveEpic:
    """An epic is the awkward container: it *is* an issue, so there is no
    endpoint listing epics and the only lookup is a scoped JQL search."""

    async def test_it_scopes_the_search_to_the_epic_issue_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scope is the whole point — without it the same query searches
        every issue on the site and returns whatever mentions the word."""
        seen: dict[str, Any] = {}

        async def fake_search(self, jql, max_results=20, **kw):
            seen["jql"] = jql
            return [_issue("PA-1844", "saas")]

        monkeypatch.setattr(JiraClient, "search_issues", fake_search)
        found = await _client().resolve_epic("saas")

        assert "issuetype = Epic" in seen["jql"]
        assert '"saas"' in seen["jql"]
        assert found.exact is True
        assert [i.key for i in found.matches] == ["PA-1844"]

    async def test_a_known_project_narrows_the_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_search(self, jql, max_results=20, **kw):
            seen["jql"] = jql
            return [_issue("PA-1844", "saas")]

        monkeypatch.setattr(JiraClient, "search_issues", fake_search)
        await _client().resolve_epic("saas", project="PA")

        assert seen["jql"].startswith('project = "PA" AND issuetype = Epic')

    async def test_several_near_matches_are_reported_as_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure one step later than the one this exists to prevent:
        taking the first of several is still a guess."""

        async def fake_search(self, jql, max_results=20, **kw):
            return [
                _issue("PA-1844", "SaaS billing"),
                _issue("PA-1769", "SaaS onboarding"),
            ]

        monkeypatch.setattr(JiraClient, "search_issues", fake_search)
        found = await _client().resolve_epic("saas")

        assert found.exact is False
        assert len(found.matches) == 2
        assert "ambiguous" in found.note.lower()

    async def test_an_exact_name_wins_over_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_search(self, jql, max_results=20, **kw):
            return [
                _issue("PA-1900", "SaaS billing"),
                _issue("PA-1844", "saas"),
            ]

        monkeypatch.setattr(JiraClient, "search_issues", fake_search)
        found = await _client().resolve_epic("SAAS")

        assert found.exact is True
        assert [i.key for i in found.matches] == ["PA-1844"]

    async def test_nothing_found_says_so_and_names_the_alternatives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"No epic bears this name" is a fact a caller acts on, and it is a
        different fact from "no issues matched"."""

        async def fake_search(self, jql, max_results=20, **kw):
            return []

        monkeypatch.setattr(JiraClient, "search_issues", fake_search)
        found = await _client().resolve_epic("saas")

        assert found.matches == []
        assert found.exact is False
        assert "board" in found.note and "label" in found.note


class TestResolveProject:
    """``project = "PipesHub AI"`` matches no project, and Jira answers a
    filter on a project that does not exist with zero issues and no error."""

    @staticmethod
    def _projects(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str]]) -> None:
        from loom.toolsets.jira.models import JiraProject

        async def fake_list(self):
            return [
                JiraProject(key=k, name=n, id=str(i))
                for i, (k, n) in enumerate(rows)
            ]

        monkeypatch.setattr(JiraClient, "list_projects", fake_list)

    async def test_a_key_matches_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._projects(monkeypatch, [("PA", "PipesHub AI"), ("QQ", "Quest")])
        found = await _client().resolve_project("pa")

        assert found.exact is True
        assert found.matches[0].key == "PA"

    async def test_a_full_name_matches_exactly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._projects(monkeypatch, [("PA", "PipesHub AI"), ("QQ", "Quest")])
        found = await _client().resolve_project("PipesHub AI")

        assert found.exact is True
        assert found.matches[0].key == "PA"

    async def test_one_containing_match_is_returned_labelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._projects(monkeypatch, [("PA", "PipesHub AI"), ("QQ", "Quest")])
        found = await _client().resolve_project("pipeshub")

        assert found.exact is False
        assert found.matches[0].key == "PA"
        assert "only one" in found.note

    async def test_several_containing_matches_are_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._projects(
            monkeypatch, [("PA", "PipesHub AI"), ("PB", "PipesHub Backend")]
        )
        found = await _client().resolve_project("pipeshub")

        assert len(found.matches) == 2
        assert "do not assume the first" in found.note

    async def test_no_match_names_the_other_namespaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A word that is not a project is usually a different container, and
        saying so is what stops the caller falling back to a text match."""
        self._projects(monkeypatch, [("QQ", "Quest"), ("ZZ", "Zebra")])
        found = await _client().resolve_project("saas")

        assert found.exact is False
        assert "epic" in found.note


class TestTheManifestDeclaresThem:
    """The declaration is what the coding agent and ``ResolutionStage`` read.

    A resolver that exists and is not declared is one nothing can find: the
    agent is never told to call it, and the check has no vocabulary to
    recognise a query scoped by it.
    """

    def test_epic_and_project_are_declared_resolvers(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        resolvers = JIRA_MANIFEST.resolvers()

        assert resolvers["epic"].id == "issues.resolve_epic"
        assert resolvers["project"].id == "projects.resolve"

    def test_every_resolver_is_a_read(self) -> None:
        """A resolver runs while authoring, through ``call_read_operation``,
        which refuses anything that is not a read."""
        from loom.toolsets.jira.manifest import JIRA_MANIFEST
        from loom.toolsets.manifest import EffectClass

        for kind, op in JIRA_MANIFEST.resolvers().items():
            assert op.effect is EffectClass.READ, kind
