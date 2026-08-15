# SpillStore

*Where an oversized tool result is kept.*

Defined in `loom/agents/bounds.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Persists an oversized tool result and reads it back in pieces.

``save`` must reject on a real storage failure rather than returning a
locator that resolves to nothing — the policy treats a rejection as
best-effort and keeps the inline text, which is a better outcome than a
reference the model cannot follow.

## Contract

### `grep(self, locator: 'str', pattern: 'str', *, max_matches: 'int' = 50) -> 'list[str]'`

### `read(self, locator: 'str', *, offset: 'int' = 0, limit: 'int' = 4000) -> 'str'`

### `save(self, text: 'str', *, run_id: 'str', tool: 'str', call_id: 'str') -> 'SpillRef'`

## Implementations

- `agents.bounds.NullSpillStore`
- `agents.bounds.BlobSpillStore`

## Consumers

- `agents.spill_tools`

<!-- END GENERATED -->
