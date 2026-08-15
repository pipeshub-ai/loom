# Agent Backends

Agent backends are the pluggable execution layer for `ctx.agent()`. The workflow code never imports or references any agent framework directly -- the backend handles the turn loop, tool dispatch, and model calls.

<!-- docs-preamble -->

Every example on this page assumes:

```python
from typing import Any, Protocol

from loom import Context, Runtime, workflow
from loom.stores.memory import MemoryStore

# Stand-ins for the framework you are wrapping.
my_agent_framework: Any = None


def convert_tool(tool: Any) -> Any:
    """Map a LOOM Tool to your framework's own tool type."""
    return tool
```

## AgentBackend Protocol

```python
from loom.agents.backend import AgentBackend
from loom.agents.result import AgentResult

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
from loom import Runtime
from loom.stores.memory import MemoryStore
from loom.agents.backend import BuiltInBackend
from loom.agents.providers.anthropic_provider import AnthropicProvider

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
from langchain_anthropic import ChatAnthropic

from loom.agents.backends.langchain import LangChainBackend

runtime = Runtime(
    store=MemoryStore(),
    # Takes a LangChain model object, not a model name.
    agent_backend=LangChainBackend(llm=ChatAnthropic(model="claude-sonnet-4-6")),
)
```

Install: `pip install loomflow[langchain]`

Requires: `ANTHROPIC_API_KEY` (or the relevant provider key).

## Agno Backend

Runs agents via the Agno framework.

```python
from agno.models.anthropic import Claude

from loom.agents.backends.agno import AgnoBackend

runtime = Runtime(
    store=MemoryStore(),
    # Takes an Agno model object.
    agent_backend=AgnoBackend(model=Claude(id="claude-sonnet-4-6")),
)
```

Install: `pip install loomflow[agno]`

## PydanticAI Backend

Runs agents via Pydantic AI.

```python
from loom.agents.backends.pydantic_ai import PydanticAIBackend

runtime = Runtime(
    store=MemoryStore(),
    # Takes a Pydantic AI model, or its string form.
    agent_backend=PydanticAIBackend(model="anthropic:claude-sonnet-4-6"),
)
```

Install: `pip install loomflow[pydantic-ai]`

## Custom Backend

Implement the `AgentBackend` protocol:

```python
from typing import Any
from loom.agents.backend import AgentBackend
from loom.agents.result import AgentResult

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

Then pass it to the runtime — `agent_backend=` is the only line that changes:

```python
from loom.agents.result import AgentResult


class MyCustomBackend:            # the class defined above
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def run(self, prompt: str, **kwargs: Any) -> AgentResult[Any]:
        ...


runtime = Runtime(
    store=MemoryStore(),
    agent_backend=MyCustomBackend(api_key="..."),
)
```

## Using Agents in Workflows

Once a backend is configured, use `ctx.agent()` in any workflow:

```python
from loom import Context, workflow

@workflow(name="research")
async def research(ctx: Context) -> str:
    summary = await ctx.agent(
        "Find the top 3 Python web frameworks and compare them"
    )
    return summary
```

The agent call is journaled -- on replay, the recorded result is returned without re-executing the agent.
