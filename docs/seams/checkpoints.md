# Checkpoints

*Where each subscriber has read to.*

Defined in `loom/events/log.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Where each subscriber has read to. The resume primitive.

## Contract

### `active(self, topic: 'str') -> 'Mapping[str, Checkpoint]'`

### `commit(self, subscriber: 'str', topic: 'str', position: 'Position') -> 'None'`

Record that *subscriber* has finished everything through *position*.

### `forget(self, subscriber: 'str', topic: 'str') -> 'None'`

Retire a subscriber. Explicit, because the alternative — letting an
abandoned subscriber pin retention forever — is how a log grows without
bound.

### `load(self, subscriber: 'str', topic: 'str') -> 'Position | None'`

Where *subscriber* got to, or ``None`` if it has never committed.

## Implementations

- `events.log.StoreBackedCheckpoints`

## Consumers

- `events.__init__`
- `events.dispatcher`
- `events.manager`
- `events.reconcile`

<!-- END GENERATED -->
