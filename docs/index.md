# loomflow Documentation

**loomflow** (LOOM) is a library-first durable execution SDK for AI-powered workflows. Install with pip, write Python, and get deterministic replay, agent orchestration, and pluggable storage out of the box.

## Why LOOM

There is no workflow builder. You describe what you want; a coding agent —
with your tools, knowledge, and skills in front of it as one live capability
catalog — writes real Python: `if`/`else`, loops, parallel branches,
sub-agents, all of it, verified before you see it. Every node is one of two
kinds: a rule the agent can state today for every input the spec allows
becomes plain code and a toolset call; anything uncertain becomes a
`ctx.agent()` node instead of a guess — backed by LOOM's own loop or any
framework you'd rather use. The code is the only source of truth: it's saved
for reuse so you generate once and run for free forever, it's durable so a
crash resumes instead of restarting, and the diagram you see is generated
from the code rather than a separate thing you keep in sync by hand.

Every other workflow tool makes you choose between a drag-and-drop canvas a
non-engineer can use and the code a real system needs. LOOM gives you the
code, and generates the picture from it — not the other way around.

**LOOM is a work in progress**, and this table is the honest version of that,
not a marketing gloss over it: some of this is shipped and tested today; some
is the direction the architecture is built toward and isn't there yet. Both
matter, so here's which is which — and the "Planned" rows are exactly where
[community contributions](https://github.com/pipeshub-ai/loom/blob/main/CONTRIBUTING.md)
help most, since each one is scoped in [`docs/design/implementation-plan.md`](design/implementation-plan.md)
with its own exit criteria.

| Capability | Status | Where |
|---|---|---|
| Coding agent generates verified Python from a sentence | **Shipped** | [Coding Agent](guides/coding-agent.md) |
| Deterministic vs. judgement classified per node, checkable on `CodingResult.plan` | **Shipped** | [Coding Agent](guides/coding-agent.md) — "Code or judgement" |
| Agent behind `ctx.agent()` is pluggable: built-in loop or LangChain/Agno/Pydantic AI | **Shipped** | [Agent Backends](guides/agent-backends.md) |
| Durable execution: journal, replay, retry vs. replay | **Shipped** | [Architecture](architecture.md) |
| Code projected to a graph (`loom check`, `graph.json`) | **Shipped** | `CLAUDE.md` — "Graph is projected from code" |
| Typed toolsets exposed to the coding agent as a live catalog | **Shipped** (manifest-based) | [Toolsets](guides/toolsets.md) |
| Toolset catalog auto-generated from an OpenAPI spec | Planned — Phase 3 | [`phases/phase-3-integrations.md`](../phases/phase-3-integrations.md) |
| Sandboxed execution — generated code runs with no ambient credentials | Planned — Phase 3 (`ExecutionBackend`) | [Implementation plan](design/implementation-plan.md) §3.1 |
| Authority via a scoped grant, checked per call | **Shipped**, in-process only | [Grants and progress](guides/grants.md) |
| Code saved and versioned, with commit/activate/rollback | Planned — Phase 5 (`SourceStore`/`VersionStore`) | [Implementation plan](design/implementation-plan.md) §4 |
| Session-shaped execution trace for debugging | Planned — Phase 5 (`TraceView`) | [Implementation plan](design/implementation-plan.md) §4 |
| Visualization: an agent renders the code as a UI, verified against the extracted graph | Planned — hybrid design | [`phases/phase-4-visualization.md`](../phases/phase-4-visualization.md) |

See [PipesHub integration](design/pipeshub-integration.md) for the fullest version of
this story — the product built on top of LOOM that this table is describing.

## Quick Links

- [Getting Started](getting-started.md) -- Install and run your first workflow
- [Architecture](architecture.md) -- How the runtime, journal, and store fit together
- [Deployment](deployment.md) -- Docker, environment variables, production storage
- [FAQ](faq.md) -- Common questions answered
- [Roadmap](roadmap.md) -- What is coming next
- [Changelog](https://github.com/pipeshub-ai/workflow/blob/main/CHANGELOG.md) -- Version history

## Guides

- [Agent Backends](guides/agent-backends.md) -- Use LangChain, Agno, PydanticAI, or your own
- [Storage Backends](guides/storage.md) -- MemoryStore, SQLite, MongoDB, PostgreSQL
- [Triggers](guides/triggers.md) -- Cron, webhook, polling, events
- [Toolsets](guides/toolsets.md) -- Create and register integration toolsets
- [Grants and progress](guides/grants.md) -- Bound what a run may do, remember state across runs, stream progress
- [Gmail and Calendar](guides/google.md) -- Read and send mail, read and write events
- [Coding Agent](guides/coding-agent.md) -- Generate workflows from natural language
- [MCP Server](guides/mcp.md) -- Drive workflows from Claude Code, Claude Desktop, or Cursor

## Design

- [Three planes](design/planes-plan.md) -- Control, authoring, and execution: the split, the storage parity gaps, and the plan
- [Implementation plan](design/implementation-plan.md) -- The ports LOOM is adding, and the boundary rule that decides what stays out
- [PipesHub integration](design/pipeshub-integration.md) -- PipesHub's workflow system, the gap analysis, and the phased plan to run it on LOOM

## Examples

See the [examples/](https://github.com/pipeshub-ai/workflow/tree/main/examples) directory:

- `examples/cookbook/` -- 19 runnable cookbook examples, including `19_pagination.py` (paged reads, bounded and unbounded)
- `examples/reference/` -- 10 production-pattern reference workflows
