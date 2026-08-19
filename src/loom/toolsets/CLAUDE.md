# Toolset internals

Per-vendor detail for `src/loom/toolsets/`. Loaded when you work in this tree,
so the root `CLAUDE.md` does not carry it into every session.

The root file holds what is cross-cutting — the three-layer lazy tool system,
`toolsets/pagination.py`, `resolves`/entity resolution, grant validation, and
`docs/guides/toolsets.md` (the end-to-end walkthrough for writing a new one).
Read that first; this file assumes it.

Everything below is unchanged from the root `CLAUDE.md`, where it used to live.

### GitHub and GitLab Toolsets

`toolsets/github/` (15 operations) and `toolsets/gitlab/` (14). Research and
schema in `docs/design/github-gitlab-toolsets.md`.

**Both signal pagination in response headers**, which no earlier dialect could
read — by the time `page_through` hands a style the response, headers are gone.
So these clients return `{"items": rows, "headers": {…}}` and `HeaderPaging`
reads both halves: GitHub's `Link: …; rel="next"` (absence of `rel="next"` is
the documented end) and GitLab's `x-next-page` (empty means the same as
absent). Plain data deliberately — an httpx object in a paging style would make
the style untestable without a transport.

**GitHub's issue listings contain pull requests.** Its own model: *every pull
request is an issue, but not every issue is a pull request*, told apart by a
`pull_request` key. `github_list_issues` filters them out by default, because
"how many open issues" answered over an unfiltered listing is wrong with
nothing to notice. `GitHubIssue.is_pull_request` keeps it visible, and the
filtered `Results` drops its `total` rather than reporting one that counted PRs.

**Three GitHub signals mean "partial", none of them an error**: search caps at
1,000 results however large `total_count` is, search is limited to 30
requests/minute, and `incomplete_results` means the query timed out server-side.
All three surface through `.complete`.

**GitLab's `iid` is not its `id`.** The number in a URL is the per-project
`iid`; the global `id` is a different number most endpoints reject. Both are
carried under GitLab's own names. Two more traps encoded: `state="opened"`, not
`"open"` (an unknown state is ignored and returns everything), and closing takes
`state_event="close"`, not a state. A `group/project` path is URL-encoded for
you — an unencoded slash is a different route and 404s.

**Both return 404 for a resource the token cannot see**, so `GitHubNotFound` and
`GitLabNotFound` say so in the message; otherwise a permissions problem is
debugged as a typo. GitHub's 403 splits on `x-ratelimit-remaining`: zero is
"wait", anything else is "never".

### Salesforce and HubSpot Toolsets

`toolsets/salesforce/` (11 operations) and `toolsets/hubspot/` (15) — the two
CRMs, built from vendor docs read during the work rather than recalled. The
research, schema, and phasing are in
`docs/design/salesforce-hubspot-toolsets.md`.

**Salesforce has no constant base URL.** Every org answers on its own host,
returned by the OAuth exchange as `instance_url`; `login.salesforce.com`
authenticates and does not serve data. The client therefore refuses to
construct without either an explicit instance URL or the refresh credentials
that produce one — a wrong base URL otherwise surfaces as 404s that look like
missing records. Sandboxes need `SALESFORCE_LOGIN_URL=https://test.salesforce.com`;
a sandbox token against the production host fails as `invalid_grant`, which
reads like a bad token rather than a wrong host.

**It also refreshes.** Access tokens expire mid-workflow, so the client owns
the exchange, under a lock, and retries a 401 exactly once — the same
arrangement `toolsets/google/auth.py` uses. Twice would turn a revoked grant
into a loop against the login host.

**A 403 splits.** `REQUEST_LIMIT_EXCEEDED` is org quota and clears if you wait;
any other 403 is a permission that never will. Same rule the Google toolset
applies to quota versus scope, and the reason `errorCode` is carried on the
exception rather than just the status.

**SOQL literals are escaped.** `O'Brien` is the most predictable surname in any
CRM, and unescaped it terminates the string literal.

**HubSpot's two caps both truncate silently.** Search returns at most 10,000
results — paging past it is a 400 — so the client stops there and reports
`complete=False` rather than turning a large query into an error at the end.
And properties are opt-in: a response carries only what `properties=` asked
for, so a default field list is declared per object type. Omitting it returns
a contact with no company, which reads as missing data rather than as an
under-specified request.

