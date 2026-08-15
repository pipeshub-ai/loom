# Contributing to workflow-builder

LOOM is a work in progress, not a finished product — the core (durable
execution, the coding agent, toolsets) is shipped and tested; larger pieces of
the design (sandboxed execution, versioned source, session traces,
agent-rendered visualization) are written down but not yet built. See the
[README's Project Status](README.md#project-status) for exactly which is
which, and [`docs/implementation-plan.md`](docs/implementation-plan.md) for
each gap scoped as its own phase with its own exit criteria.

That gap is the point of contributing here: this is meant to get to
production-ready with the community's help, and a phase in that plan is a
concrete, bounded place to start rather than an open-ended "help wanted."
Questions about which phase makes sense to pick up are welcome as a GitHub
issue before you write code.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/pipeshub-ai/workflow.git
cd workflow

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install with dev dependencies
pip install -e ".[dev]"

# Optional: install with FastAPI webhook support
pip install -e ".[dev,api]"

# Optional: install agent framework backends
pip install -e ".[dev,langchain]"
pip install -e ".[dev,agno]"
pip install -e ".[dev,pydantic-ai]"
```

## Running Tests and Checks

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_runtime.py

# Run a specific test
pytest tests/test_runtime.py::test_basic_workflow

# Run with coverage
pytest --cov=workflow_builder --cov-report=term-missing

# Lint
ruff check src tests

# Type checking
mypy
```

## Code Style

- **Formatter/linter:** ruff (enforced in CI)
- **Line length:** 100 characters
- **Python version:** 3.11+
- **Type hints:** required on all public APIs; mypy strict mode is enabled
- **Async:** all step and workflow functions must be `async def`
- **Docstrings:** Google style with `Args:` sections (used for tool schema generation)

## Adding a New Agent Backend

Implement the `AgentBackend` protocol from `workflow_builder.agents.backend`:

```python
from workflow_builder.agents.backend import AgentBackend
from workflow_builder.agents.result import AgentResult

class MyBackend:
    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
    ) -> AgentResult[Any]:
        # Convert LOOM tools to your framework's format
        # Run your agent loop
        # Return AgentResult with output and usage
        ...
```

See `src/workflow_builder/agents/backends/` for examples (LangChain, Agno, PydanticAI).

## Adding a New Storage Backend

Implement the `ExecutionStore` protocol from `workflow_builder.state.base`:

```python
from workflow_builder.state.base import ExecutionStore

class MyStore(ExecutionStore):
    async def save_execution(self, record: ExecutionRecord) -> None: ...
    async def load_execution(self, run_id: str) -> ExecutionRecord | None: ...
    async def list_executions(self, **filters) -> list[ExecutionRecord]: ...
    # ... see base.py for the full protocol
```

See `src/workflow_builder/state/` for implementations (MemoryStore, SQLiteStore, MongoStore, PostgresStore).

## Adding a New Toolset

1. Create a `ToolsetManifest` describing operations:

```python
from workflow_builder.toolsets.manifest import ToolsetManifest, OperationSpec

manifest = ToolsetManifest(
    id="my_service",
    version="1.0.0",
    summary="My Service integration",
    groups={
        "items": [
            OperationSpec(id="items.list", summary="List items"),
            OperationSpec(id="items.create", summary="Create item"),
        ]
    },
)
```

2. Register it:

```python
from workflow_builder.toolsets.registry import register_toolset
register_toolset(manifest)
```

3. Implement tool functions as `@step`-decorated async functions.

See `src/workflow_builder/toolsets/jira/` and `src/workflow_builder/toolsets/confluence/` for examples.

## Pull Request Process

1. Branch from `main`
2. Write or update tests for your changes
3. Ensure all checks pass: `pytest`, `ruff check src tests`, `mypy`
4. Write a clear PR description explaining the motivation and changes
5. Keep PRs focused -- one feature or fix per PR

## AI-Assisted Development

See [CLAUDE.md](./CLAUDE.md) for guidance on using Claude Code with this repository. It documents the architecture, key invariants, and determinism rules that AI tools should follow.
