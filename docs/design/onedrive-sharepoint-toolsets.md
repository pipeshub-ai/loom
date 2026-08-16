# OneDrive and SharePoint Online toolsets

Research notes and schema, written before the implementation. Every claim below
is from the Microsoft Graph v1.0 reference; the source page is named so a future
reader can re-check it rather than trust this file.

## The single most important fact

**OneDrive and SharePoint Online are one API.** Both are Microsoft Graph v1.0,
one bearer token, one error envelope, one pagination dialect. A SharePoint
document library *is* a `drive`, and its files *are* `driveItem`s — the same
resource OneDrive serves. `/sites/{id}/drive/root/children` and
`/me/drive/root/children` return the same shape.

So this is one shared layer plus two toolsets, exactly like `toolsets/google/`
serves Gmail, Calendar, Drive and Meet from one `auth.py`/`http.py`/`errors.py`.
Splitting them into two independent clients would duplicate the token cache, the
throttling rules, and the paging loop three ways and let them drift.

They stay **two separately-grantable toolsets** rather than one because the
grant boundary is real: a workflow that reads a team's SharePoint library has no
business reading an individual's OneDrive, and `GrantSet(toolsets=["onedrive"])`
is how that gets said.

```
src/loom/toolsets/microsoft/
    auth.py       MicrosoftAuth      — three credential modes, cached, locked
    errors.py     classify()         — Graph status + error.code taxonomy
    http.py       GraphSession       — request / download / send_bytes / paginate
    onedrive/     {models,client,tools,manifest}.py
    sharepoint/   {models,client,tools,manifest}.py
```

---

## Auth

Source: *OAuth 2.0 client credentials flow on the Microsoft identity platform*,
`learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow`.