Both APIs are uniform across object types, so both expose **generic CRUD plus
typed finders** rather than five near-identical copies — a custom `Deal__c` or
a custom HubSpot object is reachable without a library change. HubSpot's path
version is a constructor argument, since it has begun publishing dated versions
(`/crm/objects/2026-03/…`) alongside `v3`.

### ClickUp and Asana Toolsets

`toolsets/clickup/` and `toolsets/asana/` — 14 operations each, the same three
files every shipped toolset uses, pure httpx, no vendor SDK.

**Auth differs in a way worth knowing.** Asana is a plain bearer token
(`ASANA_ACCESS_TOKEN`). ClickUp has two shapes sent *differently*: a personal
token goes in `Authorization` **raw**, an OAuth token takes the `Bearer` prefix,
and sending a personal token as `Bearer pk_…` returns 401 with no hint why.
`CLICKUP_OAUTH_TOKEN` wins over `CLICKUP_API_TOKEN` when both are set.

**Paging.** ClickUp counts *pages* — `page=0,1,…` with a `last_page` flag — which
is `PageNumberPaging`, a dialect distinct from `OffsetPaging` because sending a
row offset where a page number belongs returns the wrong window and no error.
Asana carries an opaque offset inside `next_page.uri`, which `CursorPaging`
already parses.

**Asana's search neither pages nor ships on every plan.** It returns
`list[AsanaTask]`, not `Results`, and declares `pagination=False` — Asana states
results are unstable across identical queries, so there is no page to follow and
no coverage to report. `AsanaPremiumRequired` is its own error class because a
workflow can act on it: fall back to `asana_list_tasks` on a known project.

**Writes with no idempotency key are not retried** — creating a task, posting a
comment. A timeout after the service accepted it is indistinguishable from a
failure, so a retry files it twice. Updates and deletes retry once, since naming
the same task twice reaches the same end state.

Both mark their people-lookup as `resolves="user"`: every write in either API
takes an id (numeric in ClickUp, a `gid` in Asana), and a name passed where an id
belongs matches nothing and reports **no error**.

### Google Workspace Toolsets

`toolsets/google/` — four separately-grantable toolsets (`gmail`,
`google_calendar`, `google_drive`, `google_meet`) over one shared OAuth layer,
pure httpx, no vendor SDK. Four rather than one because a workflow reading a
calendar has no business holding a mail-send or a Drive-delete scope, and
`GrantSet(toolsets=["google_calendar"])` should mean exactly that.
`GOOGLE_MANIFESTS` registers all four in a line.

Credentials resolve from the environment in order: `GOOGLE_ACCESS_TOKEN`, then
`GOOGLE_CLIENT_ID`+`GOOGLE_CLIENT_SECRET`+`GOOGLE_REFRESH_TOKEN`, then
`GOOGLE_SERVICE_ACCOUNT_FILE` (the only one needing the `[google]` extra). One
cached token serves all four, refreshed under a lock — and a later toolset's
scopes are *merged into* it rather than dropped. Without that the second
toolset used gets a token carrying only the first one's scopes, which under a
service account is a Drive call authenticated for Gmail: a 403 that reads as a
broken credential rather than a shared cache. `python -m loom.toolsets.google.setup
--scopes drive` mints a refresh token; `read`/`write` are composed from the
per-toolset sets, so a scope added to one cannot be missing from the combined one.

**Errors are classified, not blanket-retried.** Google 4xx (bar 429) raises a
`NonRetryableError` subclass, so a plain `Retry` policy stops on a malformed
query rather than sleeping through three attempts. A 403 splits on `reason`:
quota is retryable, missing scope is not.

**Anything that a retry would duplicate has retries off.**
`gmail_send_message`/`gmail_reply_to_message`/`gmail_forward_message`,
`drive_upload_file`, and `meet_create_space`: none of those APIs offers an
idempotency key, so a timeout after the effect is indistinguishable from a
failure. Journaling covers replay; this covers the attempt. Calendar and Drive
metadata writes retry once — a duplicate event or a re-applied rename is
recoverable.

**Defaults that avoid surprises:** `send_updates="none"` and Drive's
`notify=False`, so bulk work does not email hundreds of people as a side effect
of a default; `singleEvents=True` so recurring series come back as instances;
`trashed = false` on every Drive search, so a workflow processing a folder does
not re-process the bin.

