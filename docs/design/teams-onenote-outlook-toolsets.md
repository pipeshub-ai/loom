# Teams, OneNote, and Outlook toolsets

Research notes and schema, written before the implementation. Every claim below
is from the Microsoft Graph v1.0 reference; the source page is named so a future
reader can re-check it rather than trust this file.

## What is already built

These are Microsoft Graph, so `toolsets/microsoft/` already supplies everything
structural: `auth.py` (three credential modes, cached, locked), `errors.py`
(status + `error.code` taxonomy), `http.py` (`GraphSession`, `LinkPaging` over
`@odata.nextLink`), and `addressing.py`. **No new auth, paging, or error code is
needed.** What follows is the per-workload knowledge that is not already
captured.

One thing *is* added to the shared layer: `scope.py`. Four more clients now need
the "`/me` does not exist under an app-only token" refusal that OneDrive
introduced, and a fifth copy of it would be a fifth chance to word it
differently.

## Four toolsets, not three

The request names three products. The implementation ships **four toolsets**,
because Outlook is two grant boundaries wearing one brand:

| Toolset | Covers |
|---|---|
| `teams` | teams, channels, channel messages and replies, chats, members |
| `onenote` | notebooks, sections, pages, page HTML |
| `outlook_mail` | messages, folders, send, drafts, attachments |
| `outlook_calendar` | calendars, events, calendar view, availability |

This is the same call already made for Google — four toolsets over one auth
layer — and for the same stated reason: *a workflow reading a calendar has no
business holding a mail-send scope*. `GrantSet(toolsets=["outlook_calendar"])`
should mean exactly that, and cannot if reading a colleague's free/busy also
grants the ability to send mail as them. Splitting is cheap here because the
shared layer is already shared.

---

## The theme: Microsoft restricts app-only auth, inconsistently

Three separate workloads, three different restrictions, all of which surface as
confusing failures rather than clear ones. This is the single most valuable
thing the research turned up, and it shapes every client below.

| Workload | Restriction | Source |
|---|---|---|
| Anything under `/me` | No signed-in user exists; 400 that reads as a broken toolset | Graph auth model |
| **Sending a Teams message** | **Application permissions not supported at all** — only `Teamwork.Migrate.All`, for migration | `chatMessage: post` |
| **OneNote** | Overview says app-only is **not supported**; per-operation pages list `Notes.ReadWrite.All` anyway | `onenote-api-overview` vs `section-post-pages` |

How each is handled:

- **`/me` paths** — refused before the request by the shared `UserScope`, naming
  both fixes (pass a user, or authenticate as a person). Unambiguous, so a hard
  refusal is right.
- **Teams send** — *not* refused, because a migration app holding
  `Teamwork.Migrate.All` is a legitimate caller. Instead the constraint is
  stated in the tool docstring and the manifest, where the coding agent reads
  it, and a 403 carries a message pointing at delegated credentials.
- **OneNote** — *not* refused, because Microsoft's own documentation
  contradicts itself and refusing would break users for whom it works. The
  contradiction is documented in the manifest verbatim. Since every OneNote
  path is a `/me` or `/users/{id}` path, the `UserScope` refusal already covers
  the case that definitely cannot work.

The general rule: **refuse what cannot work, document what might not.** A hard
refusal is a strong claim, and it should only be made where the API is
unambiguous.

---

## Teams

Sources: `teams-api-overview`, `channel-list-messages`, `chatMessage-post`,
`chat-list`.

### Polling is contractually limited

> If your app polls to see whether a resource has changed, you can only do that
> once per day. […] Apps that don't follow these polling requirements will be
> considered in violation of the Microsoft APIs Terms of Use. This may result in
> additional throttling or the suspension or termination of your use of the
> Microsoft APIs.

This is not a rate limit — it is a term of use, and it is unusually explicit.
A workflow on a five-minute cron reading `/me/joinedTeams` is in violation. The
manifest and the tool docstrings say so, because the coding agent is exactly the
thing that would otherwise generate that cron. Change notifications
(subscriptions) are the documented alternative.

### Only `$top` and `$expand` work on messages

> The other OData query parameters aren't currently supported.

No `$filter`, no `$orderby`, no `$select` on channel messages. So the client
does not accept them — offering a `filter_query` argument that Graph ignores
would be the silent-wrong-answer shape this codebase keeps designing against.
`$top` defaults to 20 and caps at **50**; chats cap at **50** too.

### Replies are a second, nested pagination

`$expand=replies` returns up to 200 replies by default and up to 1,000, with a
*separate* `replies@odata.nextLink` inside each message. A caller that reads
`value[].replies` and stops has silently truncated a long thread. So replies get
their own operation (`teams_list_message_replies`) rather than being a flag on
the listing, and the listing's docstring says why.

