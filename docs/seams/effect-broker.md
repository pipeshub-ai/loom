# EffectBroker

*What every durable operation is weighed against.*

Defined in `loom/runtime/effects.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Mediates every durable operation a workflow performs.

## Contract

### `dispatch(self, call: 'EffectCall', authority: 'Authority') -> 'EffectResult'`

Carry out *call* under *authority*, or refuse it.

## Implementations

- `runtime.effects.DirectBroker`
- `runtime.effects.GuardedBroker`
- `runtime.taint.TaintBroker`

## Consumers

- `runtime.engine`
- `runtime.taint`

<!-- END GENERATED -->
