# Deployment

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | For AI features | Anthropic API key |
| `JIRA_URL` | For Jira toolset | Jira instance URL |
| `JIRA_EMAIL` | For Jira toolset | Atlassian email |
| `JIRA_API_TOKEN` | For Jira toolset | Atlassian API token |
| `CONFLUENCE_URL` | For Confluence | Same as JIRA_URL |
| `CONFLUENCE_EMAIL` | For Confluence | Same as JIRA_EMAIL |
| `CONFLUENCE_API_TOKEN` | For Confluence | Same as JIRA_API_TOKEN |

## Docker

```bash
docker build -t workflow-builder .
docker run --env-file .env workflow-builder
```

## Docker Compose (with MongoDB)

```bash
docker-compose up -d
```

## Production storage

```python
# MongoDB
from workflow_builder.state.mongo import MongoStore
store = MongoStore("mongodb://user:pass@host:27017", database="workflows")
await store.ensure_indexes()

# PostgreSQL
from workflow_builder.state.postgres import PostgresStore
store = PostgresStore("postgresql://user:pass@host/workflows")
await store.connect()
```
