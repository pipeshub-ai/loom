"""Example 9 — Jira Workflow Coding Agent CLI.

A developer-facing CLI that shows every layer of the pipeline:
  1. Your natural-language query
  2. The exact prompt (system + user) sent to Claude
  3. Live log lines as the LLM call progresses
  4. The full generated workflow code
  5. The execution output against your real Jira instance

Usage:
    # Interactive menu — pick from preset queries
    python3 examples/cookbook/09_jira_cli.py

    # Run a specific preset
    python3 examples/cookbook/09_jira_cli.py --example 3

    # Run your own query
    python3 examples/cookbook/09_jira_cli.py --query "Show me all bugs in project PA"

Requires env vars (add to .env):
    ANTHROPIC_API_KEY, JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import ClassVar

from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider
from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry

# ---------------------------------------------------------------------------
# Preset queries
# ---------------------------------------------------------------------------

PRESETS: dict[int, tuple[str, str]] = {
    1: (
        "List open issues",
        "Create a workflow called 'list_open_issues' that:\n"
        "1. Accepts a project_key string\n"
        "2. Searches for all open issues in that project (status != Done), limit 15\n"
        "3. Prints each issue as: KEY  [TYPE/PRIORITY]  STATUS  —  Summary\n"
        "4. Returns the list of issue dicts\n"
        "In main(), fetch the project list with jira_list_projects() "
        "and use the first project key.",
    ),
    2: (
        "High-priority bugs report",
        "Create a workflow called 'high_priority_bugs_report' that:\n"
        "1. Accepts a project_key string\n"
        "2. Fetches all Bugs with priority High or Highest that are not Done, limit 20\n"
        "3. Prints a formatted report:\n"
        "     === High Priority Bugs: <project_key> ===\n"
        "     <count> open bugs\n"
        "     KEY       PRIORITY   STATUS         SUMMARY\n"
        "     ─────────────────────────────────────────────\n"
        "     PROJ-1    Highest    In Progress    Fix login...\n"
        "4. Returns {project: key, count: N, issues: [...]}\n"
        "In main(), fetch the project list and use the first project key.",
    ),
    3: (
        "Create a bug ticket",
        "Create a workflow called 'create_bug_ticket' that:\n"
        "1. Accepts a dict: {project_key, summary, description}\n"
        "2. Creates a Bug issue (issue_type='Bug', priority='High')\n"
        "3. Prints:\n"
        "     ✓ Bug created: <KEY>\n"
        "       Summary  : <summary>\n"
        "       Priority : High\n"
        "       URL      : <url>\n"
        "4. Returns {key, id, url}\n"
        "In main(), fetch projects, use the first project key, and create a bug with\n"
        "summary='Authentication fails on mobile devices' and\n"
        "description='Users on iOS 17+ cannot log in via the mobile app. '.\n"
        "Reproduce: open app → tap login → enter credentials → error 401.'",
    ),
    4: (
        "My workload summary",
        "Create a workflow called 'my_workload_summary' that:\n"
        "1. Calls jira_get_myself() to get display_name and account_id\n"
        "2. Fetches all issues assigned to currentUser() that are not Done, limit 50\n"
        "3. Groups by status (To Do / In Progress / In Review / etc.)\n"
        "4. Prints:\n"
        "     === Workload for <name> ===\n"
        "     In Progress  :  4\n"
        "     To Do        :  9\n"
        "     In Review    :  2\n"
        "     ─────────────────────\n"
        "     Total        : 15\n"
        "   Then lists each In Progress issue: KEY — Summary\n"
        "5. Returns {user, by_status: {status: count}, in_progress: [issues]}\n"
        "Include runnable main() with no input.",
    ),
}

# ---------------------------------------------------------------------------
# Pretty-print helpers (no external deps — pure ASCII)
# ---------------------------------------------------------------------------

def _build_jira_toolset() -> Toolset:
    """Build a lazy Jira toolset from the manifest.

    The manifest is metadata-only (imported cheaply).
    The resolver imports actual @step functions only when called.
    """
    from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

    def _resolver(op_id: str):
        # This import happens ONLY when a tool is actually resolved
        from workflow_builder.agents.tools import coerce_tool
        from workflow_builder.toolsets.jira import tools as jira_tools

        # Map operation IDs to @step functions
        tool_map = {
            "jira_search_issues": jira_tools.jira_search_issues,
            "jira_get_issue": jira_tools.jira_get_issue,
            "jira_create_issue": jira_tools.jira_create_issue,
            "jira_update_issue": jira_tools.jira_update_issue,
            "jira_add_comment": jira_tools.jira_add_comment,
            "jira_get_transitions": jira_tools.jira_get_transitions,
            "jira_transition_issue": jira_tools.jira_transition_issue,
            "jira_list_projects": jira_tools.jira_list_projects,
            "jira_get_myself": jira_tools.jira_get_myself,
        }
        fn = tool_map.get(op_id)
        if fn is None:
            msg = f"Unknown Jira operation: {op_id}"
            raise KeyError(msg)
        return coerce_tool(fn)

    return Toolset(manifest=JIRA_MANIFEST, _resolver=_resolver)


# ---------------------------------------------------------------------------
# Pretty-print helpers (no external deps — pure ASCII)
# ---------------------------------------------------------------------------

W = 70  # line width


def banner(text: str) -> None:
    print(f"\n{'═' * W}")
    pad = (W - len(text) - 2) // 2
    print(f"{'═' * pad}  {text}  {'═' * (W - pad - len(text) - 2)}")
    print(f"{'═' * W}")


def section(num: int, total: int, title: str) -> None:
    label = f"[{num}/{total}] {title}"
    print(f"\n{label}")
    print("─" * W)


def box(content: str, title: str = "") -> None:
    lines = content.splitlines()
    if title:
        print(f"┌─ {title} {'─' * max(0, W - len(title) - 4)}┐")
    else:
        print(f"┌{'─' * (W)}┐")
    for line in lines:
        # Wrap long lines
        while len(line) > W - 2:
            print(f"│ {line[:W-2]} │")
            line = "  " + line[W - 2:]
        print(f"│ {line:<{W-2}} │")
    print(f"└{'─' * W}┘")


def kv(key: str, value: str) -> None:
    print(f"  {key:<16} {value}")


# ---------------------------------------------------------------------------
# Logging handler — pretty-prints coding_agent log records inline
# ---------------------------------------------------------------------------

class AgentLogHandler(logging.Handler):
    """Formats workflow.coding_agent log records as inline status lines."""

    _ICONS: ClassVar[dict[str, str]] = {
        "generate": "⚙",
        "llm_request": "→",
        "llm_response": "←",
        "validation": "✔",
        "repair": "↺",
        "generate_complete": "✓",
    }

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        # Extract the event tag (first word before |)
        event = msg.split("|")[0].strip()
        icon = self._ICONS.get(event, "·")
        # Parse key=value pairs after the |
        rest = msg.split("|", 1)[-1].strip() if "|" in msg else msg
        pairs = rest.replace(",", " ").split()
        detail = "  ".join(p for p in pairs if "=" in p)
        print(f"  {icon}  {event:<22}  {detail}")


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def run_query(
    agent: WorkflowCodingAgent,
    query: str,
    label: str,
) -> None:
    steps = 5
    banner(f"Jira Workflow Coding Agent — {label}")

    # ── Step 1: query ───────────────────────────────────────────────────────
    section(1, steps, "QUERY")
    print(query)

    # ── Step 2: prompt sent to LLM ──────────────────────────────────────────
    section(2, steps, "PROMPT SENT TO CLAUDE")
    system_prompt = agent.build_system_prompt()
    print("\n[ SYSTEM PROMPT ]\n")
    box(system_prompt, "system")
    print("\n[ USER MESSAGE ]\n")
    box(query, "user")

    # ── Step 3: LLM call ────────────────────────────────────────────────────
    section(3, steps, f"GENERATING CODE  ({agent._model.model_name})")
    t0 = time.perf_counter()
    result = await agent.generate(query)
    elapsed = time.perf_counter() - t0

    print()
    kv("Model", result.model_used)
    kv("Time", f"{elapsed:.1f}s")
    kv("Tokens in", str(result.input_tokens))
    kv("Tokens out", str(result.output_tokens))
    kv("Repair rounds", str(result.repair_attempts))
    if result.issues:
        for issue in result.issues:
            kv(f"  [{issue.severity}]", f"{issue.category}: {issue.message}")
    else:
        kv("Validation", "CLEAN - no issues")

    # ── Step 4: generated code ───────────────────────────────────────────────
    section(4, steps, "GENERATED WORKFLOW CODE")
    box(result.code, "generated_workflow.py")

    if not result.is_clean:
        print("\n  X Code has errors - skipping execution.")
        return

    # ── Step 5: execution ────────────────────────────────────────────────────
    section(5, steps, "EXECUTING GENERATED WORKFLOW")

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/tmp"
    ) as f:
        f.write(result.code)
        tmp_path = f.name

    print(f"  File: {tmp_path}\n")

    t1 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, tmp_path],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ,
    )
    exec_time = time.perf_counter() - t1

    if proc.stdout:
        box(proc.stdout.rstrip(), "stdout")

    # Show only real errors from stderr (filter out retry noise)
    real_errors = [
        line for line in (proc.stderr or "").splitlines()
        if line.strip() and not any(
            skip in line for skip in ["step ", "run run_", "INFO", "DEBUG"]
        )
    ]
    if real_errors:
        box("\n".join(real_errors), "stderr")

    status_icon = "✓" if proc.returncode == 0 else "✗"
    print(f"\n  {status_icon}  exit={proc.returncode}  time={exec_time:.1f}s")

    os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Jira Workflow Coding Agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  {n}. {label}" for n, (label, _) in PRESETS.items()
        ),
    )
    p.add_argument("--query", "-q", help="Natural-language workflow query")
    p.add_argument(
        "--example", "-e",
        type=int,
        choices=list(PRESETS),
        help='Run a preset example (1-4)',
    )
    return p


def choose_interactively() -> tuple[str, str]:
    print(f"\n{'═' * W}")
    print("  Jira Workflow Coding Agent — choose a preset or enter your own query")
    print(f"{'═' * W}\n")
    for n, (label, _) in PRESETS.items():
        print(f"  {n}. {label}")
    print("  0. Enter a custom query")
    print()

    while True:
        raw = input("Your choice [0-4]: ").strip()
        if raw == "0":
            query = input("Enter your query:\n> ").strip()
            return query, "Custom query"
        try:
            n = int(raw)
            if n in PRESETS:
                label, query = PRESETS[n]
                return query, label
        except ValueError:
            pass
        print("  Please enter 0-4.")


def check_env() -> bool:
    required = ["ANTHROPIC_API_KEY", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}")
        print("Add them to .env and run: set -a && source .env && set +a")
        return False
    return True


async def main() -> None:
    if not check_env():
        sys.exit(1)

    # Configure the coding agent logger to emit pretty lines
    agent_logger = logging.getLogger("workflow.coding_agent")
    agent_logger.setLevel(logging.INFO)
    agent_logger.addHandler(AgentLogHandler())
    agent_logger.propagate = False

    # Silence everything else
    logging.getLogger().setLevel(logging.ERROR)

    args = build_parser().parse_args()

    if args.query:
        query, label = args.query, "Custom query"
    elif args.example:
        label, query = PRESETS[args.example]
    else:
        query, label = choose_interactively()

    # Lazy toolset: only the manifest (metadata) is loaded here.
    # Actual tool code is NOT imported until the agent resolves it.
    registry = ToolsetRegistry()
    registry.register(_build_jira_toolset())

    model = AnthropicProvider(
        model_name="claude-sonnet-4-6",
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    agent = WorkflowCodingAgent(
        model=model,
        max_repair_attempts=2,
        tool_registry=registry,
    )

    await run_query(agent, query, label)

    print(f"\n{'═' * W}\n")


if __name__ == "__main__":
    asyncio.run(main())
