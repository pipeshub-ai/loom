# Salesforce and HubSpot toolsets — research, schema, and plan

<!-- docs-illustrative -->

**Status:** research complete, implementation pending. Every API claim below was
read from vendor documentation during this work, not recalled; where a claim
could not be verified it says so rather than guessing.

---

## 0. Why this document exists first

Both of these are larger and stranger than the toolsets already shipped. Jira
has one auth shape and one paging dialect; **Salesforce has a per-org base URL
that is only discoverable at login, tokens that expire mid-workflow, and a
paging scheme that hands back a path rather than a cursor.** HubSpot is more
uniform but hides two hard caps that silently truncate results.

Getting either wrong produces the failure this repository keeps naming: not an
error, a wrong answer that looks right. So the endpoints, the envelopes, and
the limits are written down before any code is.

---

## 1. Salesforce — verified notes

### 1.1 Base URL is per-org and not knowable in advance

Every request goes to `https://{instance}/services/data/v{version}`, where
`{instance}` is the org's own host (`myorg.my.salesforce.com`). The OAuth token
response returns it as **`instance_url`**, and it must be used verbatim —
`login.salesforce.com` is the *authentication* host, not the API host.

This is the first real difference from every toolset here so far: there is no
constant base URL, and a client that hardcodes one works for exactly zero orgs.

### 1.2 Auth: Bearer, plus a refresh the client must own

`Authorization: Bearer {access_token}`.

Access tokens expire. The refresh flow is
`POST {login_host}/services/oauth2/token` with `grant_type=refresh_token`,
`client_id`, `client_secret`, `refresh_token`; the response carries a fresh
`access_token` **and** `instance_url`.

Precedent: the Google toolset already owns refresh, under a lock, caching the
result (`toolsets/google/auth.py`). Salesforce gets the same treatment, because
the alternative is a four-hour workflow that dies at hour two with a 401 that
reads like a permissions problem.

Credentials, in resolution order:

| Variable | Meaning |
|---|---|
| `SALESFORCE_ACCESS_TOKEN` + `SALESFORCE_INSTANCE_URL` | a token already obtained; no refresh possible |
| `SALESFORCE_CLIENT_ID` + `SALESFORCE_CLIENT_SECRET` + `SALESFORCE_REFRESH_TOKEN` | the refreshable form |
| `SALESFORCE_LOGIN_URL` | defaults to `https://login.salesforce.com`; sandboxes use `test.salesforce.com` |

The sandbox host matters: pointing a sandbox refresh token at the production
login host fails with `invalid_grant`, which reads as a bad token rather than a
wrong host.

### 1.3 Query and paging — a path, not a cursor

`GET /services/data/v{version}/query/?q={SOQL}` returns:

```json
{
  "done": false,
  "totalSize": 3214,
  "records": [ { "attributes": {"type": "Account", "url": "..."}, "Id": "001..." } ],
  "nextRecordsUrl": "/services/data/v67.0/query/01gD0000002HU6KIAW-2000"
}
```

**`nextRecordsUrl` is a complete path and takes no query parameters.** That is a
dialect none of the four existing styles express: `TokenPaging` sends a token as
a parameter, `CursorPaging` parses a parameter *out* of a link, `OffsetPaging`
counts rows, `PageNumberPaging` counts pages. Salesforce says "call this exact
path next".

→ **New dialect: `LinkPaging`.** The cursor is the path itself. `done` is the
authoritative end signal and `totalSize` gives an exact total, so coverage here
can be reported honestly rather than inferred from a short page.

### 1.4 CRUD is uniform across every object

| Operation | Method and path |
|---|---|
| create | `POST /sobjects/{Type}` → `{"id": …, "success": true, "errors": []}` |
| retrieve | `GET /sobjects/{Type}/{Id}` |
| update | `PATCH /sobjects/{Type}/{Id}` → 204, empty body |
| delete | `DELETE /sobjects/{Type}/{Id}` → 204 |
| describe | `GET /sobjects/{Type}/describe` |

Because the shape is identical for Account, Contact, Lead, Opportunity, Case,
and every custom object, the toolset exposes **generic CRUD plus typed finders**
rather than five near-identical copies. A `Deal__c` an org invented last week is
then reachable without a library change.

### 1.5 Errors: an array, and a 403 that splits

```json
[ { "message": "…", "errorCode": "REQUEST_LIMIT_EXCEEDED", "fields": ["Name"] } ]
```

| Status | Treatment |
|---|---|
| 400 | permanent — malformed SOQL, bad field |
| 401 | refresh once, then permanent |
| 403 + `REQUEST_LIMIT_EXCEEDED` | **retryable** — org API quota |
| 403 (other) | permanent — the user lacks the permission |
| 404 | permanent |
| 429 | retryable |
| 5xx | retryable |

