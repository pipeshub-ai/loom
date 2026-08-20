# BrowserProvider

*Who supplies a browser, and which capabilities it actually honours.*

Defined in `loom/browser/base.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Opens sessions. The thing a host swaps.

## Contract

### `open(self, policy: 'BrowserPolicy') -> 'BrowserSession'`

### `reattach(self, handle: 'SessionHandle') -> 'BrowserSession'`

Re-acquire a session that outlived this process.

### `supports(self) -> 'frozenset[str]'`

Capabilities this provider actually honours.

## Implementations

- `browser.fake.FakeBrowserProvider`
- `browser.local.LocalBrowserProvider`

## Consumers

- `browser.__init__`
- `browser.registry`
- `browser.sessions`

<!-- END GENERATED -->
