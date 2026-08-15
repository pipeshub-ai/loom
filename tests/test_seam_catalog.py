"""The seam catalog has to fail when the code moves, or it is decoration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_seam_catalog.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class TestTheGate:
    def test_the_committed_pages_are_current(self) -> None:
        """What CI runs. A stale page fails here rather than in review."""
        done = run("--check")
        assert done.returncode == 0, done.stderr

    def test_it_notices_a_stale_page(self, tmp_path: Path) -> None:
        """The property that makes the check worth having."""
        sys.path.insert(0, str(ROOT / "scripts"))
        from gen_seam_catalog import DOCS, SEAMS, collect

        name, (module, purpose) = next(iter(SEAMS.items()))
        page = DOCS / "clock.md"
        original = page.read_text(encoding="utf-8")
        try:
            page.write_text(original + "\nhand-edited\n", encoding="utf-8")
            done = run("--check")
            assert done.returncode == 1
            assert "no longer match" in done.stderr
        finally:
            page.write_text(original, encoding="utf-8")

        assert collect(name, module, purpose).name == name


class TestWhatItReports:
    @pytest.fixture(autouse=True)
    def _path(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))

    def test_it_finds_structural_implementations(self) -> None:
        """A Protocol is usually satisfied without naming it as a base.

        Listing only declared subclasses reported "none found" for exactly the
        seams whose implementations are cleanest.
        """
        from gen_seam_catalog import SEAMS, collect

        module, purpose = SEAMS["SpillStore"]
        seam = collect("SpillStore", module, purpose)

        assert any("BlobSpillStore" in name for name in seam.implementations)
        assert any("NullSpillStore" in name for name in seam.implementations)

    def test_consumers_come_from_imports_not_substrings(self) -> None:
        """A substring search reports half the tree, including docstrings."""
        from gen_seam_catalog import SEAMS, collect

        module, purpose = SEAMS["Clock"]
        seam = collect("Clock", module, purpose)

        assert "runtime.engine" in seam.consumers
        assert all("." in name for name in seam.consumers)

    def test_every_seam_has_a_contract(self) -> None:
        from gen_seam_catalog import SEAMS, collect

        for name, (module, purpose) in SEAMS.items():
            seam = collect(name, module, purpose)
            assert seam.methods, f"{name} declares no methods"
            assert seam.purpose, f"{name} has no stated purpose"
