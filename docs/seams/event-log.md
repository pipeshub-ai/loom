# EventLog

*The durable, resumable record of what the world said.*

Defined in `loom/events/log.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

An append-only, resumable record of what the world said.

## Contract

### `append(self, topic: 'str', records: 'Sequence[EventRecord]') -> 'Sequence[Position]'`

Durably record *records*, returning each one's position, in order.

### `head(self, topic: 'str') -> 'Position | None'`

The newest position, or ``None`` for a topic with nothing in it.

### `read(self, topic: 'str', *, after: 'Position | None', limit: 'int') -> 'Sequence[StoredEvent]'`

Records this reader has not seen, given *after*.

### `retain(self, topic: 'str', policy: 'RetentionPolicy') -> 'int'`

Discard what *policy* allows. Returns how many records went.

### `wait_for(self, topic: 'str', *, after: 'Position | None', timeout: 'float') -> 'bool'`

Block until something exists after *after*, or *timeout* elapses.

## Implementations

- `events.log.StoreBackedEventLog`

## Consumers

- `events.__init__`
- `events.dispatcher`
- `events.ingress`
- `events.manager`
- `events.reconcile`
- `events.watch`

<!-- END GENERATED -->
