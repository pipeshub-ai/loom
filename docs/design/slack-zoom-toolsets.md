# Slack and Zoom toolsets — API notes, schema, and plan

Research notes taken before implementation, so the design decisions below can be
checked against what the APIs actually do rather than against memory of them.
Every claim here was verified against vendor documentation in August 2026;
sources are listed at the end.

---

## 1. Slack

**Base URL** `https://slack.com/api/` — method names are path segments
(`https://slack.com/api/conversations.list`), not REST resources.

### 1.1 Errors do not use HTTP status codes

The single most important fact about this API, and the one that shapes the
client:

```json
HTTP 200 OK
{"ok": false, "error": "channel_not_found"}
```

Every Web API response carries a top-level `ok` boolean. A failure is an
**HTTP 200** with `ok: false` and a machine-readable `error` string.

This is exactly the failure class this codebase already worries about — *fewer
rows and no error*. A client written against the Google/Jira shape (raise on
`status >= 400`, else decode) would treat every Slack failure as a success and
return an empty list. A workflow posting to a channel it was never invited to
would report success and deliver nothing.

**Design consequence:** `SlackSession` checks the body, not the status line.
Errors classify from the `error` *string*, not from the status code.

Error strings that must be classified (from the method reference):

| `error` | Retryable? | Meaning |
|---|---|---|
| `ratelimited` | yes | also arrives as HTTP 429 |
| `service_unavailable`, `internal_error`, `fatal_error` | yes | Slack-side |
| `invalid_auth`, `not_authed`, `account_inactive`, `token_expired`, `token_revoked` | **no** | auth |
| `missing_scope`, `not_allowed_token_type` | **no** | the app lacks a scope |
| `channel_not_found`, `user_not_found`, `users_not_found`, `message_not_found` | **no** | bad argument |
| `not_in_channel`, `is_archived`, `cant_invite_self` | **no** | state |

`missing_scope` deserves its own error type: the fix is re-installing the app
with another scope, which is a different action from "the id was wrong", and
Slack helpfully returns the scope it wanted in `needed`.

### 1.2 Pagination — cursor, nested

- Request: `cursor` (omit on first call) and `limit`.
- Response: **`response_metadata.next_cursor`** — nested, not top level.
- Exhausted: `next_cursor` is `""`, `null`, or absent.
- `limit` max is 1000; Slack recommends 100–200.

