"""Shared utilities for cookbook examples."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

W = 70


def ts() -> str:
    return time.strftime("%H:%M:%S")


def header(title: str) -> None:
    print(f"\n{'=' * W}")
    pad = (W - len(title) - 4) // 2
    print(f"{'=' * pad}  {title}  {'=' * (W - pad - len(title) - 4)}")
    print(f"{'=' * W}")


def log(tag: str, msg: str) -> None:
    print(f"  [{ts()}] [{tag:<14}] {msg}")


def box(content: str, title: str = "") -> None:
    lines = content.splitlines()
    if title:
        print(f"  +-- {title} {'-' * max(0, W - len(title) - 8)}+")
    else:
        print(f"  +{'-' * (W - 4)}+")
    for line in lines:
        while len(line) > W - 6:
            print(f"  | {line[:W-6]} |")
            line = "  " + line[W - 6:]
        print(f"  | {line:<{W-6}} |")
    print(f"  +{'-' * (W - 4)}+")


def load_dotenv() -> None:
    """Load ``.env`` from the repository root into the environment.

    Real environment variables win, so exporting a key for one run still
    overrides the file. Called from :func:`require_env`, so an example picks up
    credentials that are already sitting in the repo rather than telling you to
    set something you have already set.
    """
    root = Path(__file__).resolve().parents[2] / ".env"
    if not root.exists():
        return

    for raw in root.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def require_env(*names: str) -> None:
    """Exit if any env vars are missing, after consulting ``.env``."""
    load_dotenv()
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}")
        print("Set them in the environment or in .env at the repo root.")
        sys.exit(1)


def require_any_env(*alternatives: tuple[str, ...]) -> None:
    """Exit unless one whole group of env vars is present, after reading ``.env``.

    For credentials that come in alternative shapes rather than a single fixed
    set — an OAuth toolset takes a ready-made access token *or* a client id,
    secret, and refresh token, and either is complete on its own. Listing them
    as one flat requirement would demand all four.
    """
    load_dotenv()
    if any(all(os.environ.get(name) for name in group) for group in alternatives):
        return

    print("Error: missing env vars. Set one of these groups:")
    for group in alternatives:
        print(f"  - {', '.join(group)}")
    print("In the environment, or in .env at the repo root.")
    sys.exit(1)


def print_coding_result(result: Any) -> None:
    """Print generation stats from a CodingResult."""
    log("coding-agent", f"Model   : {result.model_used}")
    log("coding-agent", f"Tokens  : {result.input_tokens} in / {result.output_tokens} out")
    log("coding-agent", f"Repairs : {result.repair_attempts}")
    log("coding-agent", f"Clean   : {result.is_clean}")
    if result.issues:
        for issue in result.issues:
            log("validate", f"[{issue.severity}] {issue.message}")


def run_generated_code(code: str, timeout: int = 60) -> int:
    """Write code to a temp file, execute in subprocess, return exit code."""
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/tmp"
    ) as f:
        f.write(code)
        path = f.name

    log("runtime", f"Executing {path}")
    proc = subprocess.run(
        [sys.executable, path],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ,
    )
    if proc.stdout:
        box(proc.stdout.rstrip(), "stdout")
    errors = [
        ln for ln in (proc.stderr or "").splitlines()
        if ln.strip() and not any(
            s in ln for s in ["DeprecationWarning", "PendingDeprecation",
                              "LangGraphDeprecated", "LangChainPending"]
        )
    ]
    if errors:
        box("\n".join(errors), "stderr")
    os.unlink(path)
    return proc.returncode


def load_workflow(code: str) -> Any:
    """Load generated code as a module and return the WorkflowDefinition."""
    from workflow_builder.runtime.workflow import WorkflowDefinition

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/tmp"
    ) as f:
        f.write(code)
        path = f.name

    spec = importlib.util.spec_from_file_location("gen_wf", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.unlink(path)

    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if isinstance(attr, WorkflowDefinition):
            return attr
    return None
