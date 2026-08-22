"""What a toolset needs, checked against what its client actually does.

`ToolsetManifest.auth` was a free-form `dict[str, Any]` whose shape varied per
toolset: some declared `credential`, most declared only `fields`, one a
`header`, one a `grant`. Nothing could answer *"what does this toolset need,
and does this machine have it?"* — so `loom connect jira` looked the credential
name up in the OAuth provider registry, found nothing, and refused with
*"'jira' is not a known provider"*. Jira's provider is `atlassian`.

The declarations here cannot be derived, which is why they are declarations:
`jira` -> `atlassian`, `gmail` -> `google_gmail`, and all six Graph toolsets ->
`microsoft`. A rule built from the toolset id is right for four of seventeen.

So this suite reads the **client source** and holds the manifest to it, in both
directions. `tests/test_manifest_imports.py` does the same thing for import
lines, and found six drifts on its first run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from loom.connectors.oauth_providers import get_oauth_provider, list_oauth_providers
from loom.toolsets.manifest import AuthField, AuthSpec, ToolsetManifest
from loom.toolsets.registry import builtin_catalog

TOOLSETS = Path(__file__).resolve().parent.parent / "src" / "loom" / "toolsets"

#: How a client says which `CredentialStore` key it reads. Three spellings,
#: because the six that do it were written at different times: a keyword
#: default, a module constant, and one hard-coded literal.
_CREDENTIAL_NAME = re.compile(
    r'credential_name: str = "([a-z_]+)"'
    r'|CREDENTIAL_NAME = "([a-z_]+)"'
    r'|resolve_bearer_token\("([a-z_]+)"\)'
)


def _takes_a_token_source(manifest: ToolsetManifest) -> bool:
    """Whether this toolset's client accepts an injected bearer-token source.

    The migrated shape. A client used to resolve its own key out of an ambient
    contextvar; it now takes a `TokenSource`, so the key lives in the manifest
    and reaches the client through `build_client`.
    """
    import importlib
    import inspect

    if not manifest.auth.client:
        return False
    module_name, _, symbol = manifest.auth.client.partition(":")
    try:
        cls = getattr(importlib.import_module(module_name), symbol)
    except (ImportError, AttributeError):
        return False
    return "token_source" in inspect.signature(cls.__init__).parameters


def _package_of(manifest: ToolsetManifest) -> Path:
    """The vendor package implementing this toolset.

    The **top-level** directory under `loom/toolsets/`, not the leaf that holds
    `tools.py`. The five Google toolsets share `google/auth.py` and the six
    Graph ones share `microsoft/auth.py` — which is where `credential_name`
    lives for eleven of the fifteen store-backed toolsets. Looking only at the
    leaf reports every one of them as reading no credential at all.
    """
    assert manifest.tools_module, f"{manifest.id} declares no tools_module"
    parts = manifest.tools_module.split(".")
    assert parts[:2] == ["loom", "toolsets"], manifest.tools_module
    return TOOLSETS / parts[2]


def _credential_names_in_source(package: Path) -> set[str]:
    """Every `CredentialStore` key the source under *package* actually reads.

    Read from the code rather than from a second list in this file: a list here
    would drift exactly as the manifests did, and the point is to have one
    source of truth that is the behaviour.
    """
    found: set[str] = set()
    for path in package.rglob("*.py"):
        for match in _CREDENTIAL_NAME.finditer(path.read_text()):
            found.add(next(group for group in match.groups() if group))
    return found


def _all_manifests() -> list[ToolsetManifest]:
    catalog = builtin_catalog()
    return [m for tid in catalog.toolset_ids if (m := catalog.get(tid)) is not None]


MANIFESTS = _all_manifests()
IDS = [m.id for m in MANIFESTS]


class TestEveryToolsetDeclaresItsAuth:
    def test_the_shipped_set_is_complete(self) -> None:
        assert len(MANIFESTS) > 20, "the built-in catalogue did not load"

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_it_is_typed(self, manifest: ToolsetManifest) -> None:
        assert isinstance(manifest.auth, AuthSpec)

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_a_toolset_needing_a_credential_names_its_variables(
        self, manifest: ToolsetManifest
    ) -> None:
        """`kind != "none"` means something must be supplied. Say what.

        This is what `ConnectionInspector` reads to answer "is this configured
        here?", and what an `ApiKeyFlow` collects. A toolset that needs a
        credential and names no variable can only be reported as unconfigured
        forever.
        """
        if manifest.auth.kind == "none":
            # No *credential* — which is not the same as no configuration.
            # DuckDuckGo needs no key and still reads `DDGS_PROXY`, and a value
            # a client can only get from the ambient environment is the thing
            # `AuthField.arg` exists to route, secret or not. So what a
            # credential-free toolset may not declare is a *secret*, rather
            # than a field.
            assert not [f for f in manifest.auth.fields if f.secret], (
                f"{manifest.id} declares kind='none' but names a secret"
            )
            return
        assert manifest.auth.fields, f"{manifest.id} needs auth but names no variable"
        for field in manifest.auth.fields:
            assert field.name.isupper(), f"{field.name} is not an environment variable"


class TestCredentialNamesMatchTheClients:
    """Both directions, because either alone leaves a silent failure.

    A manifest naming a key its client never reads makes `loom connect` report
    success and change nothing. A client reading a key no manifest declares is
    a credential nothing can tell you to connect.
    """

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_a_declared_credential_is_one_the_client_reads(
        self, manifest: ToolsetManifest
    ) -> None:
        if not manifest.auth.credential:
            return
        used = _credential_names_in_source(_package_of(manifest))
        if not used and _takes_a_token_source(manifest):
            # Migrated: the client no longer names its own store key. The
            # factory builds a `StoreTokenSource` from this declaration and
            # hands it in, which is what makes the client depend on what it was
            # given rather than on a process-wide contextvar. The connection
            # this test protects is still checked — one layer along, by
            # `build_client`, and by `tests/test_toolset_credentials.py`.
            return
        assert manifest.auth.credential in used, (
            f"{manifest.id} declares credential={manifest.auth.credential!r}, "
            f"but its client reads {sorted(used) or 'no CredentialStore key'}. "
            "Connecting it would store a token nothing looks up."
        )

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_a_client_that_reads_one_has_it_declared(
        self, manifest: ToolsetManifest
    ) -> None:
        used = _credential_names_in_source(_package_of(manifest))
        if not used:
            assert manifest.auth.credential == "" or _takes_a_token_source(manifest), (
                f"{manifest.id} declares a credential its client never reads, "
                "and its constructor takes no token_source either — so a "
                "connected token has no way in"
            )
            return
        assert manifest.auth.credential in used, (
            f"{manifest.id}'s client reads {sorted(used)} from a CredentialStore "
            "and the manifest declares none, so nothing can tell a user what to "
            "connect."
        )

    def test_the_store_backed_set_is_pinned(self) -> None:
        """15 toolsets over 6 credential names. Growing it is a deliberate act.

        The other 12 read environment variables and nothing else. That is not a
        gap in these manifests — it is a gap in those clients, and closing it
        means editing them rather than declaring something here that is not
        true. Six names because the five Google toolsets share one token and
        the six Graph ones share another.
        """
        declared = {m.id: m.auth.credential for m in MANIFESTS if m.auth.credential}
        assert declared == {
            "jira": "jira",
            "confluence": "confluence",
            "gmail": "google",
            "google_calendar": "google",
            "google_drive": "google",
            "google_meet": "google",
            "google_sheets": "google",
            "slack": "slack",
            "zoom": "zoom",
            "onedrive": "microsoft",
            "sharepoint": "microsoft",
            "teams": "microsoft",
            "onenote": "microsoft",
            "outlook_mail": "microsoft",
            "outlook_calendar": "microsoft",
        }


class TestProviders:
    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_a_declared_provider_exists(self, manifest: ToolsetManifest) -> None:
        if not manifest.auth.provider:
            return
        known = sorted(p.id for p in list_oauth_providers())
        assert get_oauth_provider(manifest.auth.provider) is not None, (
            f"{manifest.id} names provider {manifest.auth.provider!r}, which is "
            f"not registered. Known: {known}"
        )

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_a_provider_is_never_declared_alone(self, manifest: ToolsetManifest) -> None:
        # Enforced by AuthSpec itself; asserted here because the failure it
        # prevents — a flow that reports success and changes nothing — is the
        # one nobody would think to look for.
        if manifest.auth.provider:
            assert manifest.auth.credential

    def test_the_mapping_is_not_derivable_from_the_id(self) -> None:
        """Why this is declared rather than guessed, as a measurement."""
        mapped = [(m.id, m.auth.provider) for m in MANIFESTS if m.auth.provider]
        same = [tid for tid, provider in mapped if tid == provider]
        assert len(same) < len(mapped) / 2, (
            "if most toolset ids matched their provider, a rule would beat a "
            "declaration — they do not: " + repr(mapped)
        )

    def test_a_provider_without_a_credential_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never reads"):
            AuthSpec(kind="oauth2", provider="github")


class TestScopes:
    @pytest.mark.parametrize(
        "manifest", [m for m in MANIFESTS if m.auth.kind == "oauth2"],
        ids=[m.id for m in MANIFESTS if m.auth.kind == "oauth2"],
    )
    def test_reads_are_scoped_too(self, manifest: ToolsetManifest) -> None:
        """A token that can write and not read is a connect flow that fails later.

        `required_scopes()` is the union of the operations' own scopes, and
        CERT-05 only requires them on writes — so Jira and Confluence declared
        none on any of their 21 reads and would have been connected with a
        write-only token.
        """
        unscoped = [
            op.id
            for op in manifest.all_operations()
            if op.effect.value == "read" and not op.scopes
        ]
        assert not unscoped, f"{manifest.id} reads declare no scope: {unscoped}"

    @pytest.mark.parametrize("manifest", MANIFESTS, ids=IDS)
    def test_required_scopes_covers_the_operations(
        self, manifest: ToolsetManifest
    ) -> None:
        every = {scope for op in manifest.all_operations() for scope in op.scopes}
        assert every <= set(manifest.required_scopes())
        assert set(manifest.auth.scopes) <= set(manifest.required_scopes())


class TestTheLegacyShapeStillWorks:
    """A third-party manifest written against the dict keeps working."""

    def test_a_legacy_dict_is_promoted(self) -> None:
        manifest = ToolsetManifest(
            id="acme", version="1.0.0", summary="Acme.",
            auth={"type": "basic", "fields": ["ACME_URL", "ACME_TOKEN"],
                  "credential": "acme"},
        )
        assert manifest.auth.kind == "basic"
        assert manifest.auth.field_names == ("ACME_URL", "ACME_TOKEN")
        assert manifest.auth.credential == "acme"

    def test_an_unknown_legacy_type_is_not_silently_none(self) -> None:
        # "none" means *this API needs no credential*, which is a claim. A type
        # nobody recognises must not become it.
        auth = ToolsetManifest(
            id="acme", version="1.0.0", summary="Acme.",
            auth={"type": "mutual_tls", "fields": ["ACME_CERT"]},
        ).auth
        assert auth.kind != "none"

    def test_the_token_type_becomes_bearer(self) -> None:
        # ClickUp and GitLab used "token" for what is sent as a bearer.
        auth = ToolsetManifest(
            id="acme", version="1.0.0", summary="Acme.",
            auth={"type": "token", "fields": ["ACME_TOKEN"]},
        ).auth
        assert auth.kind == "bearer"

    def test_a_typed_construction_is_not_mangled(self) -> None:
        """The bug this class nearly shipped with.

        Legacy detection keyed on `"kind" in value`, which is also the *new*
        field's name — so every typed `AuthSpec(...)` took the promotion path,
        stringified each `AuthField` into a `name`, and defaulted `secret` back
        to true. Every non-secret field in all 27 manifests became a secret.
        """
        auth = AuthSpec(
            kind="bearer",
            fields=(AuthField(name="ACME_URL", secret=False),
                    AuthField(name="ACME_TOKEN")),
        )
        assert auth.field_names == ("ACME_URL", "ACME_TOKEN")
        assert [f.name for f in auth.secret_fields] == ["ACME_TOKEN"]

    def test_a_bare_string_field_is_accepted(self) -> None:
        assert AuthSpec(kind="bearer", fields=("ACME_TOKEN",)).field_names == (
            "ACME_TOKEN",
        )


class TestNothingIsReadThatIsNotDeclared:
    """A variable the client reads and the manifest omits cannot be asked for.

    This is a regression guard with a specific incident behind it: rewriting
    the 27 `auth` literals as `AuthSpec`s dropped `AZURE_TENANT_ID`,
    `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `MS_AUTHORITY_HOST` from all
    six Microsoft manifests. `MicrosoftCredentials.from_env` still read them,
    so a deployment configured the Azure-SDK way would have been reported as
    unconfigured by `ConnectionInspector` and asked for variables it had
    already set. `tests/test_toolsets_microsoft.py` caught it because that one
    toolset happens to have such a test; nothing covered the other 26.
    """

    #: Environment variables a toolset reads that are **not** credentials, and
    #: why. An allowlist rather than a pattern, because "is this a credential?"
    #: is a judgement about the variable and a regex over its name is the guess
    #: this module exists to replace — `..._URL` is configuration in one
    #: toolset and half of the credential in another.
    NOT_CREDENTIALS: ClassVar[dict[str, str]] = {
        "MS_GRAPH_BASE_URL": "an endpoint override for a sovereign cloud",
    }

    ENV_READ = re.compile(r'os\.environ(?:\.get)?[\(\[]\s*"([A-Z][A-Z0-9_]*)"')

    def test_every_credential_variable_a_client_reads_is_declared(self) -> None:
        declared: dict[Path, set[str]] = {}
        for manifest in MANIFESTS:
            package = _package_of(manifest)
            declared.setdefault(package, set()).update(manifest.auth.field_names)

        undeclared: dict[str, list[str]] = {}
        for package, names in declared.items():
            read: set[str] = set()
            for path in package.rglob("*.py"):
                read |= set(self.ENV_READ.findall(path.read_text()))
            missing = sorted(read - names - set(self.NOT_CREDENTIALS))
            if missing:
                undeclared[package.name] = missing

        assert not undeclared, (
            f"these toolsets read variables no manifest declares: {undeclared}. "
            "Add an AuthField, or add it to NOT_CREDENTIALS with the reason it "
            "is not one."
        )

    def test_the_allowlist_does_not_outlive_its_variables(self) -> None:
        """Guards the guard, as `test_the_shared_set_matches...` does next door.

        An entry for a variable nothing reads any more is a permission granted
        to nobody, and it hides the day that name comes back meaning something
        else.
        """
        read: set[str] = set()
        for path in TOOLSETS.rglob("*.py"):
            read |= set(self.ENV_READ.findall(path.read_text()))
        stale = sorted(set(self.NOT_CREDENTIALS) - read)
        assert not stale, f"NOT_CREDENTIALS names variables nothing reads: {stale}"
