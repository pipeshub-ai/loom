"""GitHub step functions for use inside LOOM workflows.

    from loom.toolsets.github.tools import github_list_issues

    issues = await ctx.step(github_list_issues, repo="octocat/hello", state="open")

Retries are per operation. Reads retry; **creating an issue, a comment, or a
pull request does not**, because GitHub has no idempotency key and a retry
after a timeout files a second one that a human then triages. Updates retry
once: setting the same state twice reaches the same end state.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.github.client import GitHubClient
from loom.toolsets.github.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRepo,
    GitHubUser,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def github_whoami() -> GitHubUser:
    """Return the user this token authenticates as.

    Returns:
        The authenticated user's login, id, name, and profile URL.
    """
    from loom.toolsets.factory import client_for


    return await (await client_for("github", GitHubClient)).whoami()


@step(retry=_READ)
async def github_find_users(query: str, limit: int = 20) -> list[GitHubUser]:
    """Find people by name, login, or email.

    Resolve someone here before assigning: GitHub assignments take a ``login``,
    and a display name passed where a login belongs is rejected or silently
    dropped from the assignee list.

    Args:
        query: Name, login, or email to search for.
        limit: Maximum users. Defaults to 20.

    Returns:
        Users with login, id, name, and profile URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).find_users(query, limit=limit)


@step(retry=_READ)
async def github_list_repos(
    owner: str = "", limit: int = 50, sort: str = "updated"
) -> Results[GitHubRepo]:
    """List repositories for an owner, or for the authenticated user.

    Args:
        owner: A user or organisation login. Empty lists your own repositories.
        limit: Maximum repositories across all pages. Defaults to 50.
        sort: ``created``, ``updated``, ``pushed``, or ``full_name``.

    Returns:
        Paginated repositories. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("github", GitHubClient)
    return await client.list_repos(owner, limit=limit, sort=sort)


@step(retry=_READ)
async def github_get_repo(repo: str) -> GitHubRepo:
    """Fetch one repository.

    Args:
        repo: ``owner/repo``, e.g. ``"octocat/hello-world"``.

    Returns:
        The repository, with default branch, language, stars, and counts.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).get_repo(repo)


@step(retry=_READ)
async def github_list_issues(
    repo: str,
    state: str = "open",
    labels: str = "",
    assignee: str = "",
    since: str = "",
    limit: int = 50,
    include_pull_requests: bool = False,
) -> Results[GitHubIssue]:
    """List issues in a repository. Pull requests are excluded by default.

    GitHub's REST API considers every pull request an issue, so this endpoint
    returns both. Counting "open issues" over an unfiltered listing counts pull
    requests too — a wrong answer with nothing to notice — so they are filtered
    out here unless you ask for them.

    Args:
        repo: ``owner/repo``.
        state: ``open``, ``closed``, or ``all``. Defaults to open.
        labels: Comma-separated label names, e.g. ``"bug,ui"``.
        assignee: A login, ``none``, or ``*``.
        since: ISO-8601 instant; only issues updated after it.
        limit: Maximum issues across all pages. Defaults to 50.
        include_pull_requests: Keep pull requests in the result.

    Returns:
        Paginated issues, each carrying ``is_pull_request``.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).list_issues(
        repo,
        state=state,
        labels=labels,
        assignee=assignee,
        since=since,
        limit=limit,
        include_pull_requests=include_pull_requests,
    )


@step(retry=_READ)
async def github_get_issue(repo: str, number: int) -> GitHubIssue:
    """Fetch one issue by its number.

    Args:
        repo: ``owner/repo``.
        number: The issue number from the URL, e.g. 412. Not the id.

    Returns:
        The issue.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).get_issue(repo, number)


@step(retry=_UNSAFE_WRITE)
async def github_create_issue(
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> GitHubIssue:
    """Open an issue.

    Not retried: GitHub has no idempotency key, so a timeout after the issue
    was filed would file a second one.

    Args:
        repo: ``owner/repo``.
        title: Issue title.
        body: Issue body. Markdown.
        labels: Label names to apply.
        assignees: Logins to assign, from ``github_find_users``.

    Returns:
        The created issue, including its number and URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).create_issue(
        repo, title, body=body, labels=labels, assignees=assignees
    )


@step(retry=_IDEMPOTENT_WRITE)
async def github_update_issue(
    repo: str,
    number: int,
    title: str = "",
    body: str = "",
    state: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> GitHubIssue:
    """Update an issue — retitle, edit, close, reopen, or relabel.

    Args:
        repo: ``owner/repo``.
        number: The issue number.
        title: New title.
        body: New body.
        state: ``closed`` or ``open``.
        labels: Replace the labels with these.
        assignees: Replace the assignees with these logins.

    Returns:
        The updated issue.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).update_issue(
        repo,
        number,
        title=title,
        body=body,
        state=state,
        labels=labels,
        assignees=assignees,
    )


