# Connecting an account

A toolset is code plus a credential. LOOM ships the code; this is how the
credential gets there, and how you find out what is missing before a workflow
fails on its first real call.

## Is it connected?

```bash
loom connections            # every integration, and what the rest need
loom connections jira       # one of them
loom connections --missing  # only what is not usable
```

```
toolset     state      auth     needs                    how
jira        connected  oauth2   -                        -
slack       missing    oauth2   SLACK_BOT_TOKEN          loom connect slack
stripe      missing    bearer   STRIPE_API_KEY           -
duckduckgo  none       none     -                        -
```

`loom doctor` reports the same thing as one line, because "27 toolsets
reachable" and "3 credentials stored" on adjacent lines never answered
*"is Jira usable here?"*.

Six states, and the four in the middle exist because they lead somewhere
different:

| state | meaning |
|---|---|
| `connected` | a stored credential, not near expiry |
| `due` | stored and usable; the next call will renew it. Seen repeatedly, renewal is failing — hours before it becomes `expired` |
| `expired` | stored and past expiry. `loom refresh <name>` may still rescue it |
| `env` | no stored credential, but the environment satisfies what the client reads. Nothing to renew, nothing to disconnect |
| `missing` | neither |
| `none` | this toolset needs no credential |

Reading a status **changes nothing**: it peeks at the store rather than
resolving through it, so it can neither renew a credential nor raise on an
expired one. That is what makes it safe to call while building a prompt.

## Connecting one

```bash
loom connect jira --client-id <id> --client-secret <secret>
```

The name is the **credential**, not the provider. Jira is served by
`atlassian`, Gmail by `google_gmail`, and all six Microsoft Graph toolsets by
`microsoft` — thirteen of the seventeen OAuth toolsets do not share their
provider's name, which is why the manifest declares it rather than anything
guessing:

```python
from loom.toolsets.manifest import AuthField, AuthSpec

auth = AuthSpec(
    kind="oauth2",
    credential="jira",        # the CredentialStore key the client reads
    provider="atlassian",     # the OAuth provider that issues it
    scopes=("offline_access",),
    fields=(AuthField(name="JIRA_URL", secret=False),),
    setup_url="https://developer.atlassian.com/console/myapps/",
)
print(auth.provider, auth.field_names)
```

`loom connect` prints the **redirect URI before it asks for anything**, because
registering it on the provider is the one step nobody can do for you and
finding out afterwards means doing the whole flow again:

```
  Redirect URI: http://127.0.0.1:8931/callback
  Requesting scopes: read:jira-work write:jira-work offline_access
  Opening your browser to continue.
```

Scopes come from the toolsets that read the credential, not from the
provider's defaults — narrower, and the union across the five Google toolsets
that share one token, because a token minted for Calendar alone is a 403 the
first time a Drive call is made.

## One credential, several toolsets

Five Google toolsets read `google`; six Graph toolsets read `microsoft`.
Connecting once serves the set. `loom connections` shows every toolset that
credential covers.

## Toolsets with no store path

Twelve of the twenty-seven read environment variables and no `CredentialStore`
at all — Stripe, Airtable, GitLab, GitHub and the rest. `loom connections`
reports them as `missing` and prints the variables rather than a
`loom connect` line, because connecting one would store a value the client
never looks up. Put them in the project's `.env`:

```
STRIPE_API_KEY=sk_live_…
```

Several accept **alternatives**, and any one complete set is enough — Google
takes an access token *or* a client-id/secret/refresh trio *or* a service
account file. `loom connections` names the nearest one you have not finished,
rather than the union of all of them.

## While a workflow is being written

The coding agent resolves entities against the real service before it writes
code — that is rung 2 of the resolution ladder, and it needs a credential. It
gets the one `loom connect` stored.

When there is none, a lookup comes back as a **state, not an error**:

```json
{
  "error": "not_connected",
  "toolset": "jira",
  "needs": {"method": "oauth2", "credential": "jira", "provider": "atlassian"},
  "next": "connect_toolset(\"jira\")",
  "note": "The machine is not configured; the operation and your code are fine …"
}
```

That distinction is load-bearing. Reported as a failure it reads as *"this
toolset is broken"*, and the cheapest repair a model can find for a broken
toolset is to stop importing it — so the request comes back having quietly
dropped the integration it was about, with every remaining stage green.

`connect_toolset` is offered only when the surface composed something that can
actually connect (`loom` does; `loom mcp` and CI do not), bounded to two
attempts, and switched off before repair and smoke so a model cannot deadlock
CI by opening a browser nobody is sitting in front of.

The `connections` verification stage warns — never errors — when a finished
workflow imports a toolset this machine has no credential for. The code is
correct; the machine is not configured, and an error there would ask the repair
loop to fix a machine.

## Embedding it

`LocalFacade` takes the two adapters, and both default to `None`:

```python
from loom.connectors.flows import ConsoleSecretPrompt, OAuthBrowserFlow
from loom.facade import LocalFacade
from loom.runtime.engine import Runtime
from loom.stores import MemoryStore

facade = LocalFacade(
    Runtime(store=MemoryStore()),
    connect_flow=OAuthBrowserFlow(),
    secret_prompt=ConsoleSecretPrompt(),
)
print(facade.connect_flow is not None)
```

A host that composes neither gets no browser and no terminal prompt: importing
`loom.connectors.flows` opens nothing and reads nothing. A library that reads
stdin because it was imported is the ambient behaviour `Runtime` avoids
everywhere else — the CLI opts in, a server does not.

Writing your own flow? `loom.testing.conformance.verify_connect_flow` asserts
what an author is least likely to test: an outcome that never carries a token,
a listener that cannot fail a connection, and a refusal that says why.