### Ids contain characters that must survive the URL

A channel id is `19:4a95f7d8db4c4e7fae857bcebe0623e6@thread.tacv2` — a colon and
an `@` inside a path segment. A chat id is worse:
`19:...@unq.gbl.spaces`. These are percent-encoded by the client; the shared
`item_address` work already established that escaping is the client's job.

### `$expand=members` silently caps at 25

> when the API is called with the `$expand=members` query parameter, the
> response returns a maximum of **25 member items**, even if a larger `$top`
> value is specified.

A truncation with no marker. `teams_list_chat_members` exists as a separate,
paged operation so a caller that needs the full membership has a way to get it.

### Operations (16)

| Tool | Method + path | Effect |
|---|---|---|
| `teams_list_joined_teams` | `GET {user}/joinedTeams` | READ, `resolves="team"` |
| `teams_get_team` | `GET /teams/{id}` | READ |
| `teams_list_channels` | `GET /teams/{id}/channels` | READ, `resolves="channel"` |
| `teams_get_channel` | `GET /teams/{id}/channels/{id}` | READ |
| `teams_list_channel_messages` | `GET .../messages` | READ, paged |
| `teams_get_channel_message` | `GET .../messages/{id}` | READ |
| `teams_list_message_replies` | `GET .../messages/{id}/replies` | READ, paged |
| `teams_send_channel_message` | `POST .../messages` | WRITE |
| `teams_reply_to_message` | `POST .../messages/{id}/replies` | WRITE |
| `teams_list_team_members` | `GET /teams/{id}/members` | READ, paged |
| `teams_list_chats` | `GET {user}/chats` | READ, `resolves="chat"` |
| `teams_get_chat` | `GET /chats/{id}` | READ |
| `teams_list_chat_messages` | `GET /chats/{id}/messages` | READ, paged |
| `teams_send_chat_message` | `POST /chats/{id}/messages` | WRITE |
| `teams_list_chat_members` | `GET /chats/{id}/members` | READ, paged |
| `teams_whoami` | `GET /me` | READ, `resolves="user"` |

Sends are **not retried**: no idempotency key, and a retry posts the message
twice, visibly, to everyone.

---

## OneNote

Sources: `onenote-api-overview`, `section-post-pages`, `onenotePage`.

### Content is HTML, not JSON

This is the shape difference that matters. A page's *metadata* is JSON; its
*content* is an HTML document fetched from a separate endpoint
(`GET /pages/{id}/content`) and written as an HTML request body. So:

- `onenote_get_page_content` returns `str` (the HTML), not a model.
- `onenote_create_page` sends `Content-Type: text/html` with an HTML document
  whose `<title>` becomes the page title. There is no `title` field to set.

The reference's own example shows the required document shape:

```html
<!DOCTYPE html>
<html>
  <head>
    <title>Page title</title>
    <meta name="created" content="2015-07-22T09:00:00-08:00" />
  </head>
  <body>…</body>
</html>
```

A caller passing plain text would create a page whose entire content is one
unstyled line and whose title is empty, so the client *builds* the document from
a title and a body fragment, and takes raw HTML only when asked explicitly.

Binary attachments need `multipart/form-data` with a part named `Presentation`
holding the HTML. That is a distinct enough operation that it is out of scope
for the first version, and the manifest says so rather than implying it works.

### The hierarchy is four levels and the middle one is optional

`notebook → sectionGroup? → section → page`. Section groups are skipped by most
tenants, so `onenote_list_sections` lists a notebook's sections directly and a
separate `onenote_list_section_groups` exists for the tenants that use them.

### Operations (13)

| Tool | Method + path | Effect |
|---|---|---|
| `onenote_list_notebooks` | `GET {user}/onenote/notebooks` | READ, paged, `resolves="notebook"` |
| `onenote_get_notebook` | `GET .../notebooks/{id}` | READ |
| `onenote_list_sections` | `GET .../notebooks/{id}/sections` | READ, paged, `resolves="section"` |
| `onenote_list_all_sections` | `GET {user}/onenote/sections` | READ, paged |
| `onenote_list_section_groups` | `GET .../notebooks/{id}/sectionGroups` | READ, paged |
| `onenote_list_pages` | `GET .../sections/{id}/pages` | READ, paged |
| `onenote_search_pages` | `GET {user}/onenote/pages?$search=` | READ, paged |
| `onenote_get_page` | `GET .../pages/{id}` | READ |
| `onenote_get_page_content` | `GET .../pages/{id}/content` | READ, returns HTML |
| `onenote_create_page` | `POST .../sections/{id}/pages` | WRITE |
| `onenote_append_to_page` | `PATCH .../pages/{id}/content` | WRITE |
| `onenote_delete_page` | `DELETE .../pages/{id}` | DESTRUCTIVE |
| `onenote_create_section` | `POST .../notebooks/{id}/sections` | WRITE |