**Pagination is per-endpoint, because Google is not consistent with itself.**
Gmail and Calendar read `maxResults`, Drive and Meet read `pageSize`, and each
*ignores* the other rather than rejecting it — so the wrong name is not an
error, it is every request silently asking for the server default. Page
ceilings differ too (Drive files 1000, Drive permissions 100, Meet artifacts
10). `GoogleSession.paginate(size_param=…, page_size=…)` takes both, every
paged read returns `Results`, and `tests/test_manifest_imports.py` checks the
client, the return type, and the manifest agree.

**Every one of the six marks a resolver**, because every one of them accepts an
id where a person says a name: Gmail a *label* (`gmail_modify_labels` takes
`Label_7`, and passing "Urgent" applies nothing and reports success), Calendar a
*calendar* (a secondary calendar's id is an opaque
`...@group.calendar.google.com`), Drive a *folder*, Slack a *channel* and a
*user*, Zoom a *user*. Meet is the exception and needs none — its inputs are
resource names produced by other calls.

A resolver that pages has a **third** answer, and the two that scan a list say
so: `slack_find_channel` and `calendar_find_calendar` raise when the scan ran
out before matching, rather than answering `None`. "Not found" is a fact a
caller acts on — it creates the channel, or reports the gap — and it is only a
fact if the whole list was searched. `None` from a truncated scan silently loses
things that plainly exist, in exactly the workspaces big enough for it to
matter.

**Timeouts are configurable, and split in two.** Every client takes `timeout=`
(30s, an API call) and the four that move bytes also take `transfer_timeout=`
(300s). One budget for both would either fail every large Drive export and Zoom
recording, or leave an ordinary API call hanging for five minutes.

Two seams matter more than the individual tools, because in both cases the
obvious toolset is the wrong one:

- **Scheduling a meeting is a Calendar operation.** The Meet API cannot
  schedule anything — `meet_create_space` makes a room with a link and no time,
  no invitees and no calendar entry, which looks like success until nobody
  joins. `calendar_create_event(..., add_meet=True)` is the real path; it sends
  `conferenceDataVersion=1`, without which Google accepts the request, ignores
  the conference block, and returns an event with no link and no error. The
  `requestId` is *derived* from the event rather than random, so it is both
  deterministic and an idempotency key against a re-driven step.
- **A meeting's recording and transcript live in Drive.** Meet reports the ids;
  `drive_download_file` fetches a recording and `drive_export_file` reads a
  transcript — which is a Google Doc and so has no bytes to download.
  `MeetRecording.is_ready` exists because Meet reports a recording the moment
  it stops and the Drive file appears later.

**Drive's failure modes are silent, so the client closes them.** A missing
`fields` mask returns a file with no timestamps; a missing
`includeItemsFromAllDrives` returns an empty list for a team whose files live
on a shared drive; downloading a Google Doc is a 403 that reads as a
permissions problem. All three answer 200-shaped and make a workflow report
something untrue, so the mask is always sent, both shared-drive flags are
always on, and a Doc download is refused up front naming the export call.
`drive_find_folder` is marked `resolves="folder"` and matches exactly — a
`name contains` query returns "Reports Archive" for "Reports", and writing to
the wrong folder is worse than finding nothing.

**Gmail permanent delete is deliberately not exposed.** `messages.delete` needs
`https://mail.google.com/`, a *restricted* scope granting full mailbox access,
so shipping one unrecoverable operation would widen what every Gmail workflow
is granted. Trash is recoverable for 30 days and `gmail_untrash_message` undoes
it. Threads are the better triage unit — Gmail's UI groups by conversation, so
labelling one message of a thread looks like nothing happened — and
`gmail_list_threads` is one request per page where `gmail_search_messages` is
one per hit. `gmail_create_draft` is the safe half of sending: an agent writes,
`ctx.wait_for_approval()` parks, a human sends.

### Web Search Toolsets

`toolsets/exa/`, `toolsets/tavily/`, `toolsets/duckduckgo/` — three because
they are not interchangeable, and the manifests carry enough for the coding
agent to choose.

| Toolset | Credential | Paginates | Distinctive |
|---|---|---|---|
| `exa` (4 ops) | `EXA_API_KEY` (`x-api-key`) | no, cap 100 | Embeddings search — a *description*, not keywords. Page text, similar pages, cited answers. |
| `tavily` (3 ops) | `TAVILY_API_KEY` (bearer) | no, cap 20 | `include_answer` returns a written answer beside the results. News/finance topics, page extract, site map. |
| `duckduckgo` (3 ops) | none | **yes** | No key. Best-effort — see below. |

