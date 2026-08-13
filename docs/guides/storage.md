# Storage Backends

Storage backends persist execution records, journals, and trigger state. Switch backends by changing the `store` argument to `Runtime()` -- no workflow code changes required.

<!-- docs-preamble -->

Every example on this page assumes:

```python
import os

from workflow_builder import Runtime
from workflow_builder.runtime.journal import Journal
from workflow_builder.state.memory import MemoryStore
from workflow_builder.state.mongo import MongoStore
from workflow_builder.state.postgres import PostgresStore
from workflow_builder.state.sqlite import SQLiteStore
```

## MemoryStore

In-memory, non-durable. Use for tests and prototyping.

```python
runtime = Runtime(store=MemoryStore())
```

No dependencies. Data is lost when the process exits.

## SQLiteStore

File-based, single-process. Use for local development and CLIs.

```python
from workflow_builder.state.sqlite import SQLiteStore

runtime = Runtime(store=SQLiteStore("workflows.db"))
```

No extra dependencies (uses Python's built-in `sqlite3`). Data survives restarts. Not suitable for multi-process or distributed deployments.

## MongoStore

Production storage with MongoDB. Use for multi-process deployments.

```python
from workflow_builder.state.mongo import MongoStore

runtime = Runtime(store=MongoStore("mongodb://localhost:27017/workflows"))
```

Install: `pip install workflow-builder[mongo]`

Requires a MongoDB 5.0+ instance. Supports:
- Concurrent access from multiple workers
- TTL indexes for automatic cleanup of old runs
- Change streams for event-driven scheduling

### Configuration

```python
store = MongoStore(
    uri="mongodb://user:pass@host:27017/dbname",
    database="workflows",
)
```

Or via environment variable:

```bash
export MONGO_URI="mongodb://localhost:27017/workflows"
```

## PostgresStore

Production storage with PostgreSQL. Use for multi-process deployments where you prefer a relational database.

```python
from workflow_builder.state.postgres import PostgresStore

runtime = Runtime(store=PostgresStore("postgresql://user:pass@localhost:5432/workflows"))
```

Install: `pip install workflow-builder[postgres]`

Requires PostgreSQL 14+. Supports:
- Concurrent access with row-level locking
- JSONB storage for execution data
- Advisory locks for leader election

### Configuration

```python
store = PostgresStore(
    dsn="postgresql://user:pass@host:5432/dbname",
    pool_size=10,
)
```

Or via environment variable:

```bash
export DATABASE_URL="postgresql://localhost:5432/workflows"
```

## Switching Stores

The store is a constructor argument. To go from development to production, change one line:

```python
# Development
runtime = Runtime(store=MemoryStore())

# Local with persistence
runtime = Runtime(store=SQLiteStore("dev.db"))

# Production
runtime = Runtime(store=MongoStore(os.environ.get("MONGO_URI", "mongodb://localhost:27017")))
```

All `ExecutionStore` implementations share the same interface, so workflows, steps, and triggers work identically regardless of the backend.

## Implementing a Custom Store

Implement the `ExecutionStore` protocol from `workflow_builder.state.base`:

```python
from workflow_builder.state.base import ExecutionStore, ExecutionRecord

class MyStore(ExecutionStore):
    async def save_execution(self, record: ExecutionRecord) -> None: ...
    async def load_execution(self, run_id: str) -> ExecutionRecord | None: ...
    async def list_executions(self, **filters) -> list[ExecutionRecord]: ...
    async def delete_execution(self, run_id: str) -> None: ...
    async def save_journal(self, run_id: str, journal: Journal) -> None: ...
    async def load_journal(self, run_id: str) -> Journal | None: ...
```

See `src/workflow_builder/state/memory.py` for the simplest reference implementation.
