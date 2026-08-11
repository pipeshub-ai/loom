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

## Running

```bash
# Examples 01-05 (no API key needed)
python3 examples/cookbook/01_sequential.py

# Examples 06+ (need API key)
set -a && source .env && set +a
python3 examples/cookbook/06_ai_agent_step.py
```

## Reference Workflows

The `reference/` directory contains 10 production-pattern workflows (wf01-wf10) with matching specs in `reference_specs/`.
