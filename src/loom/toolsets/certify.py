"""Automated toolset certification — 12-point quality checks.

``certify()`` runs all checks against a ``ToolsetManifest`` and returns
a ``CertificationResult`` indicating pass/fail for each check.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import BaseModel, Field

from loom.toolsets.manifest import ToolsetManifest


class CertFailure(Exception):  # noqa: N818
    """Raised by a certification check to indicate failure."""


class CertResult(BaseModel):
    """Result of a single certification check."""

    code: str
    description: str
    passed: bool
    reason: str = ""


class CertificationResult(BaseModel):
    """Aggregate result of all certification checks."""

    results: list[CertResult] = Field(default_factory=list)

    @property
    def certified(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


# ---------------------------------------------------------------------------
# Individual certification checks
# ---------------------------------------------------------------------------

CertCheck = Callable[[ToolsetManifest], Coroutine[Any, Any, None]]


async def check_manifest_schema(manifest: ToolsetManifest) -> None:
    """CERT-01: Manifest has required fields."""
    if not manifest.id:
        raise CertFailure("Manifest missing 'id'")
    if not manifest.version:
        raise CertFailure("Manifest missing 'version'")
    if not manifest.summary:
        raise CertFailure("Manifest missing 'summary'")


async def check_has_operations(manifest: ToolsetManifest) -> None:
    """CERT-02: Manifest has at least one operation."""
    ops = manifest.all_operations()
    if not ops:
        raise CertFailure("Manifest has no operations defined")


async def check_typed_models(manifest: ToolsetManifest) -> None:
    """CERT-03: Every operation has input or output schema."""
    for op in manifest.all_operations():
        if not op.input_schema and not op.output_schema:
            raise CertFailure(
                f"Operation '{op.id}' has neither input nor output schema"
            )


async def check_effect_classification(manifest: ToolsetManifest) -> None:
    """CERT-04: Every operation has an explicit effect classification.

    Asks whether the *author* said, not what the field currently holds.
    ``EffectClass`` is a ``StrEnum`` and every member is truthy, so the obvious
    ``if not op.effect`` can never fire — an operation named ``pages.nuke``
    with no declared effect passed this check and CERT-05 with it, because an
    unclassified operation was also exempt from the scope requirement.

    ``model_fields_set`` is the real question and survives every construction
    path a manifest arrives by, including ``model_validate_json`` for one
    loaded from a package's entry point.
    """
    for op in manifest.all_operations():
        if "effect" not in op.model_fields_set:
            raise CertFailure(
                f"Operation '{op.id}' does not declare an effect class. "
                "Declare effect=EffectClass.READ/WRITE/DESTRUCTIVE explicitly "
                "— the default is a fail-safe backstop, not a classification."
            )


#: Auth models where a scope string is a real thing the provider issues.
#:
#: The rest are not exempt out of leniency — a scope is simply not a concept
#: they have. An Exa or Tavily API key carries no scopes; Confluence and Jira
#: authenticate with an email and an API token; DuckDuckGo has no credential at
#: all. Demanding scopes there would be CERT-08's mistake again: a check that
#: fails a correct toolset for not declaring something that does not exist,
#: which teaches people to ignore the check rather than fix the toolset.
SCOPED_AUTH_MODELS = frozenset({"oauth2"})


async def check_scope_mapping(manifest: ToolsetManifest) -> None:
    """CERT-05: Write/destructive operations declare required scopes.

    Only for toolsets whose auth model has scopes — see
    :data:`SCOPED_AUTH_MODELS`. Applied to the others this check failed five
    correct toolsets for omitting a field their APIs do not define.

    Reads ``op.effect``, which defaults to ``WRITE`` — so an unclassified
    operation is held to the requirement rather than exempted from it. Under
    the old ``READ`` default this check silently skipped exactly the operations
    CERT-04 was also failing to catch.
    """
    auth_model = str((manifest.auth or {}).get("type", "")).lower()
    if auth_model not in SCOPED_AUTH_MODELS:
        return
    for op in manifest.all_operations():
        if op.effect.value in ("write", "destructive") and not op.scopes:
            raise CertFailure(
                f"Operation '{op.id}' ({op.effect}) has no scopes declared. "
                f"This toolset authenticates with {auth_model}, where a scope "
                "is what bounds what the credential may do."
            )


async def check_no_credentials(manifest: ToolsetManifest) -> None:
    """CERT-06: Auth config does not embed raw credentials."""
    auth = manifest.auth
    suspicious = {"password", "secret", "token", "api_key", "private_key"}
    for key in auth:
        if key.lower() in suspicious:
            val = auth[key]
            if isinstance(val, str) and len(val) > 8:
                raise CertFailure(
                    f"Auth config key '{key}' appears to contain "
                    "a raw credential"
                )


async def check_egress_hosts(manifest: ToolsetManifest) -> None:
    """CERT-07: Egress hosts are declared if base_url is set."""
    if manifest.base_url and not manifest.egress_hosts:
        raise CertFailure(
            "Manifest has base_url but no egress_hosts declared"
        )


async def check_fakes_declared(manifest: ToolsetManifest) -> None:
    """CERT-08: the toolset can actually be faked in the smoke sandbox.

    Checks what :func:`loom.agents.fakes.install_fakes` needs, which is *not*
    a hand-written ``fakes_module``. This check used to demand one and so
    failed all 23 shipped toolsets, none of which declares one — deliberately.
    ``agents/fakes.py`` builds a stand-in from each operation's
    ``output_schema`` precisely so nobody maintains a parallel set of fakes
    that can drift from the contract: "there is only one contract."

    What `install_fakes` really requires is a way to reach the callables it
    replaces. Without ``tools_module`` it returns an empty list and the sandbox
    silently runs against the real toolset — no credentials, a 401, and a
    repair loop whose cheapest escape is deleting the integration. Without a
    ``function`` name an individual operation is skipped the same way.

    ``fakes_module`` remains a supported override and is not required: a
    generated stub knows the shape of an answer, not its meaning.
    """
    if not manifest.tools_module:
        raise CertFailure(
            "Manifest declares no tools_module, so no operation can be faked "
            "— the smoke sandbox would run against the real service. Set "
            "tools_module (and function= on each operation), or declare a "
            "fakes_module."
        )
    unreachable = [op.id for op in manifest.all_operations() if not op.function]
    if unreachable:
        raise CertFailure(
            f"Operations name no function, so they cannot be faked or called "
            f"from generated code: {', '.join(sorted(unreachable)[:5])}"
        )


async def check_op_summaries(manifest: ToolsetManifest) -> None:
    """CERT-09: Every operation has a non-empty summary."""
    for op in manifest.all_operations():
        if not op.summary.strip():
            raise CertFailure(f"Operation '{op.id}' has empty summary")


async def check_rate_limits(manifest: ToolsetManifest) -> None:
    """CERT-10: Rate limits are declared."""
    if not manifest.rate_limits:
        raise CertFailure("Manifest missing rate_limits configuration")


async def check_unique_op_ids(manifest: ToolsetManifest) -> None:
    """CERT-11: Operation IDs are unique across all groups."""
    seen: set[str] = set()
    for op in manifest.all_operations():
        if op.id in seen:
            raise CertFailure(f"Duplicate operation id: '{op.id}'")
        seen.add(op.id)


async def check_version_format(manifest: ToolsetManifest) -> None:
    """CERT-12: Version follows semver-like format (major.minor.patch)."""
    parts = manifest.version.split(".")
    if len(parts) < 2:
        raise CertFailure(
            f"Version '{manifest.version}' is not semver-like "
            "(expected at least major.minor)"
        )
    for part in parts:
        if not part.isdigit():
            raise CertFailure(
                f"Version '{manifest.version}' has non-numeric segment '{part}'"
            )


async def check_reversibility_declarations(manifest: ToolsetManifest) -> None:
    """CERT-14: ``undone_by`` names an operation that exists, and agrees with
    ``reversible``.

    The one half of reversibility a machine can check. Whether the named
    operation genuinely restores the prior state is a judgement about the
    service — ``issues.delete`` does not undo ``issues.create`` — but an id
    that resolves to nothing is a typo, and a typo here produces an operation
    that reads as recoverable and is not.
    """
    ids = {op.id for op in manifest.all_operations()}
    for op in manifest.all_operations():
        if op.undone_by and op.undone_by not in ids:
            raise CertFailure(
                f"Operation '{op.id}' declares undone_by='{op.undone_by}', "
                f"which is not an operation in this toolset"
            )
        if op.undone_by and not op.reversible:
            raise CertFailure(
                f"Operation '{op.id}' names an inverse but is not marked "
                "reversible; policy reads the flag, so it would be treated as "
                "irreversible"
            )


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CERT_CHECKS: list[tuple[str, str, CertCheck]] = [
    ("CERT-01", "Manifest schema valid", check_manifest_schema),
    ("CERT-02", "Has operations", check_has_operations),
    ("CERT-03", "Every op has typed models", check_typed_models),
    ("CERT-04", "Effect classification present", check_effect_classification),
    ("CERT-05", "Scope mapping complete", check_scope_mapping),
    ("CERT-06", "No credential handling", check_no_credentials),
    ("CERT-07", "Egress hosts declared", check_egress_hosts),
    ("CERT-08", "Fakes present", check_fakes_declared),
    ("CERT-09", "Op summaries present", check_op_summaries),
    ("CERT-10", "Rate limits declared", check_rate_limits),
    ("CERT-11", "Unique op IDs", check_unique_op_ids),
    ("CERT-12", "Version format valid", check_version_format),
    ("CERT-14", "Reversibility declarations resolve", check_reversibility_declarations),
]


async def certify(manifest: ToolsetManifest) -> CertificationResult:
    """Run all 12 certification checks against a manifest."""
    results: list[CertResult] = []
    for code, desc, check_fn in CERT_CHECKS:
        try:
            await check_fn(manifest)
            results.append(
                CertResult(code=code, description=desc, passed=True)
            )
        except CertFailure as e:
            results.append(
                CertResult(
                    code=code,
                    description=desc,
                    passed=False,
                    reason=str(e),
                )
            )
    return CertificationResult(results=results)
