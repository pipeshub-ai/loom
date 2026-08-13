"""The cookbook is documentation that runs, so CI should treat it that way.

Examples drift silently: an API changes, nobody re-runs example 11, and the
first person to notice is a new user copying it. These tests check every
cookbook file against the SDK's own rules, and execute the ones that need no
credentials.

Examples that call a real API are checked for import-time health only — they
need keys, and running them costs money and can touch live systems.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from workflow_builder.agents.validator import CodeValidator

COOKBOOK = Path(__file__).resolve().parents[1] / "examples" / "cookbook"

#: Runnable with no credentials and no network.
OFFLINE = [
    "01_sequential.py",
    "02_parallel.py",
    "03_durable_sleep.py",
    "04_error_handling.py",
    "05_human_in_the_loop.py",
    "13_cron_trigger.py",
    "15_queue_consumer.py",
    "16_http_server.py",
    "17_files_and_artifacts.py",
]

#: Needs an API key. Exercised for structure, not executed.
NEEDS_CREDENTIALS = [
    "06_ai_agent_step.py",
    "07_coding_agent.py",
    "08_jira_agent.py",
    "09_jira_cli.py",
    "10_langchain_react_agent.py",
    "11_agno_backend.py",
    "12_pydantic_ai_backend.py",
    "14_workflow_manager_cli.py",
    "18_gmail_calendar.py",
]


def _examples() -> list[Path]:
    return sorted(
        p for p in COOKBOOK.glob("*.py") if p.name not in ("utils.py", "__init__.py")
    )


def test_every_example_is_classified() -> None:
    """A new cookbook file must be added to one list or the other.

    Otherwise it silently escapes both the run check and the structure check.
    """
    known = set(OFFLINE) | set(NEEDS_CREDENTIALS)
    actual = {p.name for p in _examples()}

    assert actual == known, f"unclassified: {sorted(actual - known)}"


@pytest.mark.parametrize("name", [p.name for p in _examples()])
def test_example_parses_and_follows_sdk_rules(name: str) -> None:
    """No syntax errors, no bare I/O in workflow bodies, no nondeterminism."""
    issues = [
        issue
        for issue in CodeValidator().validate((COOKBOOK / name).read_text())
        if issue.severity == "error"
        # CLI-only examples legitimately define no workflow of their own.
        and "No @workflow" not in issue.message
    ]
    assert issues == [], f"{name}: {[i.message for i in issues]}"


@pytest.mark.parametrize("name", [p.name for p in _examples()])
def test_example_does_not_hardcode_a_store_at_import(name: str) -> None:
    """Persistence is the host's choice — see test_store_selection.py."""
    issues = [
        i
        for i in CodeValidator().validate((COOKBOOK / name).read_text())
        if "bind its own store" in i.message
    ]
    assert issues == [], f"{name}: {[i.message for i in issues]}"


@pytest.mark.parametrize("name", NEEDS_CREDENTIALS)
def test_credentialed_example_imports_cleanly(name: str) -> None:
    """Catch API drift without spending an API call.

    Importing runs the decorators, so a renamed SDK symbol or a changed
    signature surfaces here rather than the first time someone runs the example
    with a key.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(COOKBOOK / name)!r})"],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=COOKBOOK,
        stdin=subprocess.DEVNULL,
    )
    output = done.stdout + done.stderr

    # Exiting on missing credentials is the expected outcome; a NameError,
    # ImportError, or TypeError is drift.
    for bad in ("ImportError", "NameError", "AttributeError", "TypeError"):
        assert bad not in output, f"{name} has API drift:\n{output[-1500:]}"


@pytest.mark.parametrize("name", OFFLINE)
def test_offline_example_runs(name: str) -> None:
    """These must actually work, every time, with no setup at all."""
    done = subprocess.run(
        [sys.executable, str(COOKBOOK / name)],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, (
        f"{name} exited {done.returncode}:\n{(done.stdout + done.stderr)[-2000:]}"
    )


class TestCookbookConventions:
    @pytest.mark.parametrize("name", [p.name for p in _examples()])
    def test_has_a_module_docstring(self, name: str) -> None:
        tree = ast.parse((COOKBOOK / name).read_text())
        assert ast.get_docstring(tree), f"{name} has no module docstring"

    @pytest.mark.parametrize("name", NEEDS_CREDENTIALS)
    def test_uses_the_shared_env_check(self, name: str) -> None:
        """One consistent message, and one place that reads .env."""
        source = (COOKBOOK / name).read_text()
        assert "require_env" in source or "require_any_env" in source, (
            f"{name} should use utils.require_env — or require_any_env when the "
            "credentials come in alternative shapes — so .env is honoured and "
            "the missing-credentials message matches the other examples"
        )

    def test_dotenv_is_loaded_from_the_repo_root(self, tmp_path: Path) -> None:
        """require_env reads .env, so committed keys work without exporting."""
        sys.path.insert(0, str(COOKBOOK))
        try:
            from utils import load_dotenv
        finally:
            sys.path.pop(0)

        # Resolves the repo root without raising, whether or not .env exists.
        load_dotenv()
