# workflow-builder Documentation

**workflow-builder** (LOOM) is a library-first durable execution SDK for AI-powered workflows. Install with pip, write Python, and get deterministic replay, agent orchestration, and pluggable storage out of the box.

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
- [Gmail and Calendar](guides/google.md) -- Read and send mail, read and write events
- [Coding Agent](guides/coding-agent.md) -- Generate workflows from natural language
- [MCP Server](guides/mcp.md) -- Drive workflows from Claude Code, Claude Desktop, or Cursor

## Examples

See the [examples/](https://github.com/pipeshub-ai/workflow/tree/main/examples) directory:

- `examples/cookbook/` -- 14 runnable cookbook examples
- `examples/reference/` -- 10 production-pattern reference workflows