The 403 split is the same rule the Google toolset already applies to quota
versus scope, and for the same reason: one is a wait, the other is a wiring
error, and blanket-retrying spends three attempts to prove it.

---

## 2. HubSpot — verified notes

### 2.1 Base URL and auth are simple

`https://api.hubapi.com`, and `Authorization: Bearer {token}` for both private
app tokens and OAuth access tokens — identical header, so there is only one
shape to get right. (Contrast ClickUp, where the two differ and the wrong one
returns a bare 401.) Legacy `hapikey` query-parameter keys are out of scope.

### 2.2 Objects are uniform, and versioned two ways

Stable: `/crm/v3/objects/{contacts,companies,deals,tickets}`. HubSpot has begun
publishing **dated** versions (`/crm/objects/2026-03/…`) in its API reference
while the guides still document `v3`.

→ **Target `v3`, and make the segment a constructor argument.** A host that
wants a dated version changes a string rather than waiting for a release. The
alternative — chasing whichever the reference page shows this quarter — makes
the library's correctness depend on a docs migration.

| Operation | Method and path |
|---|---|
| list | `GET /crm/v3/objects/{type}` |
| get | `GET /crm/v3/objects/{type}/{id}` |
| get by email | `GET /crm/v3/objects/contacts/{email}?idProperty=email` |
| create | `POST /crm/v3/objects/{type}` |
| update | `PATCH /crm/v3/objects/{type}/{id}` |
| archive | `DELETE /crm/v3/objects/{type}/{id}` |
| search | `POST /crm/v3/objects/{type}/search` |

### 2.3 Paging: a nested `after` token

```json
{ "results": [ … ], "paging": { "next": { "after": "33452", "link": "…" } } }
```

The token is at **`paging.next.after`** — a nested field. `TokenPaging` reads a
flat one, so it gains an optional tuple form for the field path. That is the
same dialect with a nested address, not a new one, so it is a small extension
rather than a fifth class.

`limit` caps at **100** for list and **200** for search.

### 2.4 Two caps that silently truncate

Both are the reason this needs writing down:

- **Search returns at most 10,000 results per query.** Paging past that returns
  400 — so a naive loop turns a large query into an error at the end rather
  than a short answer.
- **Search is rate limited to 5 requests/second per account**, far tighter than
  the general limit.

Also capped: 5 filter groups × 6 filters (18 total), and a 3,000-character
request body.

→ The client stops at 10,000 and returns `complete=False`. `Results` exists
precisely so "one page reported as a total" cannot happen; a cap that is hit
and not reported would reintroduce it.

### 2.5 Properties are opt-in

A response carries only the properties asked for via `properties=`. Omitting it
returns HubSpot's defaults, which for a contact is roughly email, first name,
last name, and little else — and that reads like a contact with no company and
no lifecycle stage rather than like an under-specified request. Same trap as
Asana's `opt_fields`, handled the same way: a declared default field list per
object type.

### 2.6 Errors

```json
{ "status": "error", "message": "…", "correlationId": "…", "category": "VALIDATION_ERROR" }
```

| Status | Treatment |
|---|---|
| 400, 401, 403, 404, 409 | permanent |
| 429 | retryable, honour `Retry-After` |
| 5xx | retryable |

---

## 3. Schema — the models to build

Flattened at the boundary, as with ClickUp and Asana: a model reasoning about a
deal should see `deal.amount`, not `deal["properties"]["amount"]` as a string.

### 3.1 Salesforce

```python
class SalesforceRecord(BaseModel):
    """Any sObject, flattened. `type` says which."""
    id: str
    type: str            # from attributes.type
    url: str             # attributes.url — the record's REST path
    fields: dict[str, Any]   # everything else, unwrapped from the envelope

class SalesforceAccount(BaseModel):
    id: str; name: str; industry: str; website: str; phone: str
    owner: str; created_date: str

class SalesforceContact(BaseModel):
    id: str; name: str; email: str; phone: str; title: str
    account_id: str; account_name: str

class SalesforceOpportunity(BaseModel):
    id: str; name: str; stage: str; amount: float; close_date: str
    account_id: str; account_name: str; owner: str; is_closed: bool; is_won: bool

class SalesforceUser(BaseModel):
    id: str; name: str; email: str; username: str; is_active: bool

class SalesforceWriteResult(BaseModel):
    id: str; success: bool; errors: list[str]
```

### 3.2 HubSpot

