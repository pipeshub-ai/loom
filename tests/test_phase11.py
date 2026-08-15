"""Tests for Phase 11 — Testing Infrastructure & Developer Experience.

Covers: error diagnostics, project scaffolding, CI config,
cross-phase integration smoke tests.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Error Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    def test_diagnostic_dataclass(self) -> None:
        from loom.core.diagnostics import Diagnostic

        d = Diagnostic(
            code="TEST-001",
            message="test",
            fix="fix it",
        )
        assert d.code == "TEST-001"
        assert d.location == ""
        assert d.docs_url == ""

    def test_diagnostics_registry(self) -> None:
        from loom.core.diagnostics import DIAGNOSTICS

        assert "LOOM-D001" in DIAGNOSTICS
        assert "LOOM-D002" in DIAGNOSTICS
        assert "LOOM-D003" in DIAGNOSTICS
        assert "LOOM-D004" in DIAGNOSTICS
        assert "LOOM-D005" in DIAGNOSTICS
        assert "LOOM-E001" in DIAGNOSTICS
        assert "LOOM-E002" in DIAGNOSTICS
        assert "LOOM-E003" in DIAGNOSTICS
        assert "LOOM-A001" in DIAGNOSTICS

    def test_all_diagnostics_have_fix(self) -> None:
        from loom.core.diagnostics import DIAGNOSTICS

        for code, diag in DIAGNOSTICS.items():
            assert diag.fix, f"{code} missing fix suggestion"

    def test_format_error_known_code(self) -> None:
        from loom.core.diagnostics import format_error

        result = format_error("LOOM-D001")
        assert "LOOM-D001" in result
        assert "datetime.now" in result.lower() or "ctx.now" in result.lower()

    def test_format_error_unknown_code(self) -> None:
        from loom.core.diagnostics import format_error

        result = format_error("UNKNOWN-999")
        assert "Unknown" in result or "UNKNOWN-999" in result

    def test_format_error_with_context(self) -> None:
        from loom.core.diagnostics import format_error

        result = format_error(
            "LOOM-D001", location="myfile.py:42"
        )
        assert "myfile.py:42" in result

    def test_lookup_diagnostic(self) -> None:
        from loom.core.diagnostics import (
            lookup_diagnostic,
        )

        d = lookup_diagnostic("LOOM-D001")
        assert d is not None
        assert d.code == "LOOM-D001"

    def test_lookup_diagnostic_unknown(self) -> None:
        from loom.core.diagnostics import (
            lookup_diagnostic,
        )

        assert lookup_diagnostic("NONEXISTENT") is None


# ---------------------------------------------------------------------------
# Project Scaffolding
# ---------------------------------------------------------------------------


class TestScaffolding:
    def test_quickstart_workflow_template(self) -> None:
        from loom.cli.scaffold import (
            QUICKSTART_WORKFLOW,
        )

        assert "@workflow" in QUICKSTART_WORKFLOW
        assert "@step" in QUICKSTART_WORKFLOW
        assert "ctx.step" in QUICKSTART_WORKFLOW

    def test_quickstart_workflow_compiles(self) -> None:
        from loom.cli.scaffold import (
            QUICKSTART_WORKFLOW,
        )

        compile(QUICKSTART_WORKFLOW, "<quickstart>", "exec")

    def test_quickstart_test_template(self) -> None:
        from loom.cli.scaffold import QUICKSTART_TEST

        assert "pytest" in QUICKSTART_TEST
        assert "async" in QUICKSTART_TEST

    def test_quickstart_pyproject_template(self) -> None:
        from loom.cli.scaffold import (
            QUICKSTART_PYPROJECT,
        )

        assert "loomflow" in QUICKSTART_PYPROJECT
        assert "[project]" in QUICKSTART_PYPROJECT

    def test_scaffold_project_returns_paths(self) -> None:
        from loom.cli.scaffold import scaffold_project

        paths = scaffold_project("/tmp/test-project")
        assert len(paths) >= 3
        assert any("workflow" in p for p in paths)

    def test_get_template(self) -> None:
        from loom.cli.scaffold import get_template

        assert get_template("workflow") is not None
        assert get_template("test") is not None
        assert get_template("pyproject") is not None
        assert get_template("nonexistent") is None


# ---------------------------------------------------------------------------
# CI Configuration
# ---------------------------------------------------------------------------


class TestCIConfig:
    def test_ci_config_exists(self) -> None:
        ci_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "ci.yml"
        )
        assert ci_path.exists(), "CI config not found"

    def test_ci_config_has_lint_job(self) -> None:
        ci_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "ci.yml"
        )
        content = ci_path.read_text()
        assert "lint:" in content
        assert "ruff" in content

    def test_ci_config_has_test_job(self) -> None:
        ci_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "ci.yml"
        )
        content = ci_path.read_text()
        assert "test:" in content
        assert "pytest" in content


# ---------------------------------------------------------------------------
# Cross-Phase Integration Smoke Tests
# ---------------------------------------------------------------------------


class TestCrossPhaseIntegration:
    """Quick smoke tests that key modules from all phases import."""

    def test_phase1_core(self) -> None:
        from loom import (
            Context,
            Runtime,
            step,
            workflow,
        )

        assert all([Context, Runtime, step, workflow])

    def test_phase2_agents(self) -> None:
        from loom.agents.tools import Tool

        assert Tool is not None

    def test_phase3_toolsets(self) -> None:
        from loom.toolsets.manifest import (
            ToolsetManifest,
        )
        from loom.toolsets.registry import (
            register_toolset,
        )

        assert all([ToolsetManifest, register_toolset])

    def test_phase4_graph(self) -> None:
        from loom.graph.extractor import (
            ASTExtractor,
        )
        from loom.graph.wgir import WGIRGraph

        assert all([WGIRGraph, ASTExtractor])

    def test_phase5_storage(self) -> None:
        from loom.blobs.blob import BlobService

        assert BlobService is not None

    def test_phase5_flowcontrol(self) -> None:
        from loom.runtime.flowcontrol import (
            AdmissionController,
        )

        assert AdmissionController is not None

    def test_phase6_importers(self) -> None:
        from loom.importers.n8n import N8nImporter

        assert N8nImporter is not None

    def test_phase6_templates(self) -> None:
        from loom.templates.template import (
            TemplateEngine,
        )

        assert TemplateEngine is not None

    def test_phase7_capability(self) -> None:
        from loom.agents.capability import (
            detect_tier,
        )
        from loom.agents.validator import (
            CodeValidator,
        )

        assert all([detect_tier, CodeValidator])

    def test_phase9_mcp(self) -> None:
        from loom.mcp_server.bridge import (
            RuntimeBridge,
        )

        assert RuntimeBridge is not None

    def test_phase10_integrations(self) -> None:
        from loom.integrations.base import (
            AgentExecutor,
        )

        assert AgentExecutor is not None

    def test_version_is_current(self) -> None:
        import loom

        assert loom.__version__ is not None
        parts = loom.__version__.split(".")
        assert len(parts) >= 2
