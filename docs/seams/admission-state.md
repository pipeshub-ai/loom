# AdmissionState

*Where flow-control counters live — one process, or all of them.*

Defined in `loom/runtime/admission_state.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

The counters and timestamps an admission policy decides against.

Deliberately four small methods rather than one per policy. A policy is a
rule over *numbers*; where those numbers live is a deployment decision, and
a port shaped like the rules would have to change every time a rule does.

## Contract

### `enter(self, key: 'str') -> 'None'`

Record that a run of *key* has started.

### `in_flight(self, key: 'str') -> 'int'`

How many runs of *key* are live.

### `leave(self, key: 'str') -> 'None'`

Record that a run of *key* has finished.

### `read(self, key: 'str', default: 'Any' = None) -> 'Any'`

A stored value — a timestamp, a window, a batch count.

### `write(self, key: 'str', value: 'Any') -> 'None'`

Store a value, with the implementation's expiry.

## Implementations

- `runtime.admission_state.InMemoryAdmissionState`
- `runtime.admission_state.StoreBackedAdmissionState`

## Consumers

- `runtime.flowcontrol`

<!-- END GENERATED -->
