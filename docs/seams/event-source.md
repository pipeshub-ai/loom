# EventSource

*Whether a delivery is really from the provider.*

Defined in `loom/events/sources.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

A provider LOOM can accept deliveries from.

Implemented by anyone, registered by name, discovered through the
``loom_event_source`` entry point — nothing in LOOM names a third party's
provider. See :mod:`loom.events.source_registry`.

## Contract

### `challenge(self, headers: 'Mapping[str, str]', body: 'bytes') -> 'Challenge | None'`

The handshake response, if this delivery is a handshake.

### `delivery_id(self, headers: 'Mapping[str, str]', payload: 'Any') -> 'str | None'`

The provider's own id for this delivery, or ``None`` if it has none.

### `expand(self, payload: 'Any', ctx: 'SourceContext') -> 'Sequence[InboundEvent]'`

The events this delivery represents.

### `verify(self, headers: 'Mapping[str, str]', body: 'bytes') -> 'None'`

Raise :class:`VerificationFailed` if this is not from the provider.

## Implementations

- `toolsets.google.gmail.source.GmailSource`
- `toolsets.jira.source.JiraSource`
- `toolsets.slack.source.SlackSource`

## Consumers

- `events.__init__`
- `events.ingress`
- `events.source_registry`
- `testing.conformance`

<!-- END GENERATED -->
