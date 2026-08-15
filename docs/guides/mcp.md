# MCP Server

`loom mcp` exposes a Runtime over the [Model Context Protocol](https://modelcontextprotocol.io),
so an assistant — Claude Code, Claude Desktop, Cursor — can list your workflows,
run them, read a run's journal, and unpark a run waiting on a human.

```bash
pip install "workflow-builder[mcp]"
```

## Connect it

The server finds workflows the same way every other `loom` command does: from
`--module`, or from `[tool.loom] modules` in `pyproject.toml`.

```bash
# Claude Code
claude mcp add loom -- loom mcp --module flows.py
```

```jsonc
// Claude Desktop — claude_desktop_config.json
{
  "mcpServers": {
    "loom": {
      "command": "loom",
      "args": ["mcp", "--module", "/abs/path/to/flows.py"]
    }
  }
}
```

Point it at a running LOOM server instead of importing locally with `--server`:

```bash
loom mcp --server https://loom.internal
```

`--transport http` (or `sse`) serves networked clients rather than stdio, with
`--host` and `--port` as on `loom serve`:

```bash
loom mcp --module flows.py --transport http --host 0.0.0.0 --port 8931
```

Under stdio, stdout is the protocol channel — status goes to stderr.

## What it exposes

| Tool | Does |
|------|------|
| `list_workflows` | Every workflow, with its input schema |
| `run_workflow` | Start one and wait for it to finish or park |
| `get_run_status` | Status, input, output, error |
| `list_runs` | Recent runs, filtered by workflow or status |
| `get_run_journal` | Every durable operation, in order, with attempts and errors |
| `get_run_progress` | What a running run has narrated so far |
| `approve_run` | Resolve a pending human approval |
| `send_event` | Deliver an event to a parked run |
| `cancel_run` / `retry_run` / `replay_run` | Act on an existing run |
| `search_toolsets` / `show_toolset` | Discover registered integrations (Jira, Gmail, ...) |
| `list_artifacts` / `get_artifact_url` / `put_artifact` | Read and write named artifacts |

Resources (`loom://workflows`, `loom://workflows/{name}`, `loom://runs/{id}`,
`loom://runs/{id}/journal`) let a client read the same data without spending a
tool call, and five prompts cover authoring, debugging, and review.

## Authoring new workflows

Six more tools, on by default, let the *client's own model* write and verify
new workflow code using LOOM's toolchain — no server-side API key, no second
LLM. The client drives the loop; LOOM supplies the verification it cannot do
itself.

| Tool | Does |
|------|------|
| `get_tool_contract` | Full typed schema + import line for one toolset operation |
| `get_tool_docs` | Import paths, signatures, and examples for a toolset |
| `call_read_operation` | Run a **read-only** operation now, to resolve a name to an id before generating code that needs it |
| `validate_workflow_code` | Compile + AST checks (structure, determinism, imports) — no execution |
| `smoke_test_workflow` | Run generated code once in a subprocess sandbox, every toolset faked |
| `save_workflow` | Write the finished code to a `.py` file |

The loop the `create_workflow` prompt walks a client through:

```
search_toolsets / show_toolset / get_tool_contract / get_tool_docs   (discover)
                          │
call_read_operation                                                   (resolve names → ids)
                          │
                write the code
                          │
validate_workflow_code ──┴─→ fix ─┐
        │                          │
        ▼ (clean)                  │
smoke_test_workflow ──→ fail ──────┘
        │
        ▼ (passes)
save_workflow  →  run_workflow
```

`call_read_operation` refuses anything that is not declared `read` — a model
exploring an API during authoring cannot send mail or delete an issue by way
of research. `smoke_test_workflow` never touches a real network or
credential: every registered toolset's operations are replaced with
schema-generated fakes before the code runs, so a failure there is either a
real bug or, per the response's `environmental` flag, the sandbox having no
credentials — not something to "fix" by deleting the integration.
`save_workflow` refuses an absolute path, a `..` component, or a non-`.py`
extension.

Turn it off with `--no-authoring` or `LOOM_MCP_AUTHORING=0`, to serve only
the sixteen run-management tools:

```bash
loom mcp --module flows.py --no-authoring
```

Other env vars: `LOOM_MCP_SMOKE_TIMEOUT` (seconds before a smoke-test
subprocess is killed; default 30) and `LOOM_MCP_MAX_CODE_SIZE` (bytes of
source `validate_workflow_code`/`smoke_test_workflow` accept; default 64000).

## Design notes

**Suspended is not failure.** A parked run comes back annotated with what it is
waiting for and the exact call that unparks it:

```json
{
  "status": "suspended",
  "waiting_for": "human approval 'refund'",
  "next_action": "approve_run(run_id='run_01K…', subject='refund')",
  "note": "Suspended is not failure — the run costs nothing while parked."
}
```

**Inputs are checked before anything runs.** Workflows advertise a JSON Schema
derived from the body's annotation, and a payload whose type cannot match is
refused with the expected type and an example — rather than starting a run that
fails several steps in with an `AttributeError`.

**Retry and replay are different.** `retry_run` re-runs from the failed step
against current code; `replay_run` re-executes from the journal, repeating no
side effect. The server tells the model so.

## Extending it

Tools are plain coroutines — no `mcp` import — in
`workflow_builder/mcp_server/tools.py` (run management) and
`workflow_builder/mcp_server/authoring.py` (code generation), registered in
`server.py`. Add a capability by writing the coroutine and registering it
with a `ToolAnnotations`; it is then testable without a protocol in the
picture.

```python
from workflow_builder import Runtime
from workflow_builder.facade import LocalFacade
from workflow_builder.mcp_server import build_server, serve

runtime = Runtime()

server = build_server(LocalFacade(runtime), name="my-flows")
serve(LocalFacade(runtime), transport="stdio")
```

`RuntimeBridge` is a deprecated alias for `LocalFacade` and will be removed.
