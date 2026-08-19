# Probe

*Looking, read-only, at the system a workflow is being written against.*

Defined in `loom/agents/probes/base.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Look at something. Change nothing.

Two methods rather than one ``explore()``. A probe that cannot handle a
target has to be able to say so without being asked to guess at it, and the
caller has to be able to pick between probes without running them — the same
reason ``EventSource`` is four small methods instead of one ``handle()``.

**Read-only is a property of the implementation, not a promise in the
docstring.** A probe is handed to a model, so "please do not write" is not a
control. ``HttpProbe`` sends GET and HEAD and has no code path that sends
anything else; ``BrowserProbe`` navigates, reads and screenshots and never
clicks or types. Build the capability so the unwanted call cannot be
expressed, rather than checking for it.

## Contract

### `observe(self, target: 'str', *, hint: 'str' = '') -> 'Observation'`

Look, and describe what is there.

### `supports(self, target: 'str') -> 'bool'`

Can this probe look at *target*? No side effects, no network.

## Implementations

- `agents.probes.browser.BrowserProbe`
- `agents.probes.http.HttpProbe`

## Consumers

- `agents.probes.__init__`
- `agents.probes.registry`

<!-- END GENERATED -->