@step(retry=_READ)
async def github_list_comments(
    repo: str, number: int, limit: int = 50
) -> Results[GitHubComment]:
    """List the comments on an issue or pull request.

    Args:
        repo: ``owner/repo``.
        number: The issue or pull request number.
        limit: Maximum comments across all pages. Defaults to 50.

    Returns:
        Paginated comments with body, author, and timestamp.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).list_comments(repo, number, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def github_add_comment(repo: str, number: int, body: str) -> GitHubComment:
    """Comment on an issue or pull request.

    Not retried: no idempotency key, so a retry comments twice on a thread
    people are reading. GitHub also caps content creation at 80 requests a
    minute, so a bulk commenting workflow should pace itself.

    Args:
        repo: ``owner/repo``.
        number: The issue or pull request number.
        body: Comment body. Markdown.

    Returns:
        The created comment.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).add_comment(repo, number, body)


@step(retry=_READ)
async def github_list_pull_requests(
    repo: str, state: str = "open", base: str = "", limit: int = 50
) -> Results[GitHubPullRequest]:
    """List pull requests in a repository.

    Use this rather than filtering an issue listing: the pull request endpoints
    carry branches and merge state, and their numbers address pull requests
    rather than issues.

    Args:
        repo: ``owner/repo``.
        state: ``open``, ``closed``, or ``all``.
        base: Only pull requests targeting this branch.
        limit: Maximum pull requests across all pages. Defaults to 50.

    Returns:
        Paginated pull requests with head, base, draft, and merge state.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).list_pull_requests(
        repo, state=state, base=base, limit=limit
    )


@step(retry=_READ)
async def github_get_pull_request(repo: str, number: int) -> GitHubPullRequest:
    """Fetch one pull request by its number.

    Args:
        repo: ``owner/repo``.
        number: The pull request number.

    Returns:
        The pull request, with branches, draft flag, and merge state.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).get_pull_request(repo, number)


@step(retry=_UNSAFE_WRITE)
async def github_create_pull_request(
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> GitHubPullRequest:
    """Open a pull request.

    Not retried: a retry after a timeout opens a second pull request for the
    same branch, which GitHub then rejects confusingly.

    Args:
        repo: ``owner/repo``.
        title: Pull request title.
        head: Source branch, e.g. ``"feature/login"``.
        base: Target branch, e.g. ``"main"``.
        body: Description. Markdown.
        draft: Open as a draft.

    Returns:
        The created pull request.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).create_pull_request(
        repo, title, head=head, base=base, body=body, draft=draft
    )


@step(retry=_READ)
async def github_search_issues(query: str, limit: int = 30) -> Results[GitHubIssue]:
    """Search issues and pull requests across GitHub.

    Three limits worth knowing. Search returns **at most 1,000 results** for a
    query however many match, it is rate limited to **30 requests a minute**,
    and a query that times out server-side comes back flagged incomplete. All
    three are reported through ``.complete`` rather than raised, so check it
    before treating the result as exhaustive.

    Args:
        query: GitHub search syntax, e.g.
            ``"repo:octocat/hello is:open label:bug"``.
        limit: Maximum results, capped at 1,000. Defaults to 30.

    Returns:
        Paginated matching issues and pull requests.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).search_issues(query, limit=limit)


@step(retry=_READ)
async def github_search_repos(query: str, limit: int = 30) -> Results[GitHubRepo]:
    """Search repositories across GitHub.

    Same limits as ``github_search_issues``: 1,000 results, 30 requests a
    minute, and an incomplete flag on timeout.

    Args:
        query: GitHub search syntax, e.g. ``"language:python stars:>1000"``.
        limit: Maximum results, capped at 1,000. Defaults to 30.

    Returns:
        Paginated matching repositories.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("github", GitHubClient)).search_repos(query, limit=limit)
