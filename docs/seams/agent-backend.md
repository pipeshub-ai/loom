# AgentBackend

*Which agent framework runs ctx.agent().*

Defined in `loom/agents/backend.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Pluggable agent execution backend.

Implementations own the turn loop, tool dispatch, and model calls.
The workflow engine only sees ``AgentResult`` at the boundary.

The ``tools`` parameter carries LOOM ``Tool`` objects resolved from
the ``ToolsetRegistry``. Each backend converts them to its native
format (one adapter function per framework).

## Contract

### `run(self, prompt: 'str', *, tools: 'list[Any] | None' = None, history: 'list[Message] | None' = None, agent_id: 'str' = '', max_turns: 'int | None' = None) -> 'AgentResult[Any]'`

Execute the agent with the given prompt and tools.

## Implementations

- `agents.backend.BuiltInBackend`
- `agents.backends.agno.AgnoBackend`
- `agents.backends.langchain.LangChainBackend`
- `agents.backends.pydantic_ai.PydanticAIBackend`

## Consumers

- *(none found)*

<!-- END GENERATED -->
