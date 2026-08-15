# Workflow Coding Agent

The `WorkflowCodingAgent` generates runnable Python workflows from natural language specs.

## Code or judgement

Every node the agent generates is one of two things, decided by one question:
*can I write a rule today that is right for every input the spec allows?*

| Answer | Emits | Why |
|---|---|---|
| Yes | Plain Python + a toolset call (`@step`, `ctx.step(...)`) | Deterministic, journaled, free to re-run — this is the default, and should be almost everything |
| No, or unsure | `ctx.agent("...")` | The alternative to guessing. An invented constant is the tell that a rule shouldn't exist: a keyword list, a regex over prose, a threshold nobody supplied. `if "urgent" in subject.lower()` is a guess wearing the clothes of logic, not a rule. |

This is not a stylistic choice left to the model's discretion — it is checked.
`CodingResult.plan` reports the classification per node (`node`, `kind`,
`why`), so "this should have been a rule" or "this should have been a
judgement call" is something a reviewer can verify against the generated
code, not something they have to take on faith. An empty plan means
*unreported*, not "everything was deterministic."

**The agent behind `ctx.agent()` is pluggable, not fixed.** `BuiltInBackend`
runs LOOM's own turn loop; `LangChainBackend`, `AgnoBackend`, and
`PydanticAIBackend` wrap the equivalent framework instead. The generated
workflow code is identical either way — `ctx.agent("draft a reply")` reads the
same regardless of which backend the `Runtime` is configured with. See
[Agent Backends](agent-backends.md).

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
from loom import Runtime
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers.anthropic_provider import AnthropicProvider
from loom.toolsets.jira.tools import jira_search_issues

rt = Runtime()
model = AnthropicProvider()
```

```python
import asyncio

from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers.anthropic_provider import AnthropicProvider


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
from loom.agents.tool_registry import Toolset, ToolsetRegistry

registry = ToolsetRegistry()
registry.register(Toolset.from_steps("jira", [jira_search_issues]))

agent = WorkflowCodingAgent(model=model, tool_registry=registry)
# System prompt auto-includes tool signatures from manifests
```
