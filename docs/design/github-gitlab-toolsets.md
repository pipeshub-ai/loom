# GitHub and GitLab toolsets — research, schema, and plan

<!-- docs-illustrative -->

**Status:** research complete, implementation pending. Every API claim was read
from vendor documentation during this work; anything unverified says so.

---

## 0. What is different about this pair

The four CRM and task toolsets before these all signalled pagination **in the
response body** — a token, a cursor, a page number, a `done` flag. GitHub and
GitLab both signal it **in response headers**, which none of the existing
dialects can read: `page_through` hands the style a parsed body, and headers are
gone by then.

That is the one structural addition this pair needs. Everything else is the
usual shape.

Two more things worth knowing before writing any code, because both produce
wrong answers rather than errors:

- **GitHub's issue listings contain pull requests.** Not a quirk — the
  documented model.
- **Three different "this might be incomplete" signals**, one per surface, and
  none of them is an error.

---

## 1. GitHub — verified notes

### 1.1 Base and auth

`https://api.github.com`, `Authorization: Bearer <token>` for both classic and
fine-grained personal access tokens. One shape, no per-org host.

### 1.2 Paging is a Link header

`page` and `per_page` (max 100, default 30). The response carries:

```
link: <…>; rel="prev", <…>; rel="next", <…>; rel="last", <…>; rel="first"
```

**The last page is the one whose `link` header has no `rel="next"`.** Some
endpoints use `before`/`after`/`since` cursors instead; those are out of scope
for the operations below, which all use page numbers.

→ **New dialect: `HeaderPaging`.** Reads the next-page signal out of headers.
The client returns `{"items": rows, "headers": {...}}` — plain data, so an
httpx response object never leaks into a paging style.

### 1.3 Every pull request is an issue

Quoting the docs: *"GitHub's REST API considers every pull request an issue,
but not every issue is a pull request."* `GET /repos/{owner}/{repo}/issues`
returns both, and they are told apart by the presence of a **`pull_request`**
key. Worse, *"the `id` of a pull request returned from 'Issues' endpoints will
be an issue id"* — so an id taken from a listing and passed to a pull-request
endpoint silently addresses the wrong thing.

→ `github_list_issues` takes `include_pull_requests: bool = False` and filters
by default. A caller asking for "the open issues" and getting a list half full
of PRs has been given a wrong answer with no error, and the count they report
is wrong too. The model carries `is_pull_request` so nothing is hidden.

### 1.4 Search: three limits, all silent

`GET /search/issues`, `/search/repositories`, `/search/users`. Response:

```json
{ "total_count": 4213, "incomplete_results": false, "items": [ … ] }
```

- **1,000 results maximum per query**, whatever `total_count` says. So
  `total_count` is *not* what a caller can retrieve, and reporting it as the
  answer overstates coverage by any margin.
- **30 requests per minute** for search (10 for code search) against 5,000/hour
  for everything else.
- **`incomplete_results: true`** means the query timed out server-side and
  *"more results might have been found, but also might not"*.

→ Search returns `Results` with `complete=False` when the cap is hit or when
`incomplete_results` is set, and the total is reported as what was retrievable.

### 1.5 Rate limits and errors

5,000 requests/hour authenticated. Rate limiting returns **403 or 429** —
both — with `x-ratelimit-remaining`, `x-ratelimit-reset`, and `retry-after` for
secondary limits. Secondary limits also cap content creation at 80 requests per
minute, which a bulk-comment workflow reaches.

Errors: `{"message": …, "documentation_url": …, "errors": [ … ]}`.

| Status | Treatment |
|---|---|
| 401 | permanent — bad token |
| 403 with `x-ratelimit-remaining: 0` | **retryable** — rate limited |
| 403 otherwise | permanent — missing scope, or blocked |
| 404 | permanent — *also* what a private repo returns to a token without access |
| 422 | permanent — validation |
| 429 | retryable |
| 5xx | retryable |

The 403 split mirrors Salesforce's: the same status means "wait" or "never"
depending on a header, and blanket-retrying spends the budget proving the
second case.

---

## 2. GitLab — verified notes

### 2.1 Base URL is per-instance

`{host}/api/v4` — `https://gitlab.com/api/v4` for the hosted service, and
anything at all for self-managed. Like Salesforce, there is no constant base
URL, so the host is configuration (`GITLAB_URL`, defaulting to gitlab.com).

### 2.2 Auth: two headers, not one

- `PRIVATE-TOKEN: <token>` for personal, project, and group access tokens
- `Authorization: Bearer <token>` for OAuth

Different header *names*, so unlike ClickUp this cannot be got subtly wrong —
it either matches or it does not.

### 2.3 Paging: offset, signalled in headers

`page` (default 1) and `per_page` (default 20, **max 100**). Response headers:

| Header | Meaning |
|---|---|
| `x-next-page` | next page index, empty on the last page |
| `x-page`, `x-per-page` | current position |
| `x-total`, `x-total-pages` | totals — **omitted past 10,000 records** |

That omission is the coverage trap: a client reading `x-total` to report a
total finds it missing exactly when the result set is large enough for the
total to matter. Absence must read as "unknown", never as zero.

Keyset pagination exists for large sets and is out of scope here; the
operations below are all page-numbered.

### 2.4 Errors

`{"message": …}` or `{"error": …}` depending on the endpoint, so both are read.

| Status | Treatment |
|---|---|
| 401 | permanent |
| 403 | permanent |
| 404 | permanent — also what a private project returns |
| 429 | retryable, honour `retry-after` |
| 5xx | retryable |

