# StateStore

*The KV space shared by every run of a workflow.*

Defined in `loom/runtime/state.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Workflow-scoped key-value state that survives across runs.

Keyed by ``(workflow, key)``: state belongs to a workflow, not to any one
run of it.

## Contract

### `delete(self, workflow: 'str', key: 'str') -> 'None'`

### `get(self, workflow: 'str', key: 'str') -> 'Any | None'`

The stored value, or ``None``.

### `keys(self, workflow: 'str') -> 'list[str]'`

Every key this workflow holds, sorted.

### `set(self, workflow: 'str', key: 'str', value: 'Any') -> 'None'`

Store *value*, replacing whatever was there.

## Implementations

- `runtime.state.StoreBackedState`

## Consumers

- `runtime.engine`

<!-- END GENERATED -->
