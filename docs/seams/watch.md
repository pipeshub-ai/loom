# Watch

*A provider-side subscription that expires.*

Defined in `loom/events/watch.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

A provider-side subscription that expires and must be re-established.

## Contract

### `register(self, resource: 'str') -> 'WatchRegistration'`

Establish or renew the subscription for *resource*.

### `stop(self, resource: 'str') -> 'None'`

Tear the subscription down. Called on an explicit unsubscribe, never
on shutdown — a process restarting must not deafen the mailbox.

## Implementations

- `toolsets.google.gmail.source.GmailWatcher`

## Consumers

- `events.__init__`

<!-- END GENERATED -->