---

## 3. Schema

### 3.1 GitHub

```python
class GitHubUser(BaseModel):
    login: str; id: str; name: str; email: str; type: str; url: str

class GitHubRepo(BaseModel):
    id: str; name: str; full_name: str      # "owner/repo" — what every path takes
    description: str; private: bool; default_branch: str
    language: str; stars: int; open_issues: int; url: str; updated_at: str

class GitHubIssue(BaseModel):
    number: int          # what humans and URLs use, not `id`
    id: str; title: str; body: str; state: str
    author: str; assignees: list[str]; labels: list[str]
    comments: int; created_at: str; updated_at: str; closed_at: str; url: str
    is_pull_request: bool    # see §1.3

class GitHubPullRequest(BaseModel):
    number: int; id: str; title: str; body: str; state: str
    author: str; head: str; base: str; draft: bool; merged: bool
    mergeable_state: str; url: str; created_at: str

class GitHubComment(BaseModel):
    id: str; body: str; author: str; created_at: str; url: str
```

### 3.2 GitLab

```python
class GitLabUser(BaseModel):
    id: str; username: str; name: str; email: str; state: str; web_url: str

class GitLabProject(BaseModel):
    id: str                       # numeric, and what every path takes
    path_with_namespace: str      # "group/project" — the human handle
    name: str; description: str; visibility: str; default_branch: str
    star_count: int; open_issues_count: int; web_url: str; last_activity_at: str

class GitLabIssue(BaseModel):
    iid: int          # per-project number, what humans use
    id: str           # global id, what almost nothing takes
    project_id: str; title: str; description: str; state: str
    author: str; assignees: list[str]; labels: list[str]
    created_at: str; updated_at: str; closed_at: str; web_url: str

class GitLabMergeRequest(BaseModel):
    iid: int; id: str; project_id: str; title: str; description: str
    state: str; author: str; source_branch: str; target_branch: str
    draft: bool; merge_status: str; web_url: str

class GitLabNote(BaseModel):
    id: str; body: str; author: str; system: bool; created_at: str
```

`iid` versus `id` is GitLab's own version of the GitHub trap: the number in the
URL is the `iid`, scoped to one project, and the global `id` is a different
number that most endpoints do not accept. Both are carried, named as GitLab
names them.

---

## 4. Operations

### 4.1 GitHub (14)

| Group | Function | Effect | Notes |
|---|---|---|---|
| repos | `github_list_repos` | read | paged |
| repos | `github_get_repo` | read | |
| issues | `github_list_issues` | read | paged; filters PRs out by default |
| issues | `github_get_issue` | read | |
| issues | `github_create_issue` | write | not retried |
| issues | `github_update_issue` | write | close/reopen/retitle; retried once |
| issues | `github_list_comments` | read | paged |
| issues | `github_add_comment` | write | not retried |
| pulls | `github_list_pull_requests` | read | paged |
| pulls | `github_get_pull_request` | read | real PR ids, not issue ids |
| pulls | `github_create_pull_request` | write | not retried |
| search | `github_search_issues` | read | 1,000 cap reported |
| search | `github_search_repos` | read | same cap |
| users | `github_whoami` / `github_find_users` | read | `resolves="user"` |

### 4.2 GitLab (14)

| Group | Function | Effect | Notes |
|---|---|---|---|
| projects | `gitlab_list_projects` | read | paged; `resolves="project"` |
| projects | `gitlab_get_project` | read | accepts id or `group/project` |
| issues | `gitlab_list_issues` | read | paged |
| issues | `gitlab_get_issue` | read | by `iid` |
| issues | `gitlab_create_issue` | write | not retried |
| issues | `gitlab_update_issue` | write | retried once |
| issues | `gitlab_close_issue` | write | the common one, named |
| notes | `gitlab_list_issue_notes` | read | paged |
| notes | `gitlab_add_issue_note` | write | not retried |
| merge | `gitlab_list_merge_requests` | read | paged |
| merge | `gitlab_get_merge_request` | read | |
| merge | `gitlab_create_merge_request` | write | not retried |
| users | `gitlab_find_users` | read | `resolves="user"` |
| users | `gitlab_whoami` | read | |

Creates are not retried on either side: neither API has an idempotency key, and
a retry after a timeout files a second issue that a human then triages.

---

## 5. Plan

| Phase | Work | Exit |
|---|---|---|
| **P1** | `HeaderPaging` dialect | unit tests for both the Link-header and `x-next-page` forms |
| **P2** | GitHub: models, client, tools, manifest | contract test passes; the PR filter and the 1,000 cap are tested |
| **P3** | GitLab: models, client, tools, manifest | same; `x-total` absence reads as unknown |
| **P4** | Register both; docs; cookbook | `loom toolsets` lists them; guide snippets run in CI |

**Testing.** No network. Transport stubs return the `{"items", "headers"}`
envelope so the paging dialect is exercised for real, error payloads come from
the docs above, and every guard is mutation-verified.

**The main risk.** Both APIs return **404 for a resource that exists but the
token cannot see**. Classifying that as "not found" is right, but the message
must say so, or a permissions problem is debugged as a typo.

---

## Sources

- [GitHub: Using pagination in the REST API](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- [GitHub: List repository issues](https://docs.github.com/en/rest/issues/issues)
- [GitHub: Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [GitHub: Search](https://docs.github.com/en/rest/search/search)
- [GitLab: REST API](https://docs.gitlab.com/api/rest/)
