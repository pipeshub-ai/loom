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

import asyncio
import os
import sys
import tempfile

from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider

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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    model = AnthropicProvider(model_name="claude-sonnet-4-6", api_key=api_key)
    agent = WorkflowCodingAgent(model=model, max_repair_attempts=2)

    print("=" * 60)
    print("Workflow Coding Agent")
    print("=" * 60)
    print(f"\nSpec:\n{SPEC}\n")
    print("Generating workflow code…\n")

    coding_result = await agent.generate(SPEC)

    print("=" * 60)
    print(f"Model    : {coding_result.model_used}")
    print(f"Tokens   : {coding_result.input_tokens} in / {coding_result.output_tokens} out")
    print(f"Repairs  : {coding_result.repair_attempts}")
    print(f"Clean    : {coding_result.is_clean}")
    if coding_result.issues:
        print("Issues   :")
        for issue in coding_result.issues:
            print(f"  [{issue.severity}] {issue.category}: {issue.message}")
    print("=" * 60)
    print("\n--- Generated Code ---\n")
    print(coding_result.code)
    print("\n--- End Generated Code ---\n")

    if not coding_result.is_clean:
        print("Generated code has errors — skipping execution.")
        return

    # Write to a temp file and execute it
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/tmp"
    ) as f:
        f.write(coding_result.code)
        tmp_path = f.name

    print(f"Saved to {tmp_path}")
    print("\n--- Executing generated workflow ---\n")

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

    if proc.returncode != 0:
        print(f"Workflow exited with code {proc.returncode}")
    else:
        print("--- Execution complete ---")

    os.unlink(tmp_path)


if __name__ == "__main__":
    asyncio.run(main())
