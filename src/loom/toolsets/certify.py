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
    """CERT-04: Every operation has an explicit effect classification."""
    for op in manifest.all_operations():
        if not op.effect:
            raise CertFailure(
                f"Operation '{op.id}' missing effect classification"
            )


async def check_scope_mapping(manifest: ToolsetManifest) -> None:
    """CERT-05: Write/destructive operations declare required scopes."""
    for op in manifest.all_operations():
        if op.effect.value in ("write", "destructive") and not op.scopes:
            raise CertFailure(
                f"Operation '{op.id}' ({op.effect}) has no scopes declared"
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
    """CERT-08: Fakes module is declared for testing."""
    if not manifest.fakes_module:
        raise CertFailure("Manifest missing fakes_module for testing")


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