**Neither Exa nor Tavily has a cursor of any kind**, so a request above the cap
cannot be made whole. Both clients **refuse** rather than clamp: a caller that
asked for 500, received 100, and reported 100 as the total is the failure
`Results` exists to prevent, one layer earlier. The error is a
`NonRetryableError` naming the ceiling and the alternative, so `Retry` stops
instead of failing the same impossible request three times. Their reads return
plain `list`s with `pagination=False`; `duckduckgo` returns `Results` because
`ddgs` exposes a page number and the client follows it.

**Partial success is carried.** Exa's `/contents` and Tavily's `/extract`
answer 200 for a request in which some URLs failed, so the side array reaches
the caller as `.failed` — a short list with nothing saying it is short is the
same bug as a silent page cap.

**DuckDuckGo is not an official API.** They publish none; their one documented
endpoint returns instant answers and no web results. This rides on the
third-party `ddgs` package, which parses result pages — `pip install
'loomflow[duckduckgo]'`, optional precisely because it is a different
reliability contract from the other two. Two things are engineered around it:
being blocked raises a *retryable* `DuckDuckGoRateLimited` rather than
returning `[]` (which a workflow reads as "nothing matched" and acts on), and
the client drives the paging itself so `.complete` distinguishes "that is
everything" from "it stopped early" — asking `ddgs` for 30 returns whatever it
managed, silently. A *soft* block, no rows and no error, stays
indistinguishable from a genuine miss; that one cannot be fixed here. `ddgs` is
synchronous, so every call goes through `asyncio.to_thread`.

**All ten operations are `READ` and `idempotent`**, which is load-bearing
rather than bookkeeping: web search is the canonical taint source, so under
`TaintBroker` a run that has searched needs a human before it writes. Classified
as writes, no read could taint and the rule would be unreachable.

Tavily's own `timeout` parameter is exposed as `read_timeout`, because
`ctx.step` claims `timeout` and a tool declaring it is unreachable by keyword.

### OneDrive and SharePoint Toolsets

`docs/design/onedrive-sharepoint-toolsets.md` is the research these were built
from — Graph API notes, schemas, and the decision each fact forced, with sources.

`toolsets/microsoft/` — two separately-grantable toolsets (`onedrive`,
`sharepoint`; 18 and 19 operations) over one shared Graph layer, pure httpx, no
vendor SDK. **A SharePoint document library *is* a `drive` and its files *are*
`driveItem`s**, so `models.py` is shared and a file moved between the two keeps
one shape. They stay separate toolsets because the grant boundary is real.

Credentials resolve in order: `MS_TENANT_ID`+`MS_CLIENT_ID`+`MS_CLIENT_SECRET`
+`MS_REFRESH_TOKEN` (delegated — acts as a person), the same three without it
(client credentials — acts as the app), then `MS_GRAPH_ACCESS_TOKEN`.
`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` are accepted as
fallbacks, since that is the trio the Azure SDKs already put in an environment.
The durable credential outranks the ready-made one for the same reason it does
in `GoogleAuth`. One cached token serves both toolsets.

**`/me` does not exist under an app-only token.** Client credentials
authenticate the application, so there is no signed-in person and `/me/drive`
fails with a 400 that reads as a broken toolset rather than a missing argument.
The clients refuse **before the request**, naming both fixes: `MS_ONEDRIVE_USER`
/ `MS_ONEDRIVE_DRIVE_ID`, or authenticate as a person.

**Paging reuses `LinkPaging`**, and the reference is why: `@odata.nextLink` is a
complete URL and the docs say *"Don't try to extract the `$skiptoken` […] and
use it in a different request"* — which is what `CursorPaging` does. The
follow-up therefore sends the URL verbatim with no parameters of its own; note
that `httpx` clears a URL's query when handed even an empty `params` dict, which
silently re-fetches page one forever.

**A SharePoint column has two names and the wrong one fails silently.** Item
values are keyed by the *internal* name ("Due Date" is `DueDate` or
`Due_x0020_Date`); a write using display names is accepted and sets nothing, so
the row is created and the value is missing. `sharepoint_list_columns` carries
`resolves="column"` and returns both. Likewise `$expand=fields` is always sent,
because Graph hides item values by default and an unexpanded read looks like an
empty list rather than a missing parameter.