Token endpoint, verified verbatim:

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=…&scope=https%3A%2F%2Fgraph.microsoft.com%2F.default
&client_secret=…&grant_type=client_credentials
```

Response: `{"token_type": "Bearer", "expires_in": 3599, "access_token": "…"}`.

Three modes, resolved from the environment in this order:

| Mode | Variables | Acts as |
|---|---|---|
| `client_credentials` | `MS_TENANT_ID` + `MS_CLIENT_ID` + `MS_CLIENT_SECRET` | the **app** (no user) |
| `refresh_token` | the three above + `MS_REFRESH_TOKEN` | a **person** (delegated) |
| `token` | `MS_GRAPH_ACCESS_TOKEN` | whatever minted it |

A durable credential outranks an ephemeral one, as in `GoogleAuth`: a refresh
token or a client secret mints fresh tokens indefinitely, while an access token
lives about an hour and a workflow that sleeps outlives it by design. So
`refresh_token` > `client_credentials` > `token`.

`scope=…/.default` is required for client credentials and is **not** a list of
scopes — it means "every application permission already granted to this app in
the tenant". Sending individual scopes there is an `invalid_scope` 400.

### `/me` does not exist under app-only auth

This is the one Microsoft-specific trap worth engineering against.

An app-only token (client credentials) has no user attached, so **every `/me/*`
path fails** — `/me/drive`, `/me/drive/root/children`, `/me`. Graph returns a
400 whose message is roughly *"/me request is only valid with delegated
authentication flow"*. That is an error, not a wrong answer, but it surfaces
from inside whichever step happened to be first and reads as a broken toolset
rather than a missing argument.

The clients therefore resolve a drive scope explicitly:

| Given | Path used |
|---|---|
| `drive_id="b!…"` | `/drives/{drive_id}` |
| `user_id="a@b.com"` | `/users/{user_id}/drive` |
| neither | `/me/drive` |

and **refuse before the request** when the auth mode is `client_credentials`
and neither is set, naming both fixes. A run that could never have succeeded
should not spend an HTTP round trip and a confusing traceback to learn it.

### Permissions worth documenting

Least-privileged, from each operation's reference page:

| Doing | Delegated | Application |
|---|---|---|
| read files | `Files.Read` | `Files.Read.All` |
| write files | `Files.ReadWrite` | `Files.ReadWrite.All` |
| read sites/lists | `Sites.Read.All` | `Sites.Read.All` |
| write list items | `Sites.ReadWrite.All` | `Sites.ReadWrite.All` |

Two gaps the docs call out explicitly and that a user will otherwise hit blind:
`GET /sites?search=` and `driveItem: search` **do not support the
`Sites.Selected` application permission**, and replacing the contents of a
sensitivity-labelled file is not supported app-only at all.

---

## Pagination

Source: *Paging Microsoft Graph data in your app*, `learn.microsoft.com/en-us/graph/paging`.

A paged response carries `@odata.nextLink`, a **complete URL**. The docs are
unusually direct about how to use it:

> Use the entire URL in the `@odata.nextLink` property in a GET request to
> retrieve the next page of results. […] **Don't try to extract the `$skiptoken`
> or `$skip` value and use it in a different request.**

That sentence decides the dialect. `CursorPaging` — which parses a value *out*
of a link and re-sends it as a parameter — is precisely what Graph tells you not
to do. `LinkPaging` already models "the response names the next request
outright", written for Salesforce's `nextRecordsUrl`, and it fits with no
changes to the dialect:

```python
from loom.toolsets.pagination import LinkPaging

LinkPaging(
    items="value",
    link_field="@odata.nextLink",
    done_field=None,          # Graph signals done by *omitting* the link
    total_field="@odata.count",
    size_param="$top",
    path_param="__next_url",
)
```

`done_field=None` is correct rather than lazy: `LinkPaging.read` already yields
`cursor=None` when the link is absent, and Graph has no "done" flag to read.

**One wrinkle, handled in the client.** Salesforce's `nextRecordsUrl` is a
*path* joined to the instance URL; Graph's `@odata.nextLink` is an *absolute
URL* that already encodes every original query parameter, `$top` included. So
when the session sees `__next_url` it issues a GET to that URL **verbatim with
no parameters of its own** — appending `$top` again would produce a duplicated
query parameter on a URL that already has one. That is also the literal reading
of the instruction quoted above.

Page sizes are conservative and per-endpoint, because the docs warn that an
over-large `$top` may be *ignored, clamped, or rejected* depending on the API,
and the first of those is the silent one. Defaults: 200 for drive and list item
collections, 100 for sites, permissions and columns. `@odata.count` is only
populated when `$count=true` is asked for, so `Results.total` is usually `None`
here — which `Results` already models honestly.

---

## Errors

Sources: *Microsoft Graph error responses and resource types*,
`learn.microsoft.com/en-us/graph/errors`; *Microsoft Graph throttling guidance*,
`learn.microsoft.com/en-us/graph/throttling`.

Envelope:

```json
{"error": {"code": "badRequest",
           "message": "…",
           "innerError": {"code": "invalidRange", "request-id": "…", "date": "…"},
           "details": []}}
```

The docs say to classify on `error.code`, not on `message`: *"You should only
code against error codes returned in `code` properties"* and *"Don't take any
dependency on the content of [message]"*. So the classifier reads status first
and `code` second, and `code` is what breaks the ties the status cannot.

| Status | Retryable | Note |
|---|---|---|
| 429 | **yes** | throttled; `Retry-After` header, always present except where the limits page says otherwise |
| 503 / 504 | yes | overloaded / upstream timeout; `Retry-After` may be present |
| 509 | yes | Bandwidth Limit Exceeded — "can retry the request again after more time" |
| 500 / 502 | yes | the upload-session page names these explicitly as retry-worthy |
| 401 | no | auth |
| 403 | no | permission or licence, incl. conditional-access `insufficient_claims` |
| 404 / 400 / 405 / 415 / 422 | no | |
| 507 | no | out of storage — waiting does not create quota |
| 423 | no | resource locked — see below |
| 409 | **conditional** | see below |

Two judgement calls, both documented in the code:

**423 Locked is classified permanent.** A document held open by a person is
genuinely transient, but "transient" here means minutes-to-hours while a retry
budget is two attempts over seconds. Retrying spends the budget and then reports
the same thing later, with the cause buried under three attempts. Failing at
once with `resourceLocked` is the actionable answer.

**409 splits on `Retry-After`.** The errors page says a
`Directory_ConcurrencyViolation` 409 *"can [be repeated] after some delay […]
If a Retry-After header is present, that value can be used"*, while a
driveItem 409 is `nameAlreadyExists` and will never succeed on repeat. The
header is the tell, so that is what the classifier reads.

Error codes that override the status: `activityLimitReached` and
`serviceNotAvailable` are retryable wherever they appear (OneDrive returns the
former as a 503 as often as a 429); `quotaLimitReached`, `accessDenied`,
`nameAlreadyExists`, `resourceLocked`, `invalidRequest`, `resyncRequired` are
permanent.

---

## Retry policy per operation

Following the convention already used by the other eleven toolsets:

| Class | Policy | Why |
|---|---|---|
| reads | `Retry(max_attempts=3)` | safe to repeat |
| `PATCH`/`PUT` by id | `Retry(max_attempts=2)` | idempotent — same id, same result |
| `POST` creating a thing | `Retry(max_attempts=1)` | no idempotency key; a timeout after the write double-creates |
| `DELETE` by id | `Retry(max_attempts=2)` | deleting a deleted item is a 404, not a second deletion |

`createUploadSession` and the chunked PUTs are **not** retried by the step
policy: the upload protocol has its own resume mechanism (`nextExpectedRanges`),
and a blind retry restarts a large transfer rather than resuming it.

---

## OneDrive operations (17)

Paths verified against *Working with files in Microsoft Graph*
(`api/resources/onedrive`) and the individual action pages cited inline.

`{root}` below is the resolved drive scope — `/me/drive`, `/users/{id}/drive`,
or `/drives/{id}`.

| Tool | Method + path | Effect |
|---|---|---|
| `onedrive_get_drive` | `GET {root}` | READ |
| `onedrive_list_children` | `GET {root}/root/children` or `{root}/items/{id}/children` | READ, paged |
| `onedrive_get_item` | `GET {root}/items/{id}` / `{root}/root:/{path}` | READ |
| `onedrive_search_items` | `GET {root}/root/search(q='…')` | READ, paged |
| `onedrive_download_file` | `GET {root}/items/{id}/content` | READ |
| `onedrive_upload_file` | `PUT {root}/items/{parent}:/{name}:/content` | WRITE |
| `onedrive_upload_large_file` | `POST …/createUploadSession` + chunked `PUT` | WRITE |
| `onedrive_create_folder` | `POST {root}/items/{parent}/children` | WRITE |
| `onedrive_delete_item` | `DELETE {root}/items/{id}` | DESTRUCTIVE |
| `onedrive_move_item` | `PATCH {root}/items/{id}` (parentReference) | WRITE |
| `onedrive_copy_item` | `POST {root}/items/{id}/copy` | WRITE |
| `onedrive_create_share_link` | `POST {root}/items/{id}/createLink` | WRITE |
| `onedrive_list_permissions` | `GET {root}/items/{id}/permissions` | READ |
| `onedrive_invite` | `POST {root}/items/{id}/invite` | WRITE |
| `onedrive_list_changes` | `GET {root}/root/delta` | READ, paged |
| `onedrive_list_recent` | `GET /me/drive/recent` | READ |
| `onedrive_whoami` | `GET /me` | READ, `resolves="user"` |

### Path addressing

Graph escapes a relative path with a colon: `{root}/root:/Reports/Q3.xlsx` for
an item, and `{root}/root:/Reports:/children` to list a folder's children. The
trailing colon is required when anything follows the path. Both forms are
supported by every item operation, so the clients accept `item_id` *or* `path`
and build whichever the caller supplied.

### Upload: the 10 MiB threshold

Source: *driveItem: createUploadSession*.

The threshold is Microsoft's **recommendation**, not a hard API limit — the
simple `PUT …/content` accepts considerably more than this. The guidance is what
we enforce because it is what avoids the failure: *"Use resumable file transfers
for files larger than 10 MiB"*, with fragments
*"a multiple of 320 KiB (327,680 bytes)"* and *"between 5-10 MiB"*, because
*"failing to use a fragment size that is a multiple of 320 KiB can result in
large file transfers failing after the last byte range is uploaded"* — a failure
that arrives only at the very end of a long upload.

So `onedrive_upload_file` **refuses** a payload over the threshold with an error
naming `onedrive_upload_large_file`, rather than sending it and failing
obscurely; `simple_upload_max` is a constructor argument for anyone who
disagrees. The chunk size is `320 KiB × 16 = 5 MiB`, which satisfies the
multiple rule by construction rather than by arithmetic nobody re-checks, and
lands inside the recommended 5–10 MiB band.

One more documented subtlety, and a real bug if missed: the fragment `PUT`s go
to the pre-authenticated `uploadUrl`, and *"If you include the Authorization
header when issuing the PUT call, it might result in an HTTP 401"*. The upload
loop must therefore send **no** auth header — the opposite of every other
request in the codebase.

### Copy is asynchronous

`POST /copy` returns `202 Accepted` with a `Location` header pointing at a
monitor URL, not the copied item. The tool returns that monitor URL rather than
pretending to have an item, and says so in its docstring.

---

## SharePoint Online operations (16)

Paths verified against *Working with SharePoint sites in Microsoft Graph*
(`api/resources/sharepoint`), *Search for sites* (`api/site-search`), and
*Create a new entry in a SharePoint list* (`api/listitem-create`).

| Tool | Method + path | Effect |
|---|---|---|
| `sharepoint_get_site` | `GET /sites/{id}` \| `/sites/root` \| `/sites/{host}:/{path}` | READ, `resolves="site"` |
| `sharepoint_search_sites` | `GET /sites?search={q}` | READ, `resolves="site"` |
| `sharepoint_list_subsites` | `GET /sites/{id}/sites` | READ, paged |
| `sharepoint_list_drives` | `GET /sites/{id}/drives` | READ, paged, `resolves="drive"` |
| `sharepoint_list_drive_items` | `GET /sites/{id}/drive/root/children` | READ, paged |
| `sharepoint_search_drive_items` | `GET /sites/{id}/drive/root/search(q='…')` | READ, paged |
| `sharepoint_download_file` | `GET /drives/{d}/items/{id}/content` | READ |
| `sharepoint_upload_file` | `PUT /drives/{d}/items/{p}:/{name}:/content` | WRITE |
| `sharepoint_create_share_link` | `POST /drives/{d}/items/{id}/createLink` | WRITE |
| `sharepoint_list_lists` | `GET /sites/{id}/lists` | READ, paged, `resolves="list"` |
| `sharepoint_get_list` | `GET /sites/{id}/lists/{list}` | READ |
| `sharepoint_list_columns` | `GET /sites/{id}/lists/{list}/columns` | READ, **`resolves="column"`** |
| `sharepoint_list_items` | `GET /sites/{id}/lists/{list}/items?$expand=fields` | READ, paged |
| `sharepoint_get_list_item` | `GET …/items/{item}?$expand=fields` | READ |
| `sharepoint_create_list_item` | `POST …/items` `{"fields": {…}}` | WRITE |
| `sharepoint_update_list_item` | `PATCH …/items/{item}/fields` | WRITE |
| `sharepoint_delete_list_item` | `DELETE …/items/{item}` | DESTRUCTIVE |

### Site ids are compound

A SharePoint site id is `{hostname},{spsite-guid},{spweb-guid}` — a single
string containing two commas. Three shorter forms also address a site, and all
four are legal:

```
/sites/root                                  the tenant's default site
/sites/contoso.sharepoint.com                root site of the default collection
/sites/contoso.sharepoint.com:/teams/hr      addressed by server-relative path
/sites/contoso.sharepoint.com,{guid},{guid}  the full compound id
```

`sharepoint_get_site` accepts all of them, because a workflow author has
whichever one their browser URL gave them, and the compound id is not something
anyone types from memory.

### Column internal names — the entity-resolution hazard

This is SharePoint's version of the problem `resolves` exists for, and it is
worth stating plainly because it produces a **wrong answer, not an error**.

A list item's fields are keyed by a column's *internal* name, which is not what
the site shows. A column displayed as "Due Date" is internally `DueDate` or
`Due_x0020_Date` depending on how it was created; "Title" is `Title` but
"Assigned To" is `AssignedTo` or `Assigned_x0020_To`. A spec says "set Due Date
to Friday"; an agent writes `fields={"Due Date": …}`; and SharePoint accepts a
`POST` containing unknown keys for some column types and simply does not set
them. The item is created, the workflow reports success, and the field is empty.

`sharepoint_list_columns` therefore carries `resolves="column"` and returns both
names side by side, and the manifest tells the agent to resolve display names to
internal names before writing. That is the same ladder
`DEFAULT_SYSTEM_PROMPT` already describes for users and statuses, applied to a
vocabulary that happens to be per-list rather than per-tenant.

### `$expand=fields` is not optional

`GET /sites/{id}/lists/{id}/items` returns items whose `fields` are **hidden by
default** — the reference calls references "hidden" unless expanded. Without
`$expand=fields` every item comes back with ids and timestamps and no data, so
the client always sends it and the caller cannot forget to.

---

## Change tracking (`delta`)

Source: *driveItem: delta*.

Worth having because Graph's own throttling guidance names polling as a leading
cause of throttling and points at delta as the fix. Three properties shape the
tool:

- The response ends with `@odata.deltaLink` instead of `@odata.nextLink`; the
  delta link is what you store and pass back next time. The tool returns it
  alongside the items so a workflow can keep it in `ctx.state`.
- Deleted items appear as normal entries carrying a `deleted` facet, so a caller
  distinguishes them by facet, not by absence. The model exposes this as a
  `deleted: bool`.
- `?token=latest` returns an **empty** page plus the current delta link — the
  way to start watching from now without enumerating everything first.
- A stale token gets `410 Gone` with `resyncRequired`; that is permanent (the
  stored token must be discarded), and the error message says so.

---

## Testing plan

Three layers, matching what the other toolsets do:

1. **Request shape** — `tests/test_toolset_requests.py`-style `Recorder`
   transport asserting method, URL, params, and body for every public client
   method. This is the layer that catches a `$expand` that was never sent.
2. **Behaviour** — pagination across a two-page `@odata.nextLink` chain
   (including that the second request carries *no* extra params), error
   classification per status and per `error.code`, the app-only `/me` refusal,
   the upload size cliff, path-vs-id addressing, and the no-Authorization rule
   on fragment PUTs.
3. **Contract** — enrolment in `FIRST_PARTY` and `CLIENTS` in
   `tests/test_manifest_imports.py`, which executes every declared import and
   checks client-pages ⟹ tool-returns-`Results` ⟹ manifest-declares-pagination.

Plus the standing guard: no tool parameter may shadow `ctx.step`'s own
keywords (`name`, `retry`, `timeout`, …), which is why the upload tools take
`filename` rather than `name`.

---

## Phases

| Phase | Content |
|---|---|
| **P1** | `microsoft/{auth,errors,http}.py` + their tests |
| **P2** | OneDrive: models, client, tools, manifest |
| **P3** | SharePoint: models, client, tools, manifest |
| **P4** | Registration (`BUILTIN_TOOLSETS`), CLI verification, contract-test enrolment |
| **P5** | Docs (`docs/guides/toolsets.md`, `CLAUDE.md`), cookbook example, mutation verification |
