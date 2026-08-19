"""``EffectProfile`` and its derivation — Phase 12, step 2.

Nothing consumes this yet, so these tests are the only thing holding the shape.
They are written against the two facts the derivation is built on, both measured
against the shipped toolsets rather than assumed: an HTTP verb implies the
declared class in 89 of 91 recoverable cases, and a read-only scope has never
disagreed with a READ declaration in 31 of 31.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from loom.toolsets.effects import (
    EffectProfile,
    derive_effect_profile,
    verb_disagreement,
)
from loom.toolsets.manifest import EffectClass, OperationSpec


def op(**kw) -> OperationSpec:
    return OperationSpec(id=kw.pop("id", "g.x"), summary="s", **kw)


class TestPrecedence:
    """Declared beats verb beats scope beats default."""

    def test_a_declaration_wins_over_the_verb(self) -> None:
        p = derive_effect_profile(op(effect=EffectClass.DESTRUCTIVE), verb="GET")
        assert p.effect is EffectClass.DESTRUCTIVE
        assert p.source == "declared"

    def test_the_verb_is_used_when_nobody_declared(self) -> None:
        for verb, expected in [
            ("GET", EffectClass.READ),
            ("POST", EffectClass.WRITE),
            ("PATCH", EffectClass.WRITE),
            ("DELETE", EffectClass.DESTRUCTIVE),
        ]:
            p = derive_effect_profile(op(), verb=verb)
            assert p.effect is expected, verb
            assert p.source == "derived"

    def test_a_readonly_scope_is_used_when_there_is_no_verb(self) -> None:
        p = derive_effect_profile(op(scopes=["https://x/auth/drive.readonly"]))
        assert p.effect is EffectClass.READ
        assert p.source == "derived"

    def test_nothing_at_all_falls_back_to_write(self) -> None:
        p = derive_effect_profile(op())
        assert p.effect is EffectClass.WRITE
        assert p.source == "default"

    def test_an_unknown_verb_does_not_derive(self) -> None:
        """A verb the table does not know must not silently mean READ."""
        p = derive_effect_profile(op(), verb="TRACE")
        assert p.effect is EffectClass.WRITE
        assert p.source == "default"


class TestDerivationNeverLowersADeclaration:
    """An author who wrote DESTRUCTIVE over a GET knows something the verb does
    not — a soft-delete endpoint, a request that triggers a workflow."""

    @pytest.mark.parametrize(
        ("declared", "verb"),
        [
            (EffectClass.DESTRUCTIVE, "GET"),
            (EffectClass.WRITE, "GET"),
            (EffectClass.DESTRUCTIVE, "POST"),
        ],
    )
    def test_stricter_declarations_survive(self, declared, verb) -> None:
        assert derive_effect_profile(op(effect=declared), verb=verb).effect is declared

    def test_and_are_not_reported_as_disagreements(self) -> None:
        assert verb_disagreement(op(effect=EffectClass.DESTRUCTIVE), "GET") is None


class TestVerbDisagreement:
    """The other direction is a likely mistake — and is reported, not applied."""

    def test_a_read_that_issues_a_delete_is_reported(self) -> None:
        msg = verb_disagreement(op(id="files.trash", effect=EffectClass.READ), "DELETE")
        assert msg is not None
        assert "files.trash" in msg
        assert "destructive" in msg

    def test_agreement_is_silent(self) -> None:
        assert verb_disagreement(op(effect=EffectClass.READ), "GET") is None
        assert verb_disagreement(op(effect=EffectClass.DESTRUCTIVE), "DELETE") is None

    def test_an_undeclared_operation_is_not_a_disagreement(self) -> None:
        """It has not made a claim to disagree with — that is CERT-04's job."""
        assert verb_disagreement(op(), "DELETE") is None

    def test_no_recovered_verb_is_silent(self) -> None:
        assert verb_disagreement(op(effect=EffectClass.READ), "") is None


class TestFacets:
    def test_idempotence_defaults_to_whether_it_reads(self) -> None:
        assert derive_effect_profile(op(), verb="GET").idempotent is True
        assert derive_effect_profile(op(), verb="POST").idempotent is False

    def test_a_declared_idempotence_wins(self) -> None:
        p = derive_effect_profile(op(idempotent=True), verb="DELETE")
        assert p.idempotent is True

    def test_access_control_is_not_derived_from_scopes(self) -> None:
        """Measured: it cannot be. Google covers permissions with the broad
        scope — drive_share_file and drive_remove_permission both declare
        ``auth/drive``, identical to an ordinary write — and the Microsoft
        toolsets declare no scopes at all. A derivation here would match zero
        shipped operations while reading as though it had computed something."""
        assert not derive_effect_profile(op(scopes=["drive.permissions"])).access_control
        assert not derive_effect_profile(op(scopes=["https://www.googleapis.com/auth/drive"])).access_control

    def test_open_world_follows_whether_there_is_a_client(self) -> None:
        assert derive_effect_profile(op(), has_client=True).open_world is True
        assert derive_effect_profile(op(), has_client=False).open_world is False


