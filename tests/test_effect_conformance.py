"""Declared idempotence must match what the step actually does on retry.

`OperationSpec.idempotent` existed from the start, was surfaced in the catalog
and hashed into `steps.lock` — and was read by nothing. The retry decision was
written separately by hand in each `tools.py`, so the two drifted: 26 operations
declared `idempotent=False` and were configured `max_attempts=2`.

With two sources of truth and no check between them there was no way to tell
which half was wrong, which is the whole reason this file exists. The repo
already solves this shape for pagination — "the client is ground truth" — and
that check found six drifts on its first run.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import pytest

import loom.toolsets as toolsets_pkg


def _operations() -> list[tuple[str, Any, Any]]:
    """`(toolset id, OperationSpec, the @step it names)` for every shipped op."""
    rows: list[tuple[str, Any, Any]] = []
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
                if type(man).__name__ != "ToolsetManifest" or not man.tools_module:
                    continue
                try:
                    tools = importlib.import_module(man.tools_module)
                except Exception:  # pragma: no cover - optional extras
                    continue
                for op in man.all_operations():
                    fn = getattr(tools, op.function, None) if op.function else None
                    if fn is not None:
                        rows.append((man.id, op, fn))
    return rows


OPERATIONS = _operations()


class TestIdempotenceMatchesRetry:
    def test_no_operation_disagrees_with_itself(self) -> None:
        """The gate. A non-idempotent operation that retries files it twice; an
        idempotent one that does not retry fails a run for a blip."""
        problems = []
        for tid, op, fn in OPERATIONS:
            retry = getattr(fn, "retry", None)
            attempts = getattr(retry, "max_attempts", None) if retry else None
            if attempts is None:
                continue
            if op.idempotent != (attempts > 1):
                problems.append(
                    f"{tid}.{op.id}: declared idempotent={op.idempotent} but "
                    f"max_attempts={attempts}"
                )
        assert not problems, "\n".join(problems)

    def test_the_corpus_is_real(self) -> None:
        assert len(OPERATIONS) >= 300, f"only {len(OPERATIONS)} operations resolved"

    @pytest.mark.parametrize(
        "function",
        [
            "jira_create_issue",
            "jira_add_comment",
            "confluence_create_page",
            "confluence_add_comment",
            "calendar_create_event",
            "calendar_create_calendar",
            "calendar_quick_add_event",
            "gmail_create_label",
            # Not a duplicate but the same conclusion: a *permanent* delete
            # 404s on the second call, so a retry after a timeout that actually
            # succeeded turns a completed delete into a failed run.
            "drive_delete_file",
        ],
    )
    def test_the_operations_a_retry_would_duplicate_do_not_retry(
        self, function: str
    ) -> None:
        """These have no idempotency key. A timeout after the service accepted
        the request is indistinguishable from a failure, so a retry files a
        second issue or posts the comment twice — visibly, to everyone."""
        match = [(op, fn) for _t, op, fn in OPERATIONS if op.function == function]
        assert match, f"{function} not found"
        for op, fn in match:
            assert op.idempotent is False
            assert fn.retry.max_attempts == 1

    @pytest.mark.parametrize(
        "function",
        [
            "calendar_delete_event",
            "slack_delete_message",
            "zoom_delete_meeting",
            "jira_update_issue",
        ],
    )
    def test_operations_that_reach_the_same_end_state_may_retry(
        self, function: str
    ) -> None:
        """Naming the same resource twice reaches the same end state, so here
        the *declaration* was what was wrong, not the retry.

        The rule is not "deletes are idempotent" — `drive_delete_file` is a
        permanent delete that 404s on repeat, and is in the list above. What
        matters is whether the second call leaves the caller where the first
        one did, which is a question about the service, not about the verb."""
        match = [(op, fn) for _t, op, fn in OPERATIONS if op.function == function]
        assert match, f"{function} not found"
        for op, fn in match:
            assert op.idempotent is True
            assert fn.retry.max_attempts > 1


class TestTheConformanceKit:
    """`verify_effect_profile` is what extends these checks past this
    repository. At a thousand toolsets most of them are not here and never run
    this file — the same reason `verify_event_log` and `verify_event_source`
    are shipped rather than kept internal."""

    def test_a_shipped_toolset_passes(self) -> None:
        from pathlib import Path

        import loom.toolsets.google.gmail.tools as gmail_tools
        from loom.testing.conformance import verify_effect_profile
        from loom.toolsets.google.gmail.manifest import GMAIL_MANIFEST

        verify_effect_profile(
            GMAIL_MANIFEST,
            tools_module=gmail_tools,
            client_source=Path(
                "src/loom/toolsets/google/gmail/client.py"
            ).read_text(encoding="utf-8"),
        )

    def test_an_undeclared_effect_is_caught(self) -> None:
        from loom.testing.conformance import verify_effect_profile
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="t", version="1.0.0", summary="s",
            groups={"g": [OperationSpec(id="g.x", summary="s", function="x")]},
        )
        with pytest.raises(AssertionError, match="no declared effect class"):
            verify_effect_profile(manifest)

    def test_a_dangling_inverse_is_caught(self) -> None:
        from loom.testing.conformance import verify_effect_profile
        from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="t", version="1.0.0", summary="s",
            groups={
                "g": [
                    OperationSpec(
                        id="g.trash", summary="s", function="trash",
                        effect=EffectClass.DESTRUCTIVE,
                        reversible=True, undone_by="g.nope",
                    )
                ]
            },
        )
        with pytest.raises(AssertionError, match="names no operation here"):
            verify_effect_profile(manifest)

    def test_the_verb_check_is_skipped_not_silently_passed(self) -> None:
        """A check that cannot run has found nothing. Without `client_source`
        the verb comparison does not happen — and must not be reported as a
        pass, which is why it is a separate argument rather than a default."""
        import inspect

        from loom.testing.conformance import verify_effect_profile

        assert "client_source" in inspect.signature(verify_effect_profile).parameters