**No new dialect needed** — though it looked like one at first.
`TokenPaging` already addresses a nested field through a tuple `token_field`
(HubSpot's lives at `paging.next.after`) and already treats an empty token as
exhausted, so Slack is:

```python
from loom.toolsets.pagination import TokenPaging

TokenPaging(items="channels", size_param="limit", token_param="cursor",
            token_field=("response_metadata", "next_cursor"))
```

A `NestedTokenPaging` class was written first and then deleted: it was the same
dialect written twice, which is precisely the second source of truth the
pagination module exists to avoid.

Older paging styles still exist on `search.messages`, `search.files` and
`files.list` (`page`/`count`). Those operations are **excluded** from this
toolset rather than modelled with a fifth dialect — see §1.5.

### 1.3 Rate limits

Per method, per workspace, per app, per minute. Tier 1 ≈ 1+/min, Tier 2 ≈
20+/min, Tier 3 ≈ 50+/min, Tier 4 ≈ 100+/min. Exceeding gives **HTTP 429 with a
`Retry-After` header**. `chat.postMessage` is special-cased at roughly one
message per second per channel.

Note the interaction with pagination: a paged read at Tier 3 is 50 requests a
minute, so a 5,000-message history is minutes of wall clock. The `max_results`
defaults are set low for that reason.

### 1.4 Auth

Bot tokens (`xoxb-`) via OAuth v2. `loom connect slack` already works —
`slack` is a pre-configured provider in `connectors/oauth_providers.py`
(`https://slack.com/oauth/v2/authorize`, `https://slack.com/api/oauth.v2.access`,
no PKCE). Environment fallback: `SLACK_BOT_TOKEN`, then `SLACK_TOKEN`.

Two Slack-specific wrinkles:

- The OAuth v2 token response puts the bot token at
  **`access_token`** with the *bot* identity under `bot_user_id`, while a user
  token comes back nested under `authed_user.access_token`. Only the bot token
  is used here.
- Token rotation is opt-in per app. When enabled, tokens expire in 12 hours and
  refresh normally — which the credential store's early-refresh window now
  handles without Slack-specific code.

### 1.5 Uploads changed

`files.upload` **stopped working on 11 March 2025**. The replacement is three
steps: `files.getUploadURLExternal` → `POST` the bytes to the returned URL →
`files.completeUploadExternal`. Skipping the third step aborts the upload.

That is worth wrapping in one tool: a workflow author asked to "share this file
in #general" should not be writing a three-step protocol, and the middle step is
a raw POST to a URL that is not a Slack API endpoint.

`files.list` and `search.messages` are excluded: they use the legacy `page`/
`count` dialect, and `search.*` additionally requires a *user* token, which is a
different credential from the bot token everything else here uses. Declaring
them would mean a manifest promising operations that fail on the configured
token.

### 1.6 Entity resolution

`users.lookupByEmail` takes an email and returns a user object; it answers
`users_not_found` when nobody matches (**including deactivated users**). It
requires the `users:read.email` scope, which is separate from `users:read`.

This is the `resolves="user"` operation for Slack, and the reason it matters is
the usual one: `chat.postMessage` takes a channel id or a user id, never a
name. A workflow that passes "@ada" where an id belongs posts to nothing.

Channels have the same problem — `conversations.list` + exact-name match is the
`resolves="channel"` path.

### 1.7 Schema — Slack models

```
SlackMessage      ts, channel, user, bot_id, text, thread_ts, reply_count,
                  permalink, subtype, files[SlackFileRef], reactions[dict]
SlackChannel      id, name, is_private, is_archived, is_member, topic, purpose,
                  num_members, created
SlackUser         id, name, real_name, display_name, email, is_bot, is_admin,
                  deleted, timezone, title
SlackFileRef      id, name, mimetype, size, permalink, url_private
PostedMessage     ts, channel, permalink        (the receipt from a post)
```

`ts` is Slack's message identifier *and* its timestamp — a string like
`"1718280000.123456"`. It is kept as a string deliberately: it is an id, and
float-parsing it loses precision and breaks the `thread_ts` join.

---

## 2. Zoom

**Base URL** `https://api.zoom.us/v2`.

### 2.1 Auth — Server-to-Server OAuth

The default for an unattended workflow:

```
POST https://zoom.us/oauth/token
     ?grant_type=account_credentials&account_id={ACCOUNT_ID}
Authorization: Basic base64(client_id:client_secret)
```

Returns a bearer token valid for one hour. There is no refresh token — the
grant *is* the credential, so a new token is minted on demand. That makes it
unlike every OAuth integration already here, and it means the credential-store
refresh machinery does not apply: the client mints and caches its own token,
the same shape `GoogleAuth` uses for a service account.

Environment: `ZOOM_ACCOUNT_ID` + `ZOOM_CLIENT_ID` + `ZOOM_CLIENT_SECRET`, or
`ZOOM_ACCESS_TOKEN` for a token minted elsewhere. General (user-delegated)
OAuth is also supported via `loom connect zoom`, which needs a new provider
entry — Zoom is not in `oauth_providers.py` today.

### 2.2 Pagination — token, top level, and it expires

- Request: `page_size` + `next_page_token`.
- Response: `next_page_token` at the **top level**; `""` when exhausted.
- `page_size` max: **300** for light endpoints, 30/50/100 for heavy ones.
- **The token expires after 15 minutes.**

The existing `TokenPaging` dialect fits exactly — `size_param="page_size"`,
`token_param="next_page_token"`, `token_field="next_page_token"` — and its
`body.get(field) or None` already turns `""` into "exhausted".

The 15-minute expiry is worth documenting on `Results.cursor`: carrying a cursor
in `ctx.state` across a durable park is a documented pattern in this codebase,
and for Zoom that pattern is only valid inside a 15-minute window.

`GET /users` additionally supports legacy `page_number`/`page_count`. Not used —
`next_page_token` works there too, and supporting both would be two code paths
for one answer.

### 2.3 The UUID gotcha

A meeting has **two** identifiers: a numeric `id` (reusable, identifies the
*series*) and a `uuid` (identifies one *occurrence*). Past-meeting endpoints
take the UUID.

A UUID is base64 and can contain `/` or `+`. Zoom's documented rule: **if the
UUID begins with `/` or contains `//`, it must be double URL-encoded**, or the
API answers `3001 Meeting does not exist` — a *not found* for a meeting that
does exist. Silent wrong answer, again.

**Design consequence:** one helper encodes UUIDs, applies the double-encode rule
conditionally, and is used by every past-meeting path.

### 2.4 Rate limits

Tiered by weight — Light/Medium/Heavy/Resource-intensive — as QPS *and* a daily
cap. Exceeding gives **HTTP 429**. Zoom's error body carries a numeric `code`
alongside `message`, so classification reads that.

Notable codes: `124` invalid access token, `1001` user not found, `3001` meeting
does not exist, `300` invalid request, `429` rate limit.

### 2.5 Schema — Zoom models

```
ZoomMeeting       id, uuid, topic, agenda, type, status, start_time, duration,
                  timezone, join_url, start_url, password, host_id, host_email,
                  created_at
ZoomUser          id, email, first_name, last_name, display_name, type, status,
                  timezone, dept, last_login_time
ZoomParticipant   id, user_id, name, email, join_time, leave_time, duration,
                  status
ZoomRecording     meeting_id, meeting_uuid, topic, start_time, duration,
                  total_size, files[ZoomRecordingFile]
ZoomRecordingFile id, file_type, file_extension, file_size, download_url,
                  play_url, recording_type, status
```

`start_url` carries an embedded host token and **must not be logged or shared** —
anyone holding it joins as the host. It is on the model because a workflow
legitimately needs it, and flagged in the docstring for the same reason.

---

## 3. What this adds to the codebase

| Change | Why |
|---|---|
| `toolsets/slack/` | client, models, errors, tools, manifest |
| `toolsets/zoom/` | client, models, auth, tools, manifest |
| `zoom` in `oauth_providers.py` | so `loom connect zoom` works |
| Two entries in `BUILTIN_TOOLSETS` | so a generated workflow can resolve them with nothing registered |

Slack gets its own `errors.py` because its taxonomy is body-driven and unlike
every other toolset here. Zoom reuses the HTTP-status shape and needs only a
thin classifier.

## 4. Operations

**Slack (21)** — `conversations.list/history/replies/info/members/join/create/
invite/archive/set_topic`, `chat.post/reply/update/delete/post_ephemeral/
schedule`, `users.list/info/lookup_by_email`, `reactions.add`, `files.upload`.

**Zoom (17)** — `meetings.list/get/create/update/delete/list_registrants`,
`past.participants/details`, `users.list/get/get_me/find_by_email`,
`recordings.list/get/delete/download`, `webinars.list`.

Both mark a resolver: Slack `users.lookup_by_email` (`resolves="user"`) and
`conversations.find` (`resolves="channel"`); Zoom `users.find_by_email`
(`resolves="user"`).

## 5. Test plan

Mirrors `tests/test_google_drive.py`: `httpx.MockTransport`, assertions on the
*request* (URL, query string, body) rather than only the parsed result, because
every failure mode identified above is silent.

Specifically pinned:

- Slack `ok: false` on an HTTP 200 raises, and raises the *classified* type.
- `missing_scope` names the scope Slack asked for.
- Slack paging follows `response_metadata.next_cursor` and stops on `""`,
  through the existing `TokenPaging` rather than a new class.
- A `Results` from either toolset reports `.complete` honestly.
- Slack upload performs all three steps, in order, to two different hosts.
- `chat.postMessage` is not retried (no idempotency key → duplicate messages).
- Zoom mints a token once and caches it; a 401 remints exactly once.
- Zoom double-encodes a UUID starting with `/` and single-encodes one that does
  not.
- Zoom `page_size` is clamped per endpoint weight.
- Manifest ↔ implementation pagination agreement, via the existing
  `tests/test_manifest_imports.py` drift check.

## Sources

- [Slack — pagination](https://docs.slack.dev/apis/web-api/pagination)
- [Slack — rate limits](https://docs.slack.dev/apis/web-api/rate-limits)
- [Slack — Web API overview / `ok` envelope](https://docs.slack.dev/apis/web-api/)
- [Slack — `users.lookupByEmail`](https://docs.slack.dev/reference/methods/users.lookupByEmail)
- [Slack — `files.upload` deprecation](https://docs.slack.dev/changelog/2024/05/16/apps/)
- [Slack — `files.getUploadURLExternal`](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/)
- [Zoom — pagination](https://developers.zoom.us/docs/api/pagination/)
- [Zoom — rate limits](https://developers.zoom.us/docs/api/rate-limits/)
- [Zoom — meeting ids vs UUIDs](https://developers.zoom.us/blog/meeting-api-querying-tips-part1/)
