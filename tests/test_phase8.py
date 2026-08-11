"""Tests for Phase 8 — Reference Workflows.

Covers: compilation, structure validation, SDK pattern coverage,
spec file completeness.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_REF_DIR = _EXAMPLES / "reference"
_SPEC_DIR = _EXAMPLES / "reference_specs"

WORKFLOW_FILES = sorted(_REF_DIR.glob("wf*.py"))
SPEC_FILES = sorted(_SPEC_DIR.glob("wf*_spec.txt"))

# Ensure examples dir is importable
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))


# ---------------------------------------------------------------------------
# Compilation & import tests
# ---------------------------------------------------------------------------


class TestWorkflowCompilation:
    """Verify all 10 reference workflows compile and import."""

    def test_all_10_workflow_files_exist(self) -> None:
        assert len(WORKFLOW_FILES) == 10, (
            f"Expected 10 workflow files, found {len(WORKFLOW_FILES)}: "
            f"{[f.name for f in WORKFLOW_FILES]}"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_workflow_compiles(self, wf_file: Path) -> None:
        """Each workflow file must be valid Python."""
        source = wf_file.read_text()
        compile(source, str(wf_file), "exec")

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_workflow_has_docstring(self, wf_file: Path) -> None:
        source = wf_file.read_text()
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        assert docstring, f"{wf_file.name} missing module docstring"


# ---------------------------------------------------------------------------
# Structure validation
# ---------------------------------------------------------------------------


class TestWorkflowStructure:
    """Verify each workflow has proper SDK decorators."""

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_has_workflow_decorator(
        self, wf_file: Path
    ) -> None:
        source = wf_file.read_text()
        assert "@workflow" in source, (
            f"{wf_file.name} missing @workflow decorator"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_has_step_decorator(self, wf_file: Path) -> None:
        source = wf_file.read_text()
        assert "@step" in source, (
            f"{wf_file.name} missing @step decorator"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_imports_workflow_builder(
        self, wf_file: Path
    ) -> None:
        source = wf_file.read_text()
        assert "workflow_builder" in source, (
            f"{wf_file.name} missing workflow_builder import"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_uses_ctx_step(self, wf_file: Path) -> None:
        source = wf_file.read_text()
        assert "ctx.step(" in source, (
            f"{wf_file.name} workflow body doesn't call ctx.step"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_has_future_annotations(
        self, wf_file: Path
    ) -> None:
        source = wf_file.read_text()
        assert "from __future__ import annotations" in source


# ---------------------------------------------------------------------------
# SDK pattern coverage
# ---------------------------------------------------------------------------


class TestPatternCoverage:
    """Verify SDK patterns are covered across workflows."""

    def _all_sources(self) -> str:
        return "\n".join(f.read_text() for f in WORKFLOW_FILES)

    def test_ctx_gather_used(self) -> None:
        src = self._all_sources()
        assert "ctx.gather(" in src, "No workflow uses ctx.gather"

    def test_retry_used(self) -> None:
        src = self._all_sources()
        assert "Retry(" in src, "No workflow uses Retry"

    def test_wait_for_event_used(self) -> None:
        src = self._all_sources()
        assert "wait_for_event(" in src, (
            "No workflow uses ctx.wait_for_event"
        )

    def test_pydantic_models_used(self) -> None:
        src = self._all_sources()
        assert "BaseModel" in src, "No workflow uses Pydantic models"

    def test_httpx_used(self) -> None:
        src = self._all_sources()
        assert "httpx" in src, "No workflow uses httpx for HTTP"

    def test_gather_in_multiple_workflows(self) -> None:
        count = sum(
            1
            for f in WORKFLOW_FILES
            if "ctx.gather(" in f.read_text()
        )
        assert count >= 2, (
            f"ctx.gather used in only {count} workflows, need >= 2"
        )

    def test_retry_in_multiple_workflows(self) -> None:
        count = sum(
            1
            for f in WORKFLOW_FILES
            if "Retry(" in f.read_text()
        )
        assert count >= 2, (
            f"Retry used in only {count} workflows, need >= 2"
        )


# ---------------------------------------------------------------------------
# Spec file completeness
# ---------------------------------------------------------------------------


class TestSpecFiles:
    """Verify every workflow has a matching NL spec."""

    def test_all_10_spec_files_exist(self) -> None:
        assert len(SPEC_FILES) == 10, (
            f"Expected 10 spec files, found {len(SPEC_FILES)}"
        )

    @pytest.mark.parametrize(
        "wf_file",
        WORKFLOW_FILES,
        ids=[f.stem for f in WORKFLOW_FILES],
    )
    def test_spec_exists_for_workflow(
        self, wf_file: Path
    ) -> None:
        # wf01_lead_outreach.py → wf01_spec.txt
        prefix = wf_file.stem.split("_")[0]  # "wf01"
        spec_path = _SPEC_DIR / f"{prefix}_spec.txt"
        assert spec_path.exists(), (
            f"No spec file for {wf_file.name}: "
            f"expected {spec_path.name}"
        )

    @pytest.mark.parametrize(
        "spec_file",
        SPEC_FILES,
        ids=[f.stem for f in SPEC_FILES],
    )
    def test_spec_not_empty(self, spec_file: Path) -> None:
        content = spec_file.read_text().strip()
        assert len(content) > 50, (
            f"{spec_file.name} spec is too short"
        )


# ---------------------------------------------------------------------------
# Mock HTTP client tests
# ---------------------------------------------------------------------------


class TestMockHttp:
    """Verify the mock HTTP infrastructure works."""

    @pytest.fixture()
    def _add_mocks_to_path(self) -> None:
        mocks_dir = str(
            _EXAMPLES / "reference_tests" / "mocks"
        )
        if mocks_dir not in sys.path:
            sys.path.insert(0, mocks_dir)

    def test_mock_response(
        self, _add_mocks_to_path: None
    ) -> None:
        from mock_http import MockResponse

        r = MockResponse(status_code=200, _json={"ok": True})
        assert r.json() == {"ok": True}
        r.raise_for_status()  # should not raise

    def test_mock_response_error(
        self, _add_mocks_to_path: None
    ) -> None:
        from mock_http import MockResponse

        r = MockResponse(status_code=500)
        with pytest.raises(Exception, match="500"):
            r.raise_for_status()

    @pytest.mark.asyncio()
    async def test_mock_client_routing(
        self, _add_mocks_to_path: None
    ) -> None:
        from mock_http import MockHttpClient, MockResponse

        client = MockHttpClient()
        client.add(
            "POST",
            "openai.com",
            MockResponse(_json={"choices": [{"text": "hi"}]}),
        )
        client.add(
            "GET",
            "slack.com",
            MockResponse(_json={"ok": True}),
        )

        async with client as c:
            r1 = await c.post("https://api.openai.com/v1/chat")
            assert r1.json()["choices"][0]["text"] == "hi"

            r2 = await c.get("https://slack.com/api/test")
            assert r2.json()["ok"] is True

    @pytest.mark.asyncio()
    async def test_mock_client_default(
        self, _add_mocks_to_path: None
    ) -> None:
        from mock_http import MockHttpClient

        client = MockHttpClient()
        async with client as c:
            r = await c.get("https://unknown.example.com")
            assert r.status_code == 200

    def test_make_openai_response(
        self, _add_mocks_to_path: None
    ) -> None:
        from mock_http import make_openai_response

        r = make_openai_response("Hello!")
        content = r.json()["choices"][0]["message"]["content"]
        assert content == "Hello!"
