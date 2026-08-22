"""Building a toolset client from declared values rather than the environment.

Twenty-seven toolsets each build a client from `os.environ` into a module-level
singleton, on first use, for the life of the process. The constructors already
take parameters and nothing on the call path can pass them: every one of the 390
call sites is a `@step` carrying business arguments only.

`AuthField.arg` and `AuthSpec.client` are what make construction one generic
function instead of twenty-seven factories. Being declarations, they can be
wrong in a way a docstring cannot — so this suite reads the **client
constructor** and holds the manifest to it, in both directions. That is the
shape `tests/test_manifest_imports.py` uses for import lines and
`tests/test_manifest_auth.py` for credential names; the first found six drifts
on its first run.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import re
from typing import ClassVar

import pytest

from loom.toolsets.manifest import AuthSpec, ToolsetManifest
from loom.toolsets.registry import builtin_catalog

#: Toolsets that have not declared construction yet.
#:
#: An explicit list, not a silent skip: a toolset that is neither declared nor
#: named here fails, so a new one cannot arrive without either. The list only
#: ever shrinks, and when it empties the check below deletes itself.
PENDING: frozenset[str] = frozenset()


def _manifests() -> list[tuple[str, ToolsetManifest]]:
    catalog = builtin_catalog()
    return [(tid, catalog.get(tid)) for tid in sorted(catalog.toolset_ids)]


def _load(path: str) -> type:
    module_name, _, symbol = path.partition(":")
    return getattr(importlib.import_module(module_name), symbol)


def _environment_reads(client_path: str) -> set[str]:
    """Every environment variable a client's own module reads.

    Read from the source rather than by importing and probing, because the read
    happens inside `__init__` and probing it would mean constructing a client
    with no credentials — which is exactly what this whole change makes raise.
    """
    module_name, _, _ = client_path.partition(":")
    source = pathlib.Path(importlib.import_module(module_name).__file__ or "")
    if not source.exists():
        return set()
    return set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source.read_text()))


def _declared() -> list[tuple[str, AuthSpec]]:
    return [(tid, m.auth) for tid, m in _manifests()
            if getattr(m, "auth", None) and m.auth.client]


class TestEveryToolsetIsAccountedFor:
    def test_declared_or_explicitly_pending(self) -> None:
        """Coverage can only go up. A toolset added tomorrow with neither a
        declaration nor an entry here fails, rather than quietly joining the
        set that still reads the environment."""
        undeclared = {
            tid for tid, m in _manifests()
            if not (getattr(m, "auth", None) and m.auth.client)
        }
        assert undeclared <= PENDING, (
            f"undeclared and not listed as pending: {sorted(undeclared - PENDING)}"
        )

    def test_pending_does_not_outlive_its_toolsets(self) -> None:
        """A name left here after its toolset is declared, or renamed away,
        makes the list above lie about what is left to do."""
        ids = {tid for tid, _ in _manifests()}
        declared = {tid for tid, _ in _declared()}
        assert not (PENDING - ids), f"pending names no such toolset: {sorted(PENDING - ids)}"
        assert not (PENDING & declared), (
            f"declared but still listed pending: {sorted(PENDING & declared)}"
        )


class TestTheClientPathResolves:
    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_it_imports(self, toolset: str, spec: AuthSpec) -> None:
        """A path that names nothing is worse than none: `client_for` would
        fail at call time, inside a run, rather than here."""
        assert inspect.isclass(_load(spec.client))


class TestEveryArgIsARealParameter:
    """The direction that catches a rename: a constructor parameter renamed
    without the manifest following leaves `arg` naming nothing, and the generic
    factory would raise `TypeError` deep inside a run."""

    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_args_exist_on_the_constructor(self, toolset: str, spec: AuthSpec) -> None:
        parameters = inspect.signature(_load(spec.client).__init__).parameters
        for field in spec.fields:
            if not field.arg:
                continue
            assert field.arg in parameters, (
                f"{toolset}: {field.name} declares arg={field.arg!r}, which is "
                f"not a parameter of {spec.client}"
            )


class TestNothingReachesAClientUndeclared:
    """The other direction, and the one that catches a *silent* gap.

    An earlier version of this asked which constructor parameters looked
    credential-shaped, by taking a `None` default as the tell. That is a guess,
    and it was wrong immediately: `transport` defaults to `None` on four clients
    and is a test seam, not a secret. So this reads the **client source** for
    what it actually pulls out of the environment and requires a declaration for
    each — the shape `test_manifest_auth.py` already uses, and the only version
    with no heuristic in it.

    A variable the client reads and the manifest does not declare is one the
    generic factory can never supply, so that client keeps its environment read
    forever and the migration quietly stops one toolset short.
    """

    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_every_variable_the_client_reads_is_declared(
        self, toolset: str, spec: AuthSpec
    ) -> None:
        read = _environment_reads(spec.client)
        if not read:
            pytest.skip("this client reads no environment variable")
        declared = {f.name for f in spec.fields}
        assert read <= declared, (
            f"{toolset}: {sorted(read - declared)} are read by {spec.client} and "
            f"declared by nothing, so no provider can supply them"
        )

    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_a_directly_mapped_toolset_routes_each_one(
        self, toolset: str, spec: AuthSpec
    ) -> None:
        """Declaring a variable without an `arg` names it for `loom doctor` and
        still leaves the factory unable to pass it."""
        if spec.credentials:
            pytest.skip("credentials object carries the mapping")
        read = _environment_reads(spec.client)
        routed = {f.name for f in spec.fields if f.arg}
        assert read <= routed, (
            f"{toolset}: {sorted(read - routed)} are declared but carry no arg"
        )


class TestTheTwoShapesAreDistinguishable:
    """Most clients take their values directly; Google, Microsoft and Zoom take
    a credentials object whose own `from_env` already maps every variable.
    Declaring both would be two copies of one mapping."""

    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_a_toolset_uses_one_or_the_other(self, toolset: str, spec: AuthSpec) -> None:
        maps_directly = any(f.arg for f in spec.fields)
        assert not (maps_directly and spec.credentials), (
            f"{toolset} declares both arg mappings and a credentials class"
        )
        if spec.kind == "none" and not spec.fields:
            # A service needing no credential — DuckDuckGo — legitimately has
            # neither. Keyed on the declaration rather than on an empty field
            # list alone, so a toolset that merely forgot its fields still fails.
            return
        assert maps_directly or spec.credentials, (
            f"{toolset} declares a client but no way to supply it"
        )

    @pytest.mark.parametrize(("toolset", "spec"), _declared(),
                             ids=[t for t, _ in _declared()])
    def test_an_auth_class_publishes_one_construction_path(
        self, toolset: str, spec: AuthSpec
    ) -> None:
        """`from_values(mapping, scopes=…)` is the whole contract the factory
        needs from this family.

        This asserted `from_env` while `credentials` named the *holder*. It
        passed, and the factory was handing that holder to the client as its
        `auth` — an object with no `headers()`. The declaration now names the
        auth class, so this names what an auth class must publish."""
        if not spec.credentials:
            pytest.skip("maps fields directly")
        assert callable(getattr(_load(spec.credentials), "from_values", None))


class TestEveryDeclaredToolsetActuallyBuilds:
    """The check that was missing, and the bug it would have caught.

    `build_client` passed `token_source=` to every toolset declaring a
    credential — correct for the one client that had been migrated to accept
    it, and a `TypeError` at construction for the other fourteen. The whole
    suite stayed green: the declaration tests above check what a manifest
    *says*, and nothing put a manifest and its client together.

    Constructing all 27 is cheap — no network, no credentials beyond the
    placeholder values here — and it is the only test that exercises the
    manifest, the resolver and the constructor as one thing.
    """

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    async def test_it_constructs_from_a_full_environment(self, toolset: str) -> None:
        from loom.toolsets.factory import build_client
        from loom.toolsets.resolution import EnvironmentProvider, ToolsetSession

        spec = builtin_catalog().get(toolset).auth
        environ = {f.name: "value" for f in spec.fields}
        resolved = await ToolsetSession(
            providers=(EnvironmentProvider(environ=environ),)
        ).resolve(toolset, spec)

        client = build_client(spec, resolved)

        assert client is not None

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    async def test_nothing_configured_refuses_rather_than_half_building(
        self, toolset: str
    ) -> None:
        """The other half: a client that cannot be configured must fail here,
        naming the variables, rather than construct and 401 against the vendor
        later — where the traceback names somebody else's API for a problem
        that is entirely local."""
        from loom.core.exceptions import CredentialNotFound
        from loom.toolsets.factory import build_client
        from loom.toolsets.resolution import ToolsetSession

        spec = builtin_catalog().get(toolset).auth
        resolved = await ToolsetSession(providers=()).resolve(toolset, spec)
        if resolved.complete:
            pytest.skip("this toolset needs no configuration")

        with pytest.raises(CredentialNotFound):
            build_client(spec, resolved)


class TestABuiltClientCanActuallyAuthenticate:
    """The test that was missing, and the bug it let through.

    `build_client` handed twelve clients their *credentials holder* where an
    auth object was wanted. It constructs without complaint — nothing checks the
    type — and raises `AttributeError: 'GoogleCredentials' object has no
    attribute 'headers'` on the first request.

    The whole suite stayed green because nothing drove a built client past its
    constructor. `assert client is not None` is true of a client that cannot
    make a single call, which is the check-that-cannot-fail this codebase names
    everywhere else.

    One step past construction is all it takes, and it needs no network: ask for
    the headers the next request would carry.
    """

    #: A ready-made token per provider, so the assertion below needs no network.
    #:
    #: Deliberately not "every toolset with every field set": the refresh and
    #: service-account flows *mint*, so asking them for headers reaches a token
    #: endpoint with a placeholder secret and fails for reasons that say nothing
    #: about this bug. The ready-made-token mode is the one that exercises the
    #: object's shape and nothing else — which is precisely what F1 broke.
    READY_MADE: ClassVar[dict[str, dict[str, str]]] = {
        "loom.toolsets.google.auth:GoogleAuth": {"GOOGLE_ACCESS_TOKEN": "ya29.test"},
        "loom.toolsets.microsoft.auth:MicrosoftAuth": {"MS_GRAPH_ACCESS_TOKEN": "ms.test"},
        "loom.toolsets.zoom.auth:ZoomAuth": {"ZOOM_ACCESS_TOKEN": "zm.test"},
    }

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    async def test_the_auth_object_produces_an_authorization(
        self, toolset: str
    ) -> None:
        from loom.toolsets.factory import build_client
        from loom.toolsets.resolution import EnvironmentProvider, ToolsetSession

        spec = builtin_catalog().get(toolset).auth
        environ = self.READY_MADE.get(spec.credentials)
        if environ is None:
            pytest.skip("maps its fields directly; covered by the structural check")

        resolved = await ToolsetSession(
            providers=(EnvironmentProvider(environ=environ),)
        ).resolve(toolset, spec)
        client = build_client(spec, resolved)

        auth = getattr(getattr(client, "_session", client), "_auth", None)
        assert auth is not None, f"{toolset}: built with no auth object at all"

        headers = await auth.headers()

        assert headers.get("Authorization", "").startswith("Bearer "), (
            f"{toolset}: built, and produced no usable Authorization"
        )

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    def test_the_declared_auth_class_is_the_one_the_client_takes(
        self, toolset: str
    ) -> None:
        """The narrower statement of the same bug: a manifest naming a class the
        client's `auth=` cannot use. Checked structurally so it fails at the
        declaration rather than at somebody's first request."""
        spec = builtin_catalog().get(toolset).auth
        if not spec.credentials:
            pytest.skip("this toolset maps its fields directly")

        auth_cls = _load(spec.credentials)

        assert callable(getattr(auth_cls, "from_values", None)), (
            f"{toolset} names {spec.credentials}, which publishes no "
            "`from_values` — the one construction path the factory uses"
        )
        assert callable(getattr(auth_cls, "headers", None)), (
            f"{toolset} names {spec.credentials} as its auth object, but it has "
            "no `headers()`. That is what the client calls on every request, so "
            "a class without one builds cleanly and fails on the first call."
        )
