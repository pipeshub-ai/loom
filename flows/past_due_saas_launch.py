"""Past-due unresolved tickets under the Launch SAAS epic."""

from __future__ import annotations

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.toolsets.jira.tools import jira_search_issues
from workflow_builder.toolsets.pagination import Results


@step
async def fetch_past_due_under_epic(epic_key: str) -> Results:
    """Unresolved issues linked to *epic_key* whose due date has passed.

    Uses a high max_results and checks .complete so nothing is silently dropped.
    """
    jql = (
        f'"Epic Link" = {epic_key} AND duedate < now() '
        f"AND resolution = Unresolved ORDER BY duedate ASC"
    )
    return await jira_search_issues(jql=jql, max_results=500)


@workflow(
    name="past_due_saas_launch",
    description="Fetch past-due unresolved tickets under Launch SAAS (PA-1769)",
)
async def past_due_saas_launch(ctx: Context, epic_key: str = "PA-1769") -> list:
    """Return past-due unresolved children of the Launch SAAS epic."""
    issues = await ctx.step(fetch_past_due_under_epic, epic_key)

    coverage = f"{len(issues)} past-due issue(s) under {epic_key}"
    if not issues.complete:
        coverage += f" (showing {len(issues)} of {issues.total} — results truncated)"
    await ctx.report(coverage)

    return [
        {
            "key": i.key,
            "summary": i.summary,
            "status": i.status,
            "priority": i.priority,
            "assignee": i.assignee,
        }
        for i in issues
    ]


async def main() -> None:
    rt = Runtime.from_env()
    result = await rt.run(past_due_saas_launch, "PA-1769")
    print(result.status)
    for row in result.output or []:
        print(
            f"{row['key']} | {row['status']} | {row['priority']} | "
            f"{row['assignee']} | {row['summary']}"
        )


if __name__ == "__main__":
    import asyncio as _asyncio

    _asyncio.run(main())
