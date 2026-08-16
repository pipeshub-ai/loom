# ExecutionStore

*Where runs and journals are persisted.*

Defined in `loom/stores/base.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Durable home for execution headers, journals, buffered events, and timers.

## Contract

### `claim_event_delivery(self, key: 'str', *, ttl_seconds: 'float' = 604800.0) -> 'bool'`

Claim *key* for the first caller. ``False`` for every caller after.

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

- `stores.memory.MemoryStore`
- `stores.mongo.MongoStore`
- `stores.postgres.PostgresStore`
- `stores.sqlite.SQLiteStore`

## Consumers

- `runtime.backend`
- `stores.__init__`

<!-- END GENERATED -->