Two smaller traps, both test-pinned: Graph's path escape needs a *second* colon
when anything follows it (`/root:/Reports:/children`), which is why
`addressing.py` exists; and an upload session's fragment `PUT`s must carry **no**
`Authorization` header — the upload URL is pre-authenticated and signing it can
401 — making them the only deliberately unsigned requests in the codebase.
`onedrive_upload_file` refuses over 10 MiB and names
`onedrive_upload_large_file`, whose 5 MiB chunk is a multiple of 320 KiB by
construction, because a violation of that rule fails only after the last
fragment. `onedrive_list_changes` wraps `delta` — Graph names polling as a
leading cause of throttling — and returns the delta link beside the items,
since a caller that drops it re-enumerates the whole drive next time.

### Teams, OneNote, and Outlook Toolsets

`docs/design/teams-onenote-outlook-toolsets.md` is the research these were built
from. Four toolsets — `teams` (16 ops), `onenote` (12), `outlook_mail` (15),
`outlook_calendar` (11) — over the same `toolsets/microsoft/` layer OneDrive and
SharePoint use. **Outlook is two toolsets, not one**, for the reason the Google
package already gives: reading a calendar should not confer sending mail.

**The theme is that Microsoft restricts app-only auth inconsistently**, and the
rule adopted is *refuse what cannot work, document what might not*.
`microsoft/scope.py::user_root` is the shared refusal, now used by five clients:
a `/me` path under client credentials cannot resolve, so it raises before the
request naming `MS_TEAMS_USER`/`MS_ONENOTE_USER`/`MS_OUTLOOK_USER` and
`MS_REFRESH_TOKEN`. A resource addressable without a user — `drive_id`, a
OneNote `site_id`/`group_id` — bypasses it. Two restrictions are *not* refused:
**sending a Teams message is delegated-only** (application permissions cover
only `Teamwork.Migrate.All`, and a migration app is a real caller), and
**OneNote's overview says app-only is unsupported while its per-operation pages
list an application permission** — a contradiction quoted in the manifest rather
than resolved by guessing.

**Teams.** Graph's own docs say polling a resource more than once a day violates
the Microsoft APIs Terms of Use; that is in the manifest because the coding
agent is what would otherwise write the cron. Channel messages support **only**
`$top` and `$expand` — no filter, no sort, silently ignored — so the client
offers neither. `$top` caps at 50. Replies get their own operation because
`$expand=replies` truncates at 200 behind a *nested* `replies@odata.nextLink`,
and `$expand=members` on chats caps at 25 with no marker. Channel ids
(`19:…@thread.tacv2`) carry a colon and an `@` through a path segment.

**OneNote.** A page's content is an **HTML document, not JSON**: reading returns
a string, and creating posts `Content-Type: text/html` whose `<title>` *becomes*
the page title — there is no title field, so posting a bare fragment yields an
untitled page and no error. `create_page` therefore assembles the document from
a title and a body. Updates are `target`/`action`/`content` commands, and
targeting anything but `body`/`title` needs `includeIDs=true` on the read.

**Outlook mail.** Bodies come back as HTML unless `Prefer:
outlook.body-content-type="text"` is sent, so the client sends it by default —
and re-sends it per page, since a next-link carries parameters but not headers.
`$filter` and `$orderby` have an ordering contract (sorted properties must
appear in the filter, in order, first) or Graph answers `InefficientFilter`.
Listings project a field set because a large page of full messages can hit a
504. `sendMail` returns **202 = accepted, not delivered**, and the tool says so.

**Outlook calendar.** `calendarView` versus `events` is the whole design:
`/events` returns series *masters*, so "what is on Tuesday" asked there misses
every recurring meeting and returns a plausible short list. `calendarView`
expands occurrences over a required window — the same call `singleEvents=True`
makes for Google Calendar. Times are UTC unless `Prefer: outlook.timezone` is
set, and that header does *not* reinterpret the window, so the range values need
their own offsets. `add_teams_meeting` sets `isOnlineMeeting` **and**
`onlineMeetingProvider`; setting only the first yields an event that claims to
be online and carries no join link. `cancel` notifies attendees where `delete`
leaves their invitations in place.

### Slack and Zoom Toolsets

`docs/design/slack-zoom-toolsets.md` is the research these were built from —
API notes, schemas, and the decisions each fact forced, with sources.

**Slack's failures are HTTP 200s.** `{"ok": false, "error": "channel_not_found"}`
with a 200 status line. A client written to the shape every other toolset here
uses — raise above 399, else decode — treats every failure as an *empty
success*, so a workflow posting to a channel it was never invited to reports the
message as sent and delivers nothing. `toolsets/slack/errors.py` therefore
classifies on the `error` string, and every response goes through
`raise_for_status`. `missing_scope` gets its own type because the fix is a
different action in kind — a reinstall, by a person — and Slack names the scope
it wanted.

