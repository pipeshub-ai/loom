# Reconciler

*Turning a provider pointer into the events it stands for.*

Defined in `loom/events/reconcile.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Expands one pointer into the events it stands for.

## Contract

### `expand(self, pointer: 'dict[str, Any]', cursor: 'str') -> 'Expansion'`

Ask the provider what changed between *cursor* and *pointer*.

## Implementations

- `toolsets.google.gmail.source.GmailReconciler`

## Consumers

- `events.__init__`

<!-- END GENERATED -->
