"""Example 8 — Jira Workflow Coding Agent.

You write a query in plain English.  The coding agent:
  1. Receives your query as a workflow spec
  2. Generates a complete LOOM workflow that calls Jira tools
  3. Validates the code with the AST checker
  4. Executes the generated workflow against your real Jira instance

Requires env vars (add to .env):
    ANTHROPIC_API_KEY   your Anthropic key
    JIRA_URL            https://yourorg.atlassian.net
    JIRA_EMAIL          your@email.com
    JIRA_API_TOKEN      your Atlassian API token

Run:
    python3 examples/cookbook/08_jira_agent.py              # includes a write
    python3 examples/cookbook/08_jira_agent.py --read-only  # nothing is created

Query 3 creates a task in your Jira. Use ``--read-only`` to skip it when you
just want to see the agent work against a real instance.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import require_env

from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers.anthropic_provider import AnthropicProvider
from loom.toolsets.jira import JIRA_MANIFEST
from loom.toolsets.jira.tools import JIRA_TOOL_DOCS
from loom.toolsets.registry import register_toolset

# ---------------------------------------------------------------------------
# Natural-language queries — each becomes a generated + executed workflow
# ---------------------------------------------------------------------------

class Query(NamedTuple):
    """One demo query, and whether running it changes anything in Jira.

    Marked by hand rather than inferred: every spec starts "Create a workflow",
    so any keyword heuristic reads them all as writes — and guessing wrong about
    someone's real Jira is not a mistake worth risking.
    """

    writes: bool
    spec: str


QUERIES = [
    Query(writes=False, spec="""\
Create a workflow called "list_jira_projects" that:
1. Fetches all accessible Jira projects
2. Prints them as a numbered list: "1. KEY — Name"
3. Returns the list of project dicts
Include a runnable main() with no input arguments.
"""),

    Query(writes=False, spec="""\
Create a workflow called "open_high_priority_bugs" that:
1. Accepts a project_key string (e.g. "MYPROJECT")
2. Searches Jira for issues where: project = <project_key> AND issuetype = Bug
   AND priority in (High, Highest) AND status != Done
   ordered by created DESC, limit 10
3. Prints a table with columns: KEY | PRIORITY | STATUS | SUMMARY (truncated to 60 chars)
4. Returns the list of issue dicts
Include a runnable main() that uses the first available project's key,
fetched via jira_list_projects().
"""),

    # The only query that changes anything. --read-only skips it.
    Query(writes=True, spec="""\
Create a workflow called "create_sprint_task" that:
1. Accepts a dict with keys: project_key, summary, description
2. Creates a new Jira Task (issue_type="Task", priority="Medium")
3. Prints: "Created: <KEY> — <summary>  →  <url>"
4. Returns the created issue dict {key, id, url}
Include a runnable main() that creates a task in the first available project
with summary "Workflow coding agent test task" and description
"Created automatically by the LOOM Workflow Coding Agent demo."
"""),

    Query(writes=False, spec="""\
Create a workflow called "my_open_issues_summary" that:
1. Calls jira_get_myself() to find the current user's account_id and display_name
2. Searches for open issues assigned to that user:
   JQL: "assignee = currentUser() AND statusCategory != Done ORDER BY priority ASC"
   limit 25
3. Groups results by issue_type (Bug, Story, Task, etc.)
4. Prints a summary like:
      === Open Issues for <display_name> ===
      Bug    :  3
      Story  :  7
      Task   :  2
      ─────────────────
      Total  : 12
5. Returns a dict {user: display_name, total: N, by_type: {type: count}}
Include a runnable main() with no input arguments.
"""),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

DIVIDER = "=" * 65


def check_env() -> bool:
    """Exit unless the Jira and Anthropic credentials are available.

    ``require_env`` reads ``.env`` at the repo root, so keys already committed
    there work without exporting anything.
    """
    require_env("ANTHROPIC_API_KEY", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    return True


async def run_query(
    agent: WorkflowCodingAgent,
    query: str,
    query_num: int,
    total: int,
) -> None:
    print(f"\n{DIVIDER}")
    print(f"  Query {query_num}/{total}")
    print(DIVIDER)
    print(query.strip())
    print(f"\n{'─' * 65}")
    print("Generating workflow code…")

    result = await agent.generate(query)

    print(f"Model    : {result.model_used}")
    print(f"Tokens   : {result.input_tokens} in / {result.output_tokens} out")
    print(f"Repairs  : {result.repair_attempts}")
    print(f"Clean    : {result.is_clean}")

    if result.issues:
        for issue in result.issues:
            print(f"  [{issue.severity}] {issue.message}")

    print(f"\n{'─' * 65}")
    print("Generated code:\n")
    print(result.code)

    if not result.is_clean:
        print(f"\n{'─' * 65}")
        print("Skipping execution — generated code has errors.")
        return

    # Write to temp file and execute (inherits current env including JIRA_*)
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/tmp"
    ) as f:
        f.write(result.code)
        tmp_path = f.name

    print(f"{'─' * 65}")
    print(f"Executing {tmp_path}…\n")

    proc = subprocess.run(
        [sys.executable, tmp_path],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ,
    )

    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        # Filter out INFO-level workflow logs; only show real errors
        errors = [line for line in proc.stderr.splitlines()
                  if not line.startswith("INFO") and line.strip()]
        if errors:
            print("[stderr]")
            print("\n".join(errors))

    status = "✓ completed" if proc.returncode == 0 else f"✗ exit {proc.returncode}"
    print(f"\n  {status}")
    os.unlink(tmp_path)


async def main() -> None:
    if not check_env():
        sys.exit(1)

    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = AnthropicProvider(model_name="claude-sonnet-4-6", api_key=api_key)

    # Register Jira manifest so the ReAct agent can discover it
    register_toolset(JIRA_MANIFEST)

    # Pass Jira tool docs so the agent can skip discovery when pre-loaded
    agent = WorkflowCodingAgent(
        model=model,
        max_repair_attempts=2,
        tool_docs=[JIRA_TOOL_DOCS],
    )

    read_only = "--read-only" in sys.argv
    queries = [q for q in QUERIES if not q.writes] if read_only else QUERIES

    print(DIVIDER)
    print("  Jira Workflow Coding Agent Demo")
    print(DIVIDER)
    print(f"  Jira: {os.environ['JIRA_URL']}")
    print(f"  Mode: {'read-only' if read_only else 'read + write'}")
    print(f"  {len(queries)} queries will be generated and executed")
    if read_only and len(queries) < len(QUERIES):
        print(f"  ({len(QUERIES) - len(queries)} write query skipped)")

    for i, query in enumerate(queries, 1):
        await run_query(agent, query.spec, i, len(queries))

    print(f"\n{DIVIDER}")
    print("  All queries complete.")
    print(DIVIDER)


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