**Slack's cursor is nested, and that needed no new dialect.** It lives at
`response_metadata.next_cursor` and signals exhaustion with an empty string.
Both are already `TokenPaging` behaviours — a tuple `token_field` addresses a
nested position, as HubSpot's `paging.next.after` does — so Slack uses that. A
`NestedTokenPaging` class was written and then deleted: it was the same dialect
twice, which is the second source of truth `pagination.py` exists to prevent.

**Everything in Slack takes an id, never a name.** `#incidents` is what people
type and `C024BE91L` is what Slack accepts, so `slack_find_channel`
(`resolves="channel"`) and `slack_find_user_by_email` (`resolves="user"`) are
the two resolvers, and both match *exactly* — a prefix match would return
`#eng-alerts` for `#eng` and post to the wrong room. `files.upload` stopped
working in March 2025, so `slack_upload_file` is three calls to two hosts behind
one tool, and the bot token is deliberately not sent to the pre-signed storage
URL.

**Zoom has two identifiers and they are not interchangeable.** `meeting.id` is
numeric and names the *series*; `meeting.uuid` names one *occurrence*, and every
past-meeting endpoint takes the second. Worse, a uuid is base64: one beginning
with `/` or containing `//` must be **double** URL-encoded or Zoom answers
`3001 Meeting does not exist` for a meeting that plainly does. `encode_uuid`
applies the rule conditionally — double-encoding one that does not need it
produces the same 3001 from the other direction.

**`meeting.start_url` is a host credential**, carrying an embedded token that
lets anyone who opens it run the meeting. That warning is a pydantic `Field`
description rather than an attribute docstring, deliberately: only the former
reaches `model_json_schema()`, which is what the manifest publishes and what the
coding agent reads. A warning that lives only in the source is one the agent
writing the code never sees.

**A Zoom daily rate limit is non-retryable on purpose.** Both limits arrive as a
429, and only the message text tells them apart — but a per-second limit clears
while a step backs off and a daily one does not clear until midnight UTC, so
retrying against it burns the run to reach the same answer. `ZoomDailyLimitReached`
is a `NonRetryableError`; `ZoomRateLimited` is not.

**Auth differs between them.** Slack is an ordinary bot token — `loom connect
slack` (already a provider) or `$SLACK_BOT_TOKEN`. Zoom's default is
Server-to-Server OAuth, which has **no refresh token**: the client id and secret
*are* the durable credential and an hourly token is minted from them on demand,
so the credential-store refresh machinery does not apply and `toolsets/zoom/auth.py`
caches its own under a lock, as `GoogleAuth` does for a service account.
`loom connect zoom` (a new provider entry) covers the user-delegated case.

Neither posts nor schedules under a retry: `chat.postMessage` and
`meetings.create` have no idempotency key, so a retry after a post-delivery
timeout posts the message twice — visibly, to everyone — or puts a second
meeting on the calendar with a different join link.


### Stripe, QuickBooks, Airtable, and Google Sheets

Four toolsets built for the reference workflows, and the reason they are grouped
here is that **each one handles the same problem differently, and the difference
is the API's, not a preference**.

**Idempotency, three ways.** Stripe has a first-class `Idempotency-Key` and
replays the original response for 24 hours, so its writes *do* retry — and the
key is a required parameter rather than something the client mints, because a
key generated inside the client is new on every attempt, which is exactly the
case it exists to prevent. QuickBooks and Airtable have no key at all, so their
creates carry `Retry(max_attempts=1)` and the way to make one safe across runs
is to stamp an external id and look for it first — `quickbooks_find_sales_receipts`
is that lookup, and it is the *workflow's* check rather than something hidden in
a client. Sheets is the same: append is not retried, update is.

`verify_effect_profile` is what keeps those honest, and it earned its place
immediately: it caught two operations declared `idempotent=False` sitting on a
bare `@step`, which **retries three times**. A docstring saying "not retried"
over a decorator that retries is worse than no docstring. The same mistake was
in six reference-workflow steps and nothing there was checking.

**Stripe.** Requests are **form-encoded, not JSON**, with bracket syntax for
nesting (`metadata[order]=A-1`, `expand[0]=customer`); posting JSON returns a
400 naming a parameter you did send. `None` is dropped rather than sent as the
string `"None"`. Errors classify on `error.type` before the status, because a
`card_error` will decline again while an `api_error` is worth another attempt —
merging them is how a declined card burns three retries. A publishable key
(`pk_…`) is refused at construction: it authenticates and can read almost
nothing, so it otherwise surfaces as a scatter of unrelated 401s.

