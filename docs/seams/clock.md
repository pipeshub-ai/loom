# Clock

*Every timestamp and every wait the engine takes.*

Defined in `loom/runtime/clock.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

The runtime's only source of "now" and "wait".

## Contract

### `now(self) -> 'datetime'`

The current time, timezone-aware and in UTC.

### `sleep(self, seconds: 'float') -> 'None'`

Wait for *seconds*, however this clock understands waiting.

## Implementations

- `nodes.base.NodeContext`
- `runtime.clock.SystemClock`
- `runtime.clock.ManualClock`
- `runtime.context.Context`

## Consumers

- `connectors.credentials`
- `connectors.oauth_client`
- `runtime.engine`
- `toolsets.connections`

<!-- END GENERATED -->
