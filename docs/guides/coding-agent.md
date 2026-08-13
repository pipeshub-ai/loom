# Workflow Coding Agent

The `WorkflowCodingAgent` generates runnable Python workflows from natural language specs.

## How it works

1. **DISCOVER** — calls `search_toolsets` to find relevant integrations
2. **INSPECT** — calls `show_toolset` / `get_tool_contract` for schemas
3. **DOCS** — calls `get_tool_docs` for import paths and examples
4. **GENERATE** — writes complete Python workflow code
5. **VALIDATE** — calls `validate_code` for AST checking
6. **FIX** — if errors, corrects and re-validates
7. **SUBMIT** — returns code via structured output

## Usage

<!-- docs-preamble -->

Every example on this page assumes:

```python
from workflow_builder import Runtime
from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider
from workflow_builder.toolsets.jira.tools import jira_search_issues

rt = Runtime()
model = AnthropicProvider()
```

```python
import asyncio

from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider


async def main(rt):
    agent = WorkflowCodingAgent(
        model=AnthropicProvider(model_name="claude-sonnet-4-6"),
        tool_registry=rt.toolsets,  # auto-generates docs from manifests
    )

    result = await agent.generate(
        "Create a workflow that searches Jira for open bugs"
    )
    print(result.code)      # complete, runnable Python
    print(result.is_clean)  # True if it validates, runs, and reproduces


asyncio.run(main(rt))
```

## With ToolsetRegistry (auto-generated docs)

```python
from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry

registry = ToolsetRegistry()
registry.register(Toolset.from_steps("jira", [jira_search_issues]))

agent = WorkflowCodingAgent(model=model, tool_registry=registry)
# System prompt auto-includes tool signatures from manifests
```
