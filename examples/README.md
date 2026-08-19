# Cookbook Examples

| # | Example | What it demonstrates | Env vars needed |
|---|---------|---------------------|-----------------|
| 01 | `01_sequential.py` | Step chaining with Retry | None |
| 02 | `02_parallel.py` | ctx.gather() fan-out | None |
| 03 | `03_durable_sleep.py` | ctx.sleep() + scheduler resume | None |
| 04 | `04_error_handling.py` | Retry, OnError.ROUTE, Failure | None |
| 05 | `05_human_in_the_loop.py` | ctx.wait_for_event() + send_event | None |
| 06 | `06_ai_agent_step.py` | ctx.agent() with Claude | ANTHROPIC_API_KEY |
| 07 | `07_coding_agent.py` | NL spec -> generated workflow | ANTHROPIC_API_KEY |
| 08 | `08_jira_agent.py` | Jira toolset + coding agent | ANTHROPIC_API_KEY, JIRA_* |
| 09 | `09_jira_cli.py` | Interactive Jira CLI | ANTHROPIC_API_KEY, JIRA_* |
| 10 | `10_langchain_react_agent.py` | LangChain ReAct via AgentBackend | ANTHROPIC_API_KEY |
| 11 | `11_agno_backend.py` | Agno via AgentBackend | ANTHROPIC_API_KEY |
| 12 | `12_pydantic_ai_backend.py` | Pydantic AI via AgentBackend | ANTHROPIC_API_KEY |
| 13 | `13_cron_trigger.py` | Cron-scheduled workflow | None |
| 14 | `14_workflow_manager_cli.py` | Agent that manages workflows | ANTHROPIC_API_KEY |

> The table above is partial — the cookbook has grown past it. `python
> scripts/run_examples.py --list` is the authoritative listing, because it is
> generated from the directory rather than maintained beside it.

## Running

```bash
# Examples 01-05 (no API key needed)
python3 examples/cookbook/01_sequential.py

# Examples 06+ (need API key)
set -a && source .env && set +a
python3 examples/cookbook/06_ai_agent_step.py
```

## Running all of them

```bash
python3 scripts/run_examples.py              # every example, pass/skip/fail
python3 scripts/run_examples.py --list       # just enumerate
python3 scripts/run_examples.py examples/reference   # only these
```

This runs in CI as the `examples` job. A script is executed; a reference
workflow, which defines a graph and executes nothing, is imported — which is
what catches an API that has moved underneath it. An example stopping for want
of a credential or an optional extra is reported as **skipped**, not passed.

An example that needs arguments to run non-interactively says so in its own
docstring:

```
run-examples: --example 1
```

## Reference Workflows

The `reference/` directory contains 10 workflows (wf01-wf10) drawn from n8n and
Gumloop, with matching specs in `reference_specs/`. They show the shape of each
pipeline and are being rewritten onto the toolset layer — see
[the audit](../docs/design/reference-workflows-audit.md) for what is not yet
production-ready about them, and
[the plan](../docs/design/reference-workflows-plan.md) for the order it is being
fixed in.

`tests/test_phase8.py` runs all ten against seeded journals, and
`tests/test_example_conventions.py` holds the rules an example must follow, with
the current violations recorded per file.
