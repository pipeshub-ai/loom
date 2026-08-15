# Gmail and Google Calendar

Two toolsets over one OAuth layer. Pure REST via httpx — no
`google-api-python-client`, and the common auth paths need no extra at all.

<!-- docs-preamble -->

Every example on this page assumes:

```python
from loom import Context, Runtime, workflow
from loom.security.grants import GrantSet
from loom.toolsets.google.calendar.tools import calendar_list_events
from loom.toolsets.google.gmail.tools import (
    gmail_modify_labels,
    gmail_search_messages,
    gmail_send_message,
)
```

```python
from loom.toolsets.google.gmail.tools import (
    gmail_search_messages, gmail_send_message,
)
from loom.toolsets.google.calendar.tools import calendar_list_events

@workflow(name="triage")
async def triage(ctx: Context, _in: str) -> int:
    unread = await ctx.step(gmail_search_messages, "is:unread newer_than:1d", 10)
    for message in unread:
        if "invoice" in message.subject.lower():
            await ctx.step(gmail_modify_labels, message.id, ["STARRED"], ["UNREAD"])
    return len(unread)
```

## Getting credentials

**Fast, for a look around** — [OAuth 2.0 Playground](https://developers.google.com/oauthplayground):
pick the Gmail and Calendar scopes in step 1, authorize, exchange for tokens in
step 2, and copy the access token. Valid for one hour, needs no Cloud project.

```bash
export GOOGLE_ACCESS_TOKEN='ya29...'
```

**For anything that runs more than once**, you want a refresh token, since an
access token is dead within the hour. One-time setup in the
[Cloud console](https://console.cloud.google.com):

1. Enable the [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
   and the [Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
2. Configure the OAuth consent screen (External is fine) and add your own
   account under **Test users**
3. **Credentials → Create credentials → OAuth client ID → Desktop app**

Then let the shipped helper do the flow:

```bash
python -m loom.toolsets.google.setup \
    --client-id ... --client-secret ... --scopes write
```

It opens a browser, catches the redirect on `http://127.0.0.1:8931/`, exchanges
the code, and prints the three variables to paste into `.env`. `--scopes` takes
`read` (default), `write`, `gmail`, or `calendar`.

The port is fixed so a Web application client can register one redirect URI and
keep it. If something else holds 8931 the helper takes any free port and says
so; `--port N` pins it and fails rather than moving, since that is the port
Google was told about.

> **`Error 400: redirect_uri_mismatch`** means the client is a *Web
> application*, not a *Desktop app*. Only the desktop type accepts a loopback
> redirect on an unregistered port, which is what the helper uses. Create a
> Desktop app client — or register `http://127.0.0.1:8931/` on the web client,
> which is the port the helper uses by default.

> **The 7-day trap.** While the consent screen's publishing status is
> "Testing", Google expires the refresh token after **7 days** and your workflow
> starts failing with `invalid_grant`. Publish the app to stop it.

If no refresh token comes back, Google decided you had already consented —
revoke the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
and run it again.

## Credentials

Three forms, tried in order. All are read from the environment on first call —
importing a toolset reads nothing.

| Env | When |
|---|---|
| `GOOGLE_ACCESS_TOKEN` | A token minted elsewhere: a test, a gateway, the OAuth playground. |
| `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` + `GOOGLE_REFRESH_TOKEN` | The usual choice — a workflow acting as one person. Self-renewing. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` (+ `GOOGLE_IMPERSONATE_SUBJECT`) | Workspace domain-wide delegation. Needs `pip install 'loomflow[google]'`. |

Tokens are cached until just before expiry and refreshed under a lock, so ten
parallel Gmail steps mint one token between them. Gmail and Calendar share the
cache — they authenticate against the same account.

**Scopes.** `gmail.readonly`, `gmail.send`, `gmail.modify`; `calendar.readonly`,
`calendar.events`. Each operation declares what it needs in the manifest, so
`loom certify` and the grant system can see it.

## Two toolsets, not one

`gmail` and `google_calendar` are registered separately so a grant can name one
without the other:

```python
@workflow(grants=GrantSet(toolsets=["google_calendar"]))
async def scheduler(ctx: Context, _in: str) -> str:
    ...   # ctx.agent() here cannot reach Gmail at all
```

## Retries, and the one operation that has none

Google's 4xx responses (bar 429) raise a `NonRetryableError` subclass, so an
ordinary `Retry` policy stops on a malformed query instead of sleeping through
three attempts at it. A 429, a 503, and a 403 whose reason is a quota are
retryable; a 403 whose reason is a missing scope is not.

**`gmail_send_message` and `gmail_reply_to_message` do not retry.** Gmail has no
idempotency key, so a request that times out *after* delivery is
indistinguishable from one that failed — and an automatic retry sends the mail
twice. Journaling already stops a *replay* from re-sending; this is about the
attempt within a single run. A failed send surfaces to the workflow, which can
park on a human:

```python
@workflow(name="send_with_approval")
async def send_with_approval(ctx: Context, message: dict) -> str:
    if await ctx.wait_for_approval("send"):
        await ctx.step(
            gmail_send_message, message["to"], message["subject"], message["body"]
        )
        return "sent"
    return "not sent"
```

Calendar writes do retry once: a duplicate event is visible and deletable, which
a duplicate email is not.

## Things worth knowing

**Search costs one request per hit.** Gmail's list endpoint returns bare ids, so
`gmail_search_messages(q, 100)` is 101 API calls. Keep `max_results` small.

**Messages arrive flattened.** The API returns a recursive MIME tree with
base64url bodies; `EmailMessage` gives you `subject`, `sender`, `to`, `body`
(preferring `text/plain`), and attachment metadata without the bytes. Fetch
those separately with `gmail_get_attachment`, which returns a LOOM `Attachment`
— so with a `BlobService` on the Runtime, a large one offloads out of the
journal automatically.

**Calendar events are expanded by default.** `singleEvents=True`, so a weekly
standup comes back as instances rather than one recurrence rule. An all-day
event has date-only `start`/`end` and `all_day=True`; don't compare it against
an instant without checking that flag.

**Attendees are not emailed by default.** `send_updates` defaults to `"none"` on
create, update, and delete. Pass `"all"` deliberately — a bulk workflow should
not mail a hundred people because of a default.

**Time windows must come from `ctx.now()`.** A workflow body that calls
`datetime.now()` returns different events on replay; the determinism lint flags
it. Use Gmail's own relative operators (`newer_than:1d`) where you can.

## Example

`examples/cookbook/18_gmail_calendar.py` — a morning briefing that reads both
accounts, finds the gaps between meetings, and parks on an approval before
sending anything. Read-only unless run with `--write`.
