# Toolsets

Toolsets group tools with lazy loading — only metadata at registration, code imported on demand.

## Creating a toolset from @step functions

<!-- docs-preamble -->

Every example on this page assumes:

```python
from workflow_builder import Runtime, step
from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry
from workflow_builder.toolsets.jira.tools import jira_create_issue, jira_search_issues

rt = Runtime()


@step
async def fetch_url(url: str) -> str:
    """Stand-in for a tool of your own."""
    return url


search_tool = fetch_url          # e.g. a LangChain tool
toolset = Toolset.from_steps("demo", [fetch_url])
```

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
rt.toolsets.register(toolset)          # `toolset` comes from the preamble

# Layer 2: auto-generated docs from manifests
docs = rt.toolsets.describe()

# Layer 3: tools resolved on demand when ctx.agent() is called
tools = rt.toolsets.resolve_tools(["demo"])
```

## Built-in toolsets

- **Jira** (`toolsets/jira/`) — 16 operations, typed Pydantic models
- **Confluence** (`toolsets/confluence/`) — 11 operations, typed Pydantic models
- **Gmail** (`toolsets/google/gmail/`) — 9 operations
- **Google Calendar** (`toolsets/google/calendar/`) — 8 operations