```python
class HubSpotObject(BaseModel):
    """Any CRM object, with properties flattened up one level."""
    id: str
    object_type: str
    properties: dict[str, Any]
    created_at: str; updated_at: str; archived: bool

class HubSpotContact(BaseModel):
    id: str; email: str; first_name: str; last_name: str; full_name: str
    company: str; phone: str; lifecycle_stage: str; owner_id: str
    created_at: str; updated_at: str

class HubSpotCompany(BaseModel):
    id: str; name: str; domain: str; industry: str; city: str
    country: str; owner_id: str

class HubSpotDeal(BaseModel):
    id: str; name: str; stage: str; pipeline: str; amount: float
    close_date: str; owner_id: str; is_closed: bool

class HubSpotOwner(BaseModel):
    id: str; email: str; first_name: str; last_name: str; full_name: str
```

---

## 4. Operations — what "daily use" means

Chosen by what a CRM workflow actually does: find the account, read the deals,
write a note, move a stage. Not by what the API offers.

### 4.1 Salesforce (13)

| Group | Function | Effect | Notes |
|---|---|---|---|
| query | `salesforce_query` | read | raw SOQL, paged |
| query | `salesforce_describe_object` | read | field names for the coding agent |
| records | `salesforce_get_record` | read | any type |
| records | `salesforce_create_record` | write | not retried |
| records | `salesforce_update_record` | write | idempotent, retried once |
| records | `salesforce_delete_record` | destructive | |
| crm | `salesforce_find_accounts` | read | `resolves="account"` |
| crm | `salesforce_find_contacts` | read | `resolves="contact"` |
| crm | `salesforce_find_opportunities` | read | |
| crm | `salesforce_get_account_contacts` | read | the common join |
| crm | `salesforce_get_account_opportunities` | read | |
| people | `salesforce_find_users` | read | `resolves="user"` |
| people | `salesforce_whoami` | read | |

### 4.2 HubSpot (14)

| Group | Function | Effect | Notes |
|---|---|---|---|
| objects | `hubspot_list_objects` | read | any type, paged |
| objects | `hubspot_search_objects` | read | paged, 10k cap reported |
| objects | `hubspot_get_object` | read | |
| objects | `hubspot_create_object` | write | not retried |
| objects | `hubspot_update_object` | write | retried once |
| objects | `hubspot_archive_object` | destructive | |
| contacts | `hubspot_find_contacts` | read | `resolves="contact"` |
| contacts | `hubspot_get_contact_by_email` | read | the `idProperty` route |
| contacts | `hubspot_create_contact` | write | not retried |
| companies | `hubspot_find_companies` | read | `resolves="company"` |
| deals | `hubspot_find_deals` | read | |
| deals | `hubspot_create_deal` | write | not retried |
| associations | `hubspot_get_associations` | read | deals for a contact, etc. |
| owners | `hubspot_list_owners` | read | `resolves="user"` |

Writes with no idempotency key — every create here — get `Retry(max_attempts=1)`,
per the guide: a timeout after the CRM accepted the record is indistinguishable
from a failure, and a retry files a duplicate lead a salesperson then calls.

---

## 5. Plan

| Phase | Work | Exit |
|---|---|---|
| **P1** | `LinkPaging`; nested `token_field` on `TokenPaging` | unit tests for both dialects, existing paging tests still green |
| **P2** | Salesforce: models, client, tools, manifest | contract test passes; auth/refresh and the 403 split unit-tested |
| **P3** | HubSpot: models, client, tools, manifest | same, plus the 10k cap reported as `complete=False` |
| **P4** | Register both; `loom toolsets` / `loom toolset <id>` CLI | both listed by the CLI and by MCP |
| **P5** | Docs: guide, CLAUDE.md, cookbook example | guide snippets execute in CI |

**Testing.** No network anywhere. Transport stubs for the paging loops, pure
translation tests for the models, and the classifiers driven with real error
payloads copied from the docs above. Mutation-verify each guard.

**The main risk.** Salesforce's per-org `instance_url` means the client cannot
be constructed from a constant, and a wrong one fails as 404s that look like
missing records. Construction therefore refuses without either an explicit
instance URL or the refresh credentials that produce one.

---

## Sources

- [Salesforce: Query](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_query.htm)
- [Salesforce: Create a Record](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_sobject_create.htm)
- [Salesforce: Status Codes and Error Responses](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/errorcodes.htm)
- [Salesforce: OAuth 2.0 Refresh Token Flow](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_refresh_token_flow.htm&type=5)
- [HubSpot: Understanding the CRM](https://developers.hubspot.com/docs/guides/api/crm/understanding-the-crm)
- [HubSpot: Contacts](https://developers.hubspot.com/docs/guides/api/crm/objects/contacts)
- [HubSpot: Authentication](https://developers.hubspot.com/docs/guides/apps/authentication/intro-to-auth)
- [HubSpot: Search the CRM](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm)