`PATCH /content` takes an array of commands, not a document:

```json
[{"target": "body", "action": "append", "content": "<p>text</p>"}]
```

`target` accepts `body`, `title`, or a generated element id — which is why
`GET /content?includeIDs=true` exists and why the client offers it.

---

## Outlook mail

Sources: `user-list-messages`, `user-sendMail`, `search-concept-messages`.

### Bodies come back as HTML unless you ask otherwise

> Currently, this operation returns message bodies in only HTML format.

`Prefer: outlook.body-content-type="text"` is the documented way to get text,
and a workflow feeding a message body to a model wants text almost every time —
HTML spends tokens on markup and buries the content. So the client sends the
header **by default** and exposes `body_as_html=True` to opt out. A default that
quietly costs a caller three times the tokens is a bad default.

### `$filter` and `$orderby` have an ordering contract

Genuinely surprising, and quoted in full because paraphrasing loses it:

> 1. Properties that appear in `$orderby` must also appear in `$filter`.
> 2. Properties that appear in `$orderby` are in the same order as in `$filter`.
> 3. Properties that are present in `$orderby` appear in `$filter` before any
>    properties that aren't.
>
> Failing to do this results in […] Error code: `InefficientFilter`, Error
> message: `The restriction or sort order is too complex for this operation.`

An error, not a wrong answer — but one whose message names neither the filter
nor the sort. The tool docstring states the rule where the coding agent will
read it before writing the query.

### `$top` is 1–1000 and a big page can time out

Default page size 10. The reference warns that a large page of full messages
"may trigger the gateway timeout (HTTP 504)" and recommends `$select`. The
client therefore sends a default `$select` of the fields the model actually
carries, and pages at 50 rather than the maximum.

### Search reports a page count as a total

From the Microsoft Search API's own limitations:

> For messages, the **total** property of the searchHitsContainer type contains
> the number of results on the page, **not the total number of matching
> results**.

That is precisely the bug `Results` exists to prevent, arriving pre-made from
the API. So `Results.total` is **deliberately left unset** for message search,
and `.complete` is derived from `moreResultsAvailable`. Copying `total` through
would have a workflow report "12 results" for a mailbox containing thousands.

The simpler `$search` on `/me/messages` is what the toolset actually uses; it
pages with `@odata.nextLink` like everything else, and Graph ranks the results
rather than sorting them, so the search tool takes no ordering argument at all.

### `sendMail` returns 202, which is not delivery

> A `202 Accepted` response code indicates that the request has been accepted;
> however, it doesn't indicate that the request processing has completed.

So `outlook_send_message` returns `True` meaning *accepted*, and its docstring
says so. Claiming "sent" would be a workflow reporting something untrue.

Not retried: no idempotency key, and a retry after a timeout sends the mail
twice.

### Operations (16)

| Tool | Method + path | Effect |
|---|---|---|
| `outlook_list_messages` | `GET {user}/messages` | READ, paged |
| `outlook_search_messages` | `GET {user}/messages?$search=` | READ, paged |
| `outlook_get_message` | `GET {user}/messages/{id}` | READ |
| `outlook_list_folder_messages` | `GET {user}/mailFolders/{id}/messages` | READ, paged |
| `outlook_list_folders` | `GET {user}/mailFolders` | READ, paged, `resolves="folder"` |
| `outlook_send_message` | `POST {user}/sendMail` | WRITE |
| `outlook_reply_to_message` | `POST .../messages/{id}/reply` | WRITE |
| `outlook_forward_message` | `POST .../messages/{id}/forward` | WRITE |
| `outlook_create_draft` | `POST {user}/messages` | WRITE |
| `outlook_update_message` | `PATCH .../messages/{id}` | WRITE |
| `outlook_move_message` | `POST .../messages/{id}/move` | WRITE |
| `outlook_delete_message` | `DELETE .../messages/{id}` | DESTRUCTIVE |
| `outlook_list_attachments` | `GET .../messages/{id}/attachments` | READ |
| `outlook_get_attachment` | `GET .../attachments/{id}` | READ, returns Attachment |
| `outlook_mark_read` | `PATCH .../messages/{id}` | WRITE |
| `outlook_whoami` | `GET /me` | READ, `resolves="user"` |

Deleting goes to Deleted Items and is recoverable, so it is `DESTRUCTIVE` but
retryable — the same call Gmail's trash made.

