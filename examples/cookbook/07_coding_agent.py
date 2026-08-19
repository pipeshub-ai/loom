"""Example 7 — Workflow Coding Agent.

You describe a workflow in plain English.  The coding agent sends your spec
to Claude, receives a complete Python file using the LOOM SDK, validates it
with the AST-based CodeValidator, and self-corrects if issues are found.
The generated file is then executed so you see it run end-to-end.

Requires:
    ANTHROPIC_API_KEY environment variable

Run:
    ANTHROPIC_API_KEY=sk-... python3 examples/cookbook/07_coding_agent.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log, require_env

from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers.anthropic_provider import AnthropicProvider

SPEC = """\
Create a workflow called "news_digest" that:
1. Accepts a list of topic keywords (e.g. ["python", "AI"])
2. For each keyword, fetches the top result from the Hacker News Algolia
   search API: https://hn.algolia.com/api/v1/search?query=<keyword>&hitsPerPage=1
3. Extracts the title and URL from each result
4. Combines everything into a formatted digest string like:
   "## News Digest\\n\\n**python**\\n- Title (URL)\\n\\n**AI**\\n- Title (URL)"
5. Prints and returns the digest

Use httpx for HTTP requests. Fetch all keywords in parallel using ctx.gather().
Include a runnable main() that uses keywords ["python", "AI", "workflow"].
"""


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    model = AnthropicProvider(
        model_name="claude-sonnet-5", api_key=os.environ["ANTHROPIC_API_KEY"]
    )
    agent = WorkflowCodingAgent(
        model=model,
        max_repair_attempts=2,
        # Tell the agent what the target environment actually has. Imports of
        # anything else are rejected at validation time rather than failing on
        # someone else's machine.
        allowed_packages={"httpx"},
    )

    header("Workflow Coding Agent")
    print(f"\nSpec:\n{SPEC}")
    log("agent", "Generating, validating, and smoke-running the workflow…")

    coding_result = await agent.generate(SPEC)

    header("RESULT")
    log("agent", f"Model   : {coding_result.model_used}")
    log("agent", f"Tokens  : {coding_result.input_tokens} in / "
                 f"{coding_result.output_tokens} out")
    log("agent", f"Repairs : {coding_result.repair_attempts}")
    log("agent", f"Clean   : {coding_result.is_clean}")

    # The agent does not just validate the code — it runs it once against a
    # MemoryStore and a mocked model before handing it over.
    if coding_result.smoke is not None:
        smoke = coding_result.smoke
        outcome = "passed" if smoke.ok else f"failed at {smoke.phase}"
        log("smoke", f"Smoke run {outcome}")
        if not smoke.ok:
            log("smoke", smoke.error[:160])

    for issue in coding_result.issues:
        log("issue", f"[{issue.severity}] {issue.category}: {issue.message}")

    header("GENERATED CODE")
    print(coding_result.code)

    if not coding_result.is_clean:
        log("agent", "Generated code has errors — not running it.")
        return

    # Run it for real. The smoke run above used a MemoryStore and a mock model;
    # this executes the file as a user would, against the live API.
    header("EXECUTING THE GENERATED WORKFLOW")
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(coding_result.code)
        tmp_path = f.name

    try:
        import subprocess

        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print("[stderr]", proc.stderr)
        log("run", f"exit {proc.returncode}")
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
