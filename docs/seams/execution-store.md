# ExecutionStore

*Where runs and journals are persisted.*

Defined in `workflow_builder/state/base.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Durable home for execution headers, journals, buffered events, and timers.

## Contract

### `create_execution(self, record: 'ExecutionRecord') -> 'None'`

### `delete_execution(self, run_id: 'str') -> 'None'`

Remove an execution and its journal. Used by retention compaction.

### `due_runs(self, now: 'datetime', *, limit: 'int' = 100) -> 'list[str]'`

Suspended runs whose wake time has passed.

### `enqueue_event(self, event: 'Event') -> 'None'`

Buffer an event, including ones that arrive before the run waits for them.

### `find_by_idempotency_key(self, key: 'str') -> 'ExecutionRecord | None'`

### `get_execution(self, run_id: 'str') -> 'ExecutionRecord | None'`

### `list_executions(self, *, workflow: 'str | None' = None, status: 'ExecutionStatus | None' = None, tags: 'list[str] | None' = None, metadata: 'dict[str, Any] | None' = None, limit: 'int' = 100, offset: 'int' = 0) -> 'list[ExecutionRecord]'`

Query execution history. Metadata filtering is what makes runs findable.

### `load_journal(self, run_id: 'str') -> 'list[JournalEntry]'`

### `runs_awaiting_event(self, name: 'str') -> 'list[str]'`

### `save_journal(self, run_id: 'str', entries: 'list[JournalEntry]') -> 'None'`

Upsert journal entries by sequence number.

### `take_event(self, run_id: 'str', name: 'str') -> 'Event | None'`

Atomically consume the oldest matching buffered event.

### `truncate_journal(self, run_id: 'str', from_path: 'str') -> 'None'`

Drop entries at or after ``from_path``; used by retry-from-failure.

### `update_execution(self, record: 'ExecutionRecord') -> 'None'`

## Implementations

- `state.memory.MemoryStore`
- `state.mongo.MongoStore`
- `state.postgres.PostgresStore`
- `state.sqlite.SQLiteStore`

## Consumers

- `runtime.backend`
- `state.__init__`

<!-- END GENERATED -->
