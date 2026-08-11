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
    python3 examples/cookbook/08_jira_agent.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider
from workflow_builder.toolsets.jira import JIRA_MANIFEST
from workflow_builder.toolsets.jira.tools import JIRA_TOOL_DOCS
from workflow_builder.toolsets.registry import register_toolset

# ---------------------------------------------------------------------------
# Natural-language queries — each becomes a generated + executed workflow
# ---------------------------------------------------------------------------

QUERIES = [
    # Query 1 — Read: list projects
    """\
Create a workflow called "list_jira_projects" that:
1. Fetches all accessible Jira projects
2. Prints them as a numbered list: "1. KEY — Name"
3. Returns the list of project dicts
Include a runnable main() with no input arguments.
""",

    # Query 2 — Read: search open high-priority bugs
    """\
Create a workflow called "open_high_priority_bugs" that:
1. Accepts a project_key string (e.g. "MYPROJECT")
2. Searches Jira for issues where: project = <project_key> AND issuetype = Bug
   AND priority in (High, Highest) AND status != Done
   ordered by created DESC, limit 10
3. Prints a table with columns: KEY | PRIORITY | STATUS | SUMMARY (truncated to 60 chars)
4. Returns the list of issue dicts
Include a runnable main() that uses the first available project's key,
fetched via jira_list_projects().
""",

    # Query 3 — Write: create a task
    """\
Create a workflow called "create_sprint_task" that:
1. Accepts a dict with keys: project_key, summary, description
2. Creates a new Jira Task (issue_type="Task", priority="Medium")
3. Prints: "Created: <KEY> — <summary>  →  <url>"
4. Returns the created issue dict {key, id, url}
Include a runnable main() that creates a task in the first available project
with summary "Workflow coding agent test task" and description
"Created automatically by the LOOM Workflow Coding Agent demo."
""",

    # Query 4 — Read + aggregate: my open issues summary
    """\
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
""",
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

DIVIDER = "=" * 65


def check_env() -> bool:
    missing = [v for v in ["ANTHROPIC_API_KEY", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
               if not os.environ.get(v)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}")
        print("Add them to your .env file and run: set -a && source .env && set +a")
        return False
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

    print(DIVIDER)
    print("  Jira Workflow Coding Agent Demo")
    print(DIVIDER)
    print(f"  Jira: {os.environ['JIRA_URL']}")
    print(f"  {len(QUERIES)} queries will be generated and executed")

    for i, query in enumerate(QUERIES, 1):
        await run_query(agent, query, i, len(QUERIES))

    print(f"\n{DIVIDER}")
    print("  All queries complete.")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(main())
