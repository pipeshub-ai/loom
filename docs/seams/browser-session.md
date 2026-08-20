# BrowserSession

*A live page: navigate, read the accessibility tree, act on one control.*

Defined in `loom/browser/base.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

A live browser. Everything here is I/O; nothing here is journaled.

Journaling happens one layer up, in the ``browser.*`` nodes, so that this
port stays a thin description of a browser and a host implementing it does
not have to know what a journal is.

## Contract

### `close(self) -> 'None'`

### `extract_text(self, target: 'Target | None' = None) -> 'str'`

### `live_view_url(self) -> 'str | None'`

A URL a person can watch or take over. ``None`` when unsupported.

### `locate(self, target: 'Target') -> 'int'`

How many **visible** controls *target* matches. Tier 0, no model.

### `navigate(self, url: 'str', *, wait: 'str' = 'load') -> 'PageSnapshot'`

### `perform(self, plan: 'ActionPlan') -> 'ActResult'`

### `snapshot(self, *, vision: 'bool' = False) -> 'PageSnapshot'`

### `storage_state(self) -> 'bytes'`

## Implementations

- `browser.fake.FakeBrowserSession`
- `browser.local.LocalBrowserSession`

## Consumers

- `browser.__init__`
- `browser.sessions`
- `nodes.browser.nodes`

<!-- END GENERATED -->
