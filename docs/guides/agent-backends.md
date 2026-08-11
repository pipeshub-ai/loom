# Agent Backends

Agent backends are the pluggable execution layer for `ctx.agent()`. The workflow code never imports or references any agent framework directly -- the backend handles the turn loop, tool dispatch, and model calls.

## AgentBackend Protocol

```python
from workflow_builder.agents.backend import AgentBackend
from workflow_builder.agents.result import AgentResult

class AgentBackend(Protocol):
    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
    ) -> AgentResult[Any]: ...
```

The `tools` parameter carries LOOM `Tool` objects resolved from the `ToolsetRegistry`. Each backend converts them to its native format internally.

## Built-in Backend (LOOM Native)

The default. Uses the LOOM `BuiltInAgentRuntime` with Anthropic models.

```python
from workflow_builder import Runtime
from workflow_builder.state.memory import MemoryStore
from workflow_builder.agents.backend import BuiltInBackend
from workflow_builder.agents.models import AnthropicProvider

runtime = Runtime(
    store=MemoryStore(),
    agent_backend=BuiltInBackend(
        model=AnthropicProvider(),
        instructions="You are a helpful assistant.",
    ),
)
```

Requires: `ANTHROPIC_API_KEY` environment variable.

## LangChain Backend

Runs agents via LangChain/LangGraph. LOOM tools are converted to LangChain tools automatically.

```python
from workflow_builder.agents.backends.langchain import LangChainBackend

runtime = Runtime(
    store=MemoryStore(),
    agent_backend=LangChainBackend(
        model_name="claude-sonnet-4-20250514",
    ),
)
```

Install: `pip install workflow-builder[langchain]`

Requires: `ANTHROPIC_API_KEY` (or the relevant provider key).

## Agno Backend

Runs agents via the Agno framework.

```python
from workflow_builder.agents.backends.agno import AgnoBackend

runtime = Runtime(
    store=MemoryStore(),
    agent_backend=AgnoBackend(
        model_id="claude-sonnet-4-20250514",
    ),
)
```

Install: `pip install workflow-builder[agno]`

## PydanticAI Backend

Runs agents via Pydantic AI.

```python
from workflow_builder.agents.backends.pydantic_ai import PydanticAIBackend

runtime = Runtime(
    store=MemoryStore(),
    agent_backend=PydanticAIBackend(
        model_name="claude-sonnet-4-20250514",
    ),
)
```

Install: `pip install workflow-builder[pydantic-ai]`

## Custom Backend

Implement the `AgentBackend` protocol:

```python
from typing import Any
from workflow_builder.agents.backend import AgentBackend
from workflow_builder.agents.result import AgentResult

class MyCustomBackend:
    def __init__(self, api_key: str):
        self._api_key = api_key

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
    ) -> AgentResult[Any]:
        # 1. Convert LOOM tools to your framework's format
        native_tools = [convert_tool(t) for t in (tools or [])]

        # 2. Run your agent loop
        response = await my_agent_framework.run(
            prompt=prompt,
            tools=native_tools,
            api_key=self._api_key,
        )

        # 3. Return an AgentResult
        return AgentResult(
            output=response.text,
            tool_calls=response.tool_calls,
            usage=response.usage,
        )
```

Then pass it to the runtime:

```python
runtime = Runtime(
    store=MemoryStore(),
    agent_backend=MyCustomBackend(api_key="..."),
)
```

## Using Agents in Workflows

Once a backend is configured, use `ctx.agent()` in any workflow:

```python
from workflow_builder import Context, workflow

@workflow(name="research")
async def research(ctx: Context) -> str:
    summary = await ctx.agent(
        "Find the top 3 Python web frameworks and compare them"
    )
    return summary
```

The agent call is journaled -- on replay, the recorded result is returned without re-executing the agent.
