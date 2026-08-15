# QueueBackend

*Where at-least-once messages come from.*

Defined in `workflow_builder/triggers/queue.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

A broker LOOM can consume from.

Implement this over Redis Streams, SQS, RabbitMQ, Kafka, or anything else.
The consumer only needs to take messages, confirm them, and return them.

## Contract

### `ack(self, message_id: 'str') -> 'None'`

Confirm permanent handling. The message must not be redelivered.

### `nack(self, message_id: 'str', *, requeue: 'bool') -> 'None'`

Return a message. ``requeue=False`` means give up on it.

### `poll(self, max_messages: 'int') -> 'list[QueueMessage]'`

Take up to *max_messages*, making them invisible to other consumers.

## Implementations

- `triggers.queue.InMemoryQueue`

## Consumers

- *(none found)*

<!-- END GENERATED -->
