"""The CI gate: a declared effect that contradicts its own client fails here.

This is what makes the classification scale. Hand-declared effects are correct
today because 320 of them were written carefully; the 321st is written by
somebody who has not read this file, and `_guess_effect` already shows what
happens to a hand-maintained classification left unchecked — 14% of it drifts,
one-directionally, toward more permitted.

The check is static: `toolsets/derive.py` reads `client.py` and `tools.py` with
`ast`, so it costs no imports and no credentials and can run in a pre-commit
hook.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import loom.toolsets as toolsets_pkg
from loom.toolsets.derive import verbs_for_manifest, verbs_in_client, wiring_in_tools
from loom.toolsets.effects import verb_disagreement
from loom.toolsets.manifest import EffectClass, OperationSpec


def _shipped() -> list[tuple[str, OperationSpec, str]]:
    """Every shipped operation for which a client verb could be recovered."""
    rows: list[tuple[str, OperationSpec, str]] = []
    for mod in pkgutil.walk_packages(toolsets_pkg.__path__, toolsets_pkg.__name__ + "."):
        if not mod.name.endswith(".manifest"):
            continue
        try:
            loaded = importlib.import_module(mod.name)
        except Exception:  # pragma: no cover - optional extras
            continue
        verbs = verbs_for_manifest(Path(loaded.__file__ or ""))
        if not verbs:
            continue
        for value in vars(loaded).values():
            for man in value if isinstance(value, list | tuple) else [value]:
                if type(man).__name__ != "ToolsetManifest":
                    continue
                for op in man.all_operations():
                    verb = verbs.get(op.function or "")
                    if verb:
                        rows.append((man.id, op, verb))
    return rows


class TestTheGate:
    def test_no_declared_effect_contradicts_its_client(self) -> None:
        """The whole point. A `DELETE` declared read is how `drive_trash_file`
        would read as harmless to a read-only agent."""
        problems = [
            verb_disagreement(op, verb)
            for _tid, op, verb in _shipped()
            if verb_disagreement(op, verb)
        ]
        assert not problems, "\n".join(p for p in problems if p)

    def test_the_gate_is_not_vacuous(self) -> None:
        """A check that silently matched nothing would pass forever."""
        rows = _shipped()
        assert len(rows) >= 60, f"only {len(rows)} operations resolved to a verb"

    def test_it_catches_a_seeded_contradiction(self) -> None:
        """Proves the gate fires, rather than trusting that it would."""
        seeded = OperationSpec(
            id="files.trash",
            function="drive_trash_file",
            summary="Move a file to the bin.",
            effect=EffectClass.READ,
        )
        assert verb_disagreement(seeded, "DELETE") is not None


class TestVerbRecovery:
    def test_a_single_verb_is_recovered(self) -> None:
        assert verbs_in_client(
            'class C:\n'
            '    async def get_thing(self):\n'
            '        return await self._request("GET", "/x")\n'
        ) == {"get_thing": "GET"}

    def test_two_verbs_yield_nothing(self) -> None:
        """A method that reads then deletes is destructive, and which literal
        you pick decides whether it looks harmless."""
        assert verbs_in_client(
            'class C:\n'
            '    async def replace(self):\n'
            '        await self._request("GET", "/x")\n'
            '        return await self._request("DELETE", "/x")\n'
        ) == {}

    def test_the_helper_itself_is_not_a_caller(self) -> None:
        assert "_request" not in verbs_in_client(
            'class C:\n'
            '    async def _request(self, method, path):\n'
            '        return await self._send("GET", path)\n'
        )

    def test_a_keyword_verb_is_recovered(self) -> None:
        assert verbs_in_client(
            'class C:\n'
            '    async def d(self):\n'
            '        return await self._request(method="DELETE", path="/x")\n'
        ) == {"d": "DELETE"}

    def test_tool_wiring_links_to_one_client_method(self) -> None:
        assert wiring_in_tools(
            "async def gmail_send_message(to):\n"
            "    return await get_default_client().send_message(to)\n"
        ) == {"gmail_send_message": "send_message"}

    def test_a_missing_client_file_is_not_an_error(self) -> None:
        assert verbs_for_manifest(Path("/nonexistent/manifest.py")) == {}


class TestPostIsNotAContradiction:
    """GET means read and DELETE means destroy in essentially every REST API.
    POST means both — create, and query-by-body. Both exceptions across the
    shipped toolsets are the second kind."""

    @pytest.mark.parametrize(
        "function", ["hubspot_search_objects", "outlook_get_schedule"]
    )
    def test_the_two_known_search_by_post_operations_are_silent(
        self, function: str
    ) -> None:
        rows = [(op, verb) for _t, op, verb in _shipped() if op.function == function]
        assert rows, f"{function} no longer resolves to a verb"
        for op, verb in rows:
            assert verb == "POST"
            assert verb_disagreement(op, verb) is None


class TestCoverageIsReportedNotHidden:
    """Verb recovery reaches some clients and not others, and that has to stay
    visible. A toolset whose client routes through a shared session — Google's
    `GoogleSession`, Slack, Zoom, most of Graph — issues no verb literal this
    pass can see, so its operations are simply not checked here. A green gate
    over an empty set is the failure this guards against."""

    def test_the_covered_toolsets_are_what_we_think_they_are(self) -> None:
        covered = {tid for tid, _op, _verb in _shipped()}
        # Recorded rather than asserted loosely: if a client is refactored onto
        # a shared session, coverage drops and this names which one.
        assert {"asana", "clickup", "github", "gitlab", "hubspot", "salesforce"} <= (
            covered
        ), f"lost verb coverage; now covering {sorted(covered)}"

    def test_uncovered_toolsets_are_not_silently_passing(self) -> None:
        """Google Drive's delete is destructive and this pass cannot see it —
        so the gate must not be read as 'every effect is verified'."""
        covered_fns = {op.function for _t, op, _v in _shipped()}
        assert "drive_delete_file" not in covered_fns, (
            "drive_delete_file is now recoverable — good; widen the coverage "
            "assertion above and delete this test"
        )