---

## Outlook calendar

Source: `user-list-calendarview`.

### `calendarView` is the one that answers the question people ask

The single most important distinction in this toolset, and the direct analogue
of the `singleEvents=True` decision already made for Google Calendar.

- `GET /me/events` returns **series masters**. A weekly stand-up appears *once*,
  as one event carrying a recurrence rule — not as occurrences.
- `GET /me/calendarView?startDateTime=…&endDateTime=…` returns "the occurrences,
  exceptions, and single instances of events" in the range — the expanded view.

So "what is on my calendar on Tuesday" answered over `/events` misses every
recurring meeting, and returns something that looks like a valid, short answer.
`outlook_list_calendar_view` is therefore the primary read, and
`outlook_list_events` documents what it is for (editing a series) rather than
being the obvious default.

Both `startDateTime` and `endDateTime` are **required** query parameters.

### Times come back in UTC unless the header says otherwise

`Prefer: outlook.timezone="…"` sets the response timezone. Without it everything
is UTC, which is correct but reads wrong in a summary a person sees. The client
takes a `timezone` argument and sends the header when given.

A subtlety worth encoding: the values of `startDateTime`/`endDateTime` are
interpreted using *their own* offset and are **not** affected by that header. So
a caller passing a naive `"2026-01-05T09:00:00"` with `timezone="Pacific
Standard Time"` gets a UTC-interpreted window, silently shifted. The client
documents this and recommends offsets in the range values.

### Operations (13)

| Tool | Method + path | Effect |
|---|---|---|
| `outlook_list_calendars` | `GET {user}/calendars` | READ, paged, `resolves="calendar"` |
| `outlook_list_calendar_view` | `GET {user}/calendarView` | READ, paged |
| `outlook_list_events` | `GET {user}/events` | READ, paged |
| `outlook_get_event` | `GET {user}/events/{id}` | READ |
| `outlook_create_event` | `POST {user}/events` | WRITE |
| `outlook_update_event` | `PATCH {user}/events/{id}` | WRITE |
| `outlook_delete_event` | `DELETE {user}/events/{id}` | DESTRUCTIVE |
| `outlook_accept_event` | `POST .../events/{id}/accept` | WRITE |
| `outlook_decline_event` | `POST .../events/{id}/decline` | WRITE |
| `outlook_tentatively_accept_event` | `POST .../tentativelyAccept` | WRITE |
| `outlook_cancel_event` | `POST .../events/{id}/cancel` | WRITE |
| `outlook_find_meeting_times` | `POST {user}/findMeetingTimes` | READ |
| `outlook_get_schedule` | `POST {user}/calendar/getSchedule` | READ |

`outlook_create_event` takes `add_teams_meeting=True`, which sets
`isOnlineMeeting` and `onlineMeetingProvider` — the Outlook analogue of the
`conferenceDataVersion=1` trap already handled for Google Calendar, where
creating the event and creating the meeting are one call or the link is missing.

---

## Retry policy

Unchanged from the convention the other toolsets use:

| Class | Policy |
|---|---|
| reads | `Retry(max_attempts=3)` |
| `PATCH`/`PUT` by id | `Retry(max_attempts=2)` |
| anything that posts, sends, or replies | `Retry(max_attempts=1)` |
| `DELETE` by id | `Retry(max_attempts=2)` |

Every send in these four toolsets is unretried, and the reason is the same in
each case: no idempotency key, and the duplicate is visible to people.

---

## Testing plan

Same three layers as OneDrive/SharePoint, driving a real `httpx` transport:

1. **Request shape** — URL, query, headers, body per client method. This is the
   layer that catches a missing `Prefer` header or an `$expand` never sent.
2. **Behaviour** — the app-only refusals, the Teams `$top` cap, replies as a
   separate pagination, the HTML page-document construction, the
   `calendarView` required parameters, `Results.total` deliberately unset for
   search, and 202-is-not-delivery.
3. **Contract** — enrolment in `FIRST_PARTY` and `CLIENTS`, so client-pages ⟹
   tool-returns-`Results` ⟹ manifest-declares-pagination is enforced.

Plus the standing guard that no tool parameter shadows `ctx.step`'s reserved
keywords — which bites here, because "subject", "body" and "content" are fine
but `name` and `timeout` are not.

## Phases

| Phase | Content |
|---|---|
| **P1** | `microsoft/scope.py` — the shared `/me` refusal, extracted from OneDrive |
| **P2** | Teams: models, client, tools, manifest |
| **P3** | OneNote: models, client, tools, manifest |
| **P4** | Outlook mail and calendar |
| **P5** | Registration, tests, CLI/MCP verification, docs, cookbook |
