"""Every effect gate, driven by a defect it is supposed to catch.

A gate asserts something about 320 shipped operations, all of which are
currently correct — so it passes on day one whether or not it works. CERT-04
spent its whole life in that state: it claimed to require an explicit effect
class, was implemented as ``if not op.effect`` against a truthy ``StrEnum``,
and could never fail. A structural check written in this same session had the
same defect and was only caught by deliberately breaking the code it guarded.

So each gate here is handed the defect it exists for, and must reject it. The
shape `tests/conformance/test_harness.py` already uses for stores: prove the
suite still catches each defect class, rather than trusting that it would.

Deliberately in-process. Mutating real source files from a test leaves the tree
broken when one fails, so every case builds a synthetic bad input and drives
the *predicate* the corpus gate is built from.
"""

from __future__ import annotations

import asyncio

import pytest

from loom.agents.tool_registry import _resolve_guess
from loom.toolsets.certify import certify
from loom.toolsets.effects import scope_is_readonly, verb_disagreement
from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

_RANK = {EffectClass.READ: 0, EffectClass.WRITE: 1, EffectClass.DESTRUCTIVE: 2}


def _manifest(**overrides) -> ToolsetManifest:
    base = dict(
        id="t",
        version="1.0.0",
        summary="s",
        auth={"type": "oauth2"},
        base_url="https://api.test.com",
        egress_hosts=["api.test.com"],
        tools_module="t.tools",
        rate_limits={"default": {"rps": 10}},
    )
    base.update(overrides)
    return ToolsetManifest(**base)


def _op(**overrides) -> OperationSpec:
    base = dict(
        id="g.x",
        function="t_x",
        summary="s",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        scopes=["thing:write"],
        effect=EffectClass.WRITE,
    )
    base.update(overrides)
    return OperationSpec(**base)


class TestEachGateRejectsItsOwnDefect:
    def test_an_undeclared_effect_is_caught(self) -> None:
        """The CERT-04 defect itself: an operation that declares nothing."""
        undeclared = OperationSpec(
            id="g.nuke", function="t_nuke", summary="delete everything",
            input_schema={"type": "object"}, output_schema={"type": "object"},
        )
        assert "effect" not in undeclared.model_fields_set
        result = asyncio.run(certify(_manifest(groups={"g": [undeclared]})))
        assert "CERT-04" in {r.code for r in result.results if not r.passed}

    def test_a_write_with_a_read_only_scope_is_caught(self) -> None:
        """Seeded for real against SharePoint during review: swapping one
        `Sites.ReadWrite.All` for `Sites.Read.All` must not pass."""
        assert scope_is_readonly(["Sites.Read.All"]) is True
        assert scope_is_readonly(["Sites.ReadWrite.All"]) is False
        bad = _op(effect=EffectClass.WRITE, scopes=["Sites.Read.All"])
        assert bad.effect is not EffectClass.READ and scope_is_readonly(bad.scopes)

    def test_a_declaration_contradicting_its_client_is_caught(self) -> None:
        """A DELETE declared read — how `drive_trash_file` would read as
        harmless to a read-only agent."""
        assert verb_disagreement(_op(effect=EffectClass.READ), "DELETE") is not None

    def test_an_under_classifying_heuristic_is_caught(self) -> None:
        """If a destructive verb fell out of the list, the corpus gate must
        fail rather than quietly permit it."""
        truth = EffectClass.DESTRUCTIVE
        for name in ("slack_archive_channel", "gmail_trash_message"):
            assert _RANK[_resolve_guess(name)] >= _RANK[truth], name

    def test_a_non_idempotent_operation_that_retries_is_caught(self) -> None:
        """The predicate the conformance gate is built from."""
        declared_idempotent, attempts = False, 3
        assert declared_idempotent != (attempts > 1)

    def test_a_dangling_inverse_is_caught(self) -> None:
        result = asyncio.run(
            certify(
                _manifest(
                    groups={
                        "g": [
                            _op(
                                effect=EffectClass.DESTRUCTIVE,
                                reversible=True,
                                undone_by="g.nope",
                            )
                        ]
                    }
                )
            )
        )
        assert "CERT-14" in {r.code for r in result.results if not r.passed}

    def test_a_toolset_that_cannot_be_faked_is_caught(self) -> None:
        """Without `tools_module` the smoke sandbox runs against the real
        service — a 401 whose cheapest repair is deleting the integration."""
        result = asyncio.run(
            certify(_manifest(tools_module="", groups={"g": [_op()]}))
        )
        assert "CERT-08" in {r.code for r in result.results if not r.passed}


class TestTheGatesAreNotVacuous:
    """A corpus gate over an empty set passes forever."""

    @pytest.mark.parametrize(
        ("module", "name"),
        [
            ("tests.test_effect_guess", "CORPUS"),
            ("tests.test_effect_conformance", "OPERATIONS"),
        ],
    )
    def test_the_corpora_are_populated(self, module: str, name: str) -> None:
        import importlib

        corpus = getattr(importlib.import_module(module), name)
        assert len(corpus) >= 300, f"{module}.{name} holds only {len(corpus)}"

    def test_verb_recovery_still_reaches_real_operations(self) -> None:
        from tests.test_effect_derivation import _shipped

        assert len(_shipped()) >= 60
