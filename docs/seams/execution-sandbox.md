# ExecutionSandbox

*Where a workflow body is invoked.*

Defined in `loom/runtime/sandbox.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Runs a workflow body, and proxies its durable calls back.

## Contract

### `run(self, *, body: 'SandboxBody', run_id: 'str', input: 'Any', channel: 'ContextChannel', policy: 'SandboxPolicy') -> 'SandboxOutcome'`

Run *body*, proxying its durable calls to *channel*.

## Implementations

- `runtime.sandbox.InlineSandbox`
- `runtime.sandboxes.subprocess.SubprocessSandbox`

## Consumers

- `runtime.engine`

<!-- END GENERATED -->