Paging needed a new dialect. `starting_after` takes **the id of the last row on
the current page**, and the envelope carries only `has_more` — so the
continuation is not in a field at all, which no existing style could express.
`RowIdPaging` is that, and an empty page ends the walk whatever `has_more` says,
because there is no row to continue from.

`StripeSource` is the webhook half. `Stripe-Signature` carries **several**
signatures (`t=…,v1=…,v1=…`) — one per active secret during a rotation — and any
matching is valid; checking only the first breaks every delivery for the length
of the rollover. There is no handshake, so `challenge()` returns `None`, which
is the whole implementation. The `evt_…` id is stable across the three days
Stripe retries.

**QuickBooks.** No constant base URL — a realm id names one company file and is
in every path, so the client refuses to construct without one, as Salesforce
does for `instance_url`. Sandbox and production are different hosts. Every
update carries a **SyncToken**: optimistic concurrency, and a stale one is
*rejected* rather than merged, which is why every model carries it and why the
read before a write is not optional. An update must also send `sparse: true` or
QuickBooks treats the payload as the whole record and blanks every field not
sent — data loss that returns 200. Queries are SQL-shaped and are not SQL:
`STARTPOSITION` is **1-based**, so `OffsetPaging` is wrong here and the client
drives `collect` itself. Intuit rotates the refresh token on every exchange, so
the client keeps the new one; dropping it makes the *next* refresh fail, hours
later, looking unrelated.

**Airtable.** A response is keyed by field **name**, so renaming a column in the
UI changes every key and a workflow reading the old one gets `None` rather than
an error. An empty field is *omitted* rather than nulled, so "empty" and "no
such column" are indistinguishable in a response — `airtable_list_fields` is the
resolver that tells them apart, and returns the stable `fld…` id beside the
name. Writes cap at **ten records** and are batched for you, but there is no
transaction across batches: a failure partway leaves the earlier batches
written, and saying so beats pretending. Only `PATCH` is exposed — Airtable's
`PUT` clears every field not sent. Five requests per second per base, and a 429
locks the base for **thirty seconds**, which is why nothing fans out internally.

**Google Sheets.** A fifth separately-grantable toolset over the same
`toolsets/google` auth layer, for the reason the other four are separate: a
workflow appending to a tracking sheet has no business holding a mail-send
scope. Four silent wrong answers are closed. Rows come back **ragged** — Sheets
truncates trailing empty cells, so a header of eight columns and a row whose
last three are blank return eight and five, and indexing column 7 raises on some
rows and not others; `rows_padded()` and `as_dicts()` are the safe reads.
`valueInputOption` decides whether text is data or input: `USER_ENTERED` makes
`=SUM(A1:A9)` a formula and turns `1/2` into a date, so `RAW` is the default
here and the choice is explicit at every write. Append targets the **table the
range sits in**, not the sheet, so appending to `A1` when an unrelated block is
at the top writes into that block. And `insertDataOption=INSERT_ROWS` is always
sent, because Sheets' own default overwrites whatever is below the table.

### Jira Toolset

`toolsets/jira/` — 18 operations. Older than the four-file convention, so it
carries the traps the later toolsets encode structurally.

**A custom field has two identifiers and neither is the one on screen.**
`JiraField.id` (`customfield_10016`) is what a REST payload uses — the
`custom_fields=` list on a read, the `fields=` dict on an update.
`JiraField.clause_names` (`["Story Points", "cf[10016]"]`) is what JQL
accepts. Putting the REST id in JQL matches nothing *and does not error*;
putting a clause name in an update payload is a 400. The number differs per
site, so nothing about the pair can be derived — `jira_resolve_field`
(`resolves="field"`) is what joins the display name a spec uses to the id a
payload needs, and the coding agent is told to call it before filtering.

**`GET /field` is not the list of fields.** On an instance with only
team-managed projects it silently omits app-created custom fields — about 45 of
85 in the report this was built from — and the missing field looks like a field
nobody created. `list_fields` reads `field/search`, which returns them
regardless of project type. `field` is still called, because it is the only
endpoint carrying `clauseNames`; where it does not know an id, the clause names
are synthesised as `[name, cf[customId]]` rather than left empty, since an empty
list reads as "unusable in JQL".

