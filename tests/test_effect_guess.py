"""The name heuristic, scored against every operation LOOM ships.

`_guess_effect` classifies any toolset built with `from_steps` / `from_callables`
— which is every user-written toolset that does not hand-write a manifest. It
used to return `READ` for anything it did not recognise, and READ is the one
class exempt from every write and destructive control, so an unrecognised verb
was not flagged, it was *granted*.

The assertion here is deliberately asymmetric. Over-classifying costs an
approval; under-classifying costs a deletion. So over-classification is allowed
and under-classification is a hard failure.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import loom.toolsets as toolsets_pkg
from loom.agents.tool_registry import _guess_effect, _resolve_guess
from loom.toolsets.manifest import EffectClass

_RANK = {EffectClass.READ: 0, EffectClass.WRITE: 1, EffectClass.DESTRUCTIVE: 2}


def _corpus() -> list[tuple[str, str, EffectClass]]:
    """`(toolset, function, declared effect)` for every shipped operation.

    Ground truth, because these 320 were hand-classified with the vendor's
    documentation open — see `docs/design/*-toolsets.md`.
    """
    seen: set[tuple[str, str]] = set()
    rows: list[tuple[str, str, EffectClass]] = []
    for mod in pkgutil.walk_packages(
        toolsets_pkg.__path__, toolsets_pkg.__name__ + "."
    ):
        if not mod.name.endswith(".manifest"):
            continue
        try:
            loaded = importlib.import_module(mod.name)
        except Exception:  # pragma: no cover - optional extras
            continue
        for value in vars(loaded).values():
            for man in value if isinstance(value, list | tuple) else [value]:
                if type(man).__name__ != "ToolsetManifest":
                    continue
                for op in man.all_operations():
                    key = (man.id, op.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append((man.id, op.function or op.id, op.effect))
    return rows


CORPUS = _corpus()


class TestTheHeuristicNeverUnderClassifies:
    def test_zero_under_classifications(self) -> None:
        """The gate. Was 46 of 320 before the verb lists were grown from these
        very misses — including seven destructive operations guessed READ:
        archive, trash, unshare, end."""
        under = [
            f"{tid}.{fn}: declared {truth.value}, guessed {_resolve_guess(fn).value}"
            for tid, fn, truth in CORPUS
            if _RANK[_resolve_guess(fn)] < _RANK[truth]
        ]
        assert not under, "\n".join(under)

    def test_the_corpus_is_real(self) -> None:
        """A corpus that silently emptied would make this suite vacuous."""
        assert len(CORPUS) >= 300

    def test_accuracy_has_not_regressed(self) -> None:
        """Not a gate — a tripwire. Over-classification is safe but a heuristic
        that got much blunter would make read-only toolsets unusable."""
        exact = sum(1 for _t, fn, truth in CORPUS if _resolve_guess(fn) == truth)
        assert exact / len(CORPUS) >= 0.88, f"exact accuracy fell to {exact}/{len(CORPUS)}"


class TestAbstention:
    """`None` means the name carries no signal — which is not the same as
    'this is a read', and conflating the two is what made the old fallback
    dangerous."""

    @pytest.mark.parametrize(
        "name", ["frobnicate", "xyzzy", "handle_thing", "process", "do_it"]
    )
    def test_an_unrecognised_name_abstains(self, name: str) -> None:
        assert _guess_effect(name) is None

    @pytest.mark.parametrize(
        "name", ["gmail_send_message", "drive_delete_file", "jira_search_issues"]
    )
    def test_a_recognised_name_does_not(self, name: str) -> None:
        assert _guess_effect(name) is not None

    def test_abstention_resolves_to_write_not_read(self) -> None:
        """The direction that costs an approval rather than a deletion."""
        assert _resolve_guess("frobnicate") is EffectClass.WRITE

    def test_abstention_is_logged_with_the_name(self, caplog) -> None:
        """Every operation classified WRITE by default is safe and useless —
        a read-only resolve hands the agent nothing. The log is what turns
        that from a mystery into `effects={...}`."""
        import logging

        with caplog.at_level(logging.INFO, logger="loom.agents.tool_registry"):
            _resolve_guess("frobnicate")
        assert "frobnicate" in caplog.text


class TestTheVerbsThatUsedToBeMissed:
    """Each of these was a real operation the old list read as harmless."""

    @pytest.mark.parametrize(
        "name",
        [
            "slack_archive_channel",
            "gmail_trash_message",
            "gmail_trash_thread",
            "drive_trash_file",
            "hubspot_archive_object",
            "calendar_unshare_calendar",
            "meet_end_active_conference",
        ],
    )
    def test_destructive_operations_are_no_longer_reads(self, name: str) -> None:
        assert _guess_effect(name) is EffectClass.DESTRUCTIVE

    @pytest.mark.parametrize(
        "name",
        [
            "gmail_reply_to_message",
            "gmail_forward_message",
            "drive_upload_file",
            "drive_share_file",
            "onedrive_invite",
            "calendar_move_event",
            "outlook_cancel_event",
            "gitlab_close_issue",
            "asana_complete_task",
            "onenote_append_to_page",
        ],
    )
    def test_writes_are_no_longer_reads(self, name: str) -> None:
        assert _guess_effect(name) is not EffectClass.READ
