# Toolsets

Toolsets group tools with lazy loading — only metadata at registration, code imported on demand.

## Creating a toolset from @step functions

```python
from workflow_builder.agents.tool_registry import Toolset

toolset = Toolset.from_steps("jira", [jira_search_issues, jira_create_issue])
rt.toolsets.register(toolset)
```

## Creating a toolset from plain callables

```python
toolset = Toolset.from_callables("web", [search_tool, fetch_url],
                                  summary="Web search + fetch")
rt.toolsets.register(toolset)
```

## Lazy resolution

```python
# Layer 1: only manifest metadata stored (no imports)
rt.toolsets.register(toolset)

# Layer 2: auto-generated docs from manifests
docs = rt.toolsets.describe()

# Layer 3: tools resolved on demand when ctx.agent() is called
tools = rt.toolsets.resolve_tools(["jira"])
```

## Built-in toolsets

- **Jira** (`toolsets/jira/`) — 9 tools, typed Pydantic models
- **Confluence** (`toolsets/confluence/`) — 11 tools, typed Pydantic models