class TestIrreversible:
    """The predicate an approval gate should use — not ``effect is DESTRUCTIVE``."""

    def test_outside_and_unrecoverable(self) -> None:
        assert EffectProfile(open_world=True, reversible=False).irreversible is True

    def test_recoverable_is_not_irreversible_however_destructive(self) -> None:
        p = EffectProfile(
            effect=EffectClass.DESTRUCTIVE, open_world=True, reversible=True
        )
        assert p.irreversible is False

    def test_local_is_not_irreversible(self) -> None:
        assert EffectProfile(open_world=False, reversible=False).irreversible is False

    def test_the_gmail_inversion_this_exists_to_fix(self) -> None:
        """Ranked by ``effect`` alone, trashing outranks sending. Ranked by
        ``irreversible``, which is the question a human is being asked, it does
        not."""
        trash = EffectProfile(
            effect=EffectClass.DESTRUCTIVE, open_world=True, reversible=True,
            undone_by="messages.untrash",
        )
        send = EffectProfile(effect=EffectClass.WRITE, open_world=True)
        assert trash.effect is EffectClass.DESTRUCTIVE and not trash.irreversible
        assert send.effect is EffectClass.WRITE and send.irreversible


class TestProfileIsAValue:
    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            EffectProfile().effect = EffectClass.READ  # type: ignore[misc]

    def test_equal_by_value(self) -> None:
        assert derive_effect_profile(op(), verb="GET") == derive_effect_profile(
            op(), verb="GET"
        )


class TestAgainstTheShippedCorpus:
    """The derivation's premises, re-checked against real manifests rather than
    against fixtures written to agree with them."""

    def test_a_readonly_scope_never_contradicts_a_read_declaration(self) -> None:
        import importlib
        import pkgutil

        import loom.toolsets as pkg
        from loom.toolsets.effects import scope_is_readonly

        contradictions = []
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if not mod.name.endswith(".manifest"):
                continue
            try:
                loaded = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover - optional extras
                continue
            for value in vars(loaded).values():
                for m in value if isinstance(value, list | tuple) else [value]:
                    if type(m).__name__ != "ToolsetManifest":
                        continue
                    for spec in m.all_operations():
                        if (
                            scope_is_readonly(spec.scopes)
                            and spec.effect is not EffectClass.READ
                        ):
                            contradictions.append(
                                f"{m.id}.{spec.id}={spec.effect.value}"
                            )
        assert not contradictions, (
            "a read-only scope implied READ for all 31 shipped operations that "
            f"carry one; these now contradict it: {contradictions}"
        )

    def test_the_profile_round_trips_a_json_loaded_operation(self) -> None:
        """Third-party manifests arrive as data, not Python literals."""
        spec = OperationSpec.model_validate_json(
            json.dumps({"id": "g.x", "summary": "s", "effect": "destructive"})
        )
        assert derive_effect_profile(spec, verb="GET").source == "declared"


class TestReadonlyScopeMatching:
    """``.read`` is a prefix of ``.readwrite``. A bare substring test therefore
    reads ``Sites.ReadWrite.All`` as read-only — which fired the moment real
    Graph scopes were added to OneDrive and SharePoint, and would have
    classified every SharePoint write as a read."""

    @pytest.mark.parametrize(
        ("scopes", "expected"),
        [
            (["Files.Read"], True),
            (["Sites.Read.All"], True),
            (["https://www.googleapis.com/auth/drive.readonly"], True),
            (["channels:read"], True),
            (["Files.ReadWrite"], False),
            (["Sites.ReadWrite.All"], False),
            (["channels:manage"], False),
            (["Mail.Send"], False),
            ([], False),
        ],
    )
    def test_matching(self, scopes, expected) -> None:
        from loom.toolsets.effects import scope_is_readonly

        assert scope_is_readonly(scopes) is expected

    def test_one_broad_scope_disqualifies_the_set(self) -> None:
        """Granted together, the broad one is what the credential can do."""
        from loom.toolsets.effects import scope_is_readonly

        assert scope_is_readonly(["Files.Read", "Files.ReadWrite"]) is False


class TestArgumentDependentEffects:
    """For the handful of operations whose class is a property of the *call*
    rather than of the operation. `io.http_request` is one node with one class,
    and it is what a generated workflow reaches for when no toolset covers the
    API — so `method="DELETE"` reading as a write is a real hole."""

    def _spec(self):
        from loom.nodes.registry import get_node_catalog, load_builtin_nodes

        load_builtin_nodes()
        return get_node_catalog().get("io.http_request")

    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            ("GET", EffectClass.READ),
            ("HEAD", EffectClass.READ),
            ("POST", EffectClass.WRITE),
            ("PATCH", EffectClass.WRITE),
            ("DELETE", EffectClass.DESTRUCTIVE),
        ],
    )
    def test_the_method_decides(self, method, expected) -> None:
        from loom.toolsets.effects import resolve_effect

        spec = self._spec()
        assert resolve_effect(spec.effect, spec.effect_by, {"method": method}) is expected

    def test_an_unlisted_method_keeps_the_declared_class(self) -> None:
        """Cautious, not read. An unrecognised method must not fall through."""
        from loom.toolsets.effects import resolve_effect

        spec = self._spec()
        got = resolve_effect(spec.effect, spec.effect_by, {"method": "TRACE"})
        assert got is EffectClass.WRITE

    def test_a_missing_argument_keeps_the_declared_class(self) -> None:
        from loom.toolsets.effects import resolve_effect

        spec = self._spec()
        assert resolve_effect(spec.effect, spec.effect_by, {}) is EffectClass.WRITE

    def test_an_operation_with_no_table_is_unaffected(self) -> None:
        from loom.toolsets.effects import resolve_effect

        assert (
            resolve_effect(EffectClass.READ, {}, {"method": "DELETE"})
            is EffectClass.READ
        )

    def test_matching_is_case_insensitive_for_strings(self) -> None:
        from loom.toolsets.effects import resolve_effect

        spec = self._spec()
        assert (
            resolve_effect(spec.effect, spec.effect_by, {"method": "delete"})
            is EffectClass.DESTRUCTIVE
        )
