"""Example 14 — Workflow Manager Agent CLI.

Talk to an agent that can manage your workflows: list them, run them,
schedule them with cron, check run status, and cancel runs.

The agent has access to:
  - Workflow management tools (list, run, schedule, cancel, status)
  - Coding workflow agent tools (generate new workflows from specs)
  - Any registered toolsets (Jira, web, etc.)

Usage:
    python3 examples/cookbook/14_workflow_manager_cli.py

    # Non-interactive: run a single command
    python3 examples/cookbook/14_workflow_manager_cli.py \\
        --command "List all registered workflows"

Requires:
    ANTHROPIC_API_KEY
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log, require_env

from loom import Context, Runtime, step, workflow
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.interaction import CLIUserInteraction
from loom.agents.providers.anthropic_provider import (
    AnthropicProvider,
)
from loom.agents.tool_registry import Toolset, ToolsetRegistry
from loom.agents.workflow_tools import build_workflow_tools
from loom.runtime.dispatcher import TriggerDispatcher
from loom.stores.memory import MemoryStore
from loom.triggers.specs import Schedule

# ---------------------------------------------------------------------------
# Sample workflows to pre-register
# ---------------------------------------------------------------------------


@step
async def greet(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"


@step
async def fetch_time() -> str:
    """Get current UTC time."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@workflow(name="hello", description="A simple greeting workflow")
async def hello_workflow(ctx: Context, name: str) -> str:
    """Greet someone by name."""
    return await ctx.step(greet, name)


@workflow(
    name="heartbeat",
    description="Periodic health check",
    triggers=[Schedule("*/2 * * * *")],
)
async def heartbeat_workflow(ctx: Context, _: None = None) -> str:
    """Return a timestamped heartbeat."""
    t = await ctx.step(fetch_time)
    return f"Heartbeat: {t}"


@workflow(name="echo", description="Echo back the input")
async def echo_workflow(ctx: Context, message: str) -> str:
    """Echo the input message."""
    return f"Echo: {message}"


# ---------------------------------------------------------------------------
# Build the manager agent
# ---------------------------------------------------------------------------


class WorkflowManagerAgent:
    """Agent that manages workflows via natural language."""

    def __init__(
        self,
        runtime: Runtime,
        model: AnthropicProvider,
        dispatcher: TriggerDispatcher,
        tool_registry: ToolsetRegistry,
        executor: object | None = None,
    ) -> None:
        self._runtime = runtime
        self._model = model
        self._dispatcher = dispatcher
        self._registry = tool_registry
        # Both agents take an executor, so the same manager runs on LOOM's
        # built-in ReAct loop or on LangGraph, Agno, Pydantic AI, or a host's
        # own — the tools and the verification pipeline do not change.
        self._executor = executor
        self._coding_agent = WorkflowCodingAgent(
            model=model,
            tool_registry=tool_registry,
            executor=executor,
            user_interaction=CLIUserInteraction(),
        )

    async def chat(self, message: str) -> str:
        """Process a user message and return the agent's response."""
        from loom.agents.agent import Agent
        from loom.agents.tools import tool

        # Build tools from registry + workflow tools
        wf_tools = build_workflow_tools(self._runtime)

        @tool
        async def generate_workflow(spec: str) -> str:
            """Generate a new workflow from a natural language spec.

            Args:
                spec: Plain-English description of the workflow.
            """
            result = await self._coding_agent.generate(spec)
            if result.is_clean:
                return (
                    f"Generated workflow code "
                    f"({result.input_tokens}+{result.output_tokens} tokens):"
                    f"\n\n{result.code}"
                )
            issues = "\n".join(
                f"  [{i.severity}] {i.message}" for i in result.issues
            )
            return f"Generation had issues:\n{issues}\n\nCode:\n{result.code}"

        all_tools = [
            *[
                tool(fn)
                for fn in wf_tools
            ],
            generate_workflow,
        ]

        agent = Agent(
            executor=self._executor,
            name="workflow_manager",
            instructions=(
                "You are a workflow manager assistant. You help users:\n"
                "- List registered workflows\n"
                "- Run workflows with inputs\n"
                "- Schedule workflows with cron expressions\n"
                "- Check run status and cancel runs\n"
                "- Generate new workflows from descriptions\n\n"
                "Available workflows are pre-registered on the runtime.\n"
                "Use the tools to fulfill user requests.\n"
                "Be concise and actionable in responses."
            ),
            model=self._model,
            tools=all_tools,
        )
        result = await agent(message)
        return result.output if isinstance(result.output, str) else str(result.output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def interactive_loop(manager: WorkflowManagerAgent) -> None:
    """Run an interactive chat loop."""
    print(
        "\nType your request (or 'quit' to exit).\n"
        "Examples:\n"
        '  "List all workflows"\n'
        '  "Run hello with name Alice"\n'
        '  "Schedule heartbeat to run every 5 minutes"\n'
        '  "Show me the status of the last run"\n'
        '  "Create a workflow that fetches weather data"\n'
    )

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        log("agent", "Thinking...")
        try:
            response = await manager.chat(user_input)
            print(f"\nagent> {response}")
        except Exception as exc:
            print(f"\nagent> Error: {exc}")


async def single_command(
    manager: WorkflowManagerAgent, command: str
) -> None:
    """Run a single command and print the response."""
    log("command", command)
    response = await manager.chat(command)
    print(f"\n{response}")


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    import argparse

    parser = argparse.ArgumentParser(
        description="Workflow Manager Agent CLI"
    )
    parser.add_argument(
        "--command", "-c",
        help="Run a single command instead of interactive mode",
    )
    args = parser.parse_args()

    header("Workflow Manager Agent")

    # Setup runtime with sample workflows
    log("setup", "Creating runtime with sample workflows...")
    model = AnthropicProvider(model_name="claude-sonnet-5")
    async with Runtime(store=MemoryStore()) as rt:
        # Register sample workflows
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(hello_workflow)
        await dispatcher.register(heartbeat_workflow)
        rt.register(echo_workflow)

        log("setup", f"Registered {len(rt._workflows)} workflows")
        for name in rt._workflows:
            log("setup", f"  - {name}")

        # Build tool registry
        registry = ToolsetRegistry()
        registry.register(
            Toolset.from_callables(
                "workflow_manager",
                build_workflow_tools(rt),
                summary="Manage workflows: list, run, schedule, cancel",
            )
        )

        # Create the manager agent
        manager = WorkflowManagerAgent(
            runtime=rt,
            model=model,
            dispatcher=dispatcher,
            tool_registry=registry,
        )

        log("setup", "Manager agent ready")

        if args.command:
            await single_command(manager, args.command)
        else:
            await interactive_loop(manager)

        await dispatcher.stop()


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