**Custom field values are not flattened.** A number field is a number, a select
is `{"value": "High", "id": "10001"}`, a user is an account object. Which half
of a select a caller wants depends on the field, so `JiraIssue.custom_fields`
carries Jira's own shape. An empty mapping means *not requested* — Jira returns
only the fields `fields=` named — and never "this issue has none".

**`jira_update_issue(fields=...)` is the raw REST payload**, not a friendly
mapping: named entities are objects (`{"priority": {"name": "High"}}`),
`description` is Atlassian Document Format rather than a string, and the wrong
shape is a 400 rather than a coercion.

**The 400 body is the only place Jira names the offending field.**
`{"errors": {"customfield_10042": "Field ... cannot be set"}}` — and the client
raised through `raise_for_status`, which discards the body, so setting an
unknown field produced `Client error '400 Bad Request' for url ...` and three
retries of it. `_classify` keeps the map on `JiraError.fields`; a 4xx is a
`JiraPermanentError` so a plain `Retry` stops on it. 404 is `JiraNotFound` and
means "not visible to you" as often as "absent" — Jira returns it for a
resource the token cannot see.

**`createmeta` is deliberately unused.** It is the documented way to learn which
custom fields a project requires, and it is deprecated and observed to drop
fields it should return. A create that omits a mandatory field is refused by
name, which is a worse error message and a true one.

**`jira_resolve_user`'s parameter is `name`, which `ctx.step` claims for
itself** — it is on the known-offenders list in
`tests/test_manifest_imports.py` and is unreachable by keyword. `jira_resolve_field`
takes `field_name` for that reason; do not copy the older signature.

#### Doc reconciliation, and what it changed

These four were the first toolsets here written from **recalled** API knowledge
rather than from vendor docs read during the work — unlike Salesforce and
HubSpot above. A reconciliation pass against the primary references afterwards
found five real defects and confirmed the rest. Recording both halves, because
"we checked" is only useful with the score attached.

**Stripe — four fixes.**

- `error.type` has exactly four values: `api_error`, `card_error`,
  `idempotency_error`, `invalid_request_error`. There is **no**
  `authentication_error` and **no** `rate_limit_error`, though both read as
  though there should be. The table listed them; the entries were dead (status
  fallbacks caught 401 and 429) but claimed a contract the API does not have.
- **403** is "the API key doesn't have permissions" — a restricted key missing
  a scope. It was falling into the generic 4xx branch and reporting as a
  malformed request, pointing a reader at the parameters instead of the key.
- **409** is the idempotency-conflict status. Without a type in the body it was
  reported as an invalid request.
- **424** ("External Dependency Failed") is the one 4xx worth retrying. It was
  non-retryable.
- `decline_code` — the *issuer's* reason — is now carried. It is the actionable
  half: `code` says `card_declined` where `decline_code` says
  `insufficient_funds` against `lost_card`.
- The pinned version was `2024-06-20`, a bare date. Stripe versions now carry
  the major release name (`2026-07-29.dahlia`), and the models here were
  written against current docs — so the old pin would have parsed a different
  response shape while looking equally valid.

**QuickBooks — one hardening.** Whether `PrimaryEmailAddr` is filterable could
not be confirmed from Intuit's own reference (their pages do not render for
retrieval, and third-party sources disagree). QuickBooks marks each attribute
filterable or not and *raises* on a `WHERE` against one that is not, so
`find_customer_by_email` now tries the filter and falls back to a bounded scan
on a validation fault. The scan raises rather than answering `None` when it
runs out first — "not found" makes the caller create a customer, and that is
only safe if the whole set was searched.

Confirmed correct: `STARTPOSITION` is 1-based, `MAXRESULTS` caps at 1000
(default 100).

**Airtable — nothing wrong.** Every claim held, several verbatim: *"A `PUT`
request will perform a destructive update and clear all unincluded cell
values"*; *"Returned records do not include any fields with 'empty' values"*;
"5 requests per second per base" with a 429 and *"wait 30 seconds"*; the
ten-record batch cap; `v0/meta/bases/{baseId}/tables` under `schema.bases:read`.

**Google Sheets — one over-claim removed.** *"Empty trailing rows and columns
are omitted"* is verbatim, and so are the `RAW`/`USER_ENTERED` definitions and
append's table-finding behaviour. But the reference states **no default** for
`insertDataOption`, where the docstring asserted Stripe-style that the default
was `OVERWRITE`. The client sends `INSERT_ROWS` explicitly either way; the
claim about the default was removed rather than left as a plausible guess.
