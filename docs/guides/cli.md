# The command line

<!-- docs-illustrative -->

`loom` is the whole SDK from a terminal: write a workflow, run it, watch it,
answer the human gate it parks on, and turn a failure into a regression test.
Everything here works the same against an in-process Runtime and a remote
server — pass `--server URL` and nothing else about the command changes.

Installed as both `loom` and `loomsdk`.

```bash
pip install 'loomsdk[cli]'
```

The `[cli]` extra is `rich` (colour, tables) and `prompt_toolkit` (the
interactive session). Without it every command still works in plain text and
`loom` on its own prints help.

## Start here

```bash
loom init my-project && cd my-project
pip install -e '.[dev]'
loom doctor
```

`loom doctor` is the one to run when something is not working, and the one to
run before you conclude it is. It reports where runs are kept and whether that
store can actually be written, which provider key is set, whether the modules
your project declares import, how many workflows registered, and which
integrations are reachable. It exits `1` on anything that would fail later, so
it works in CI too.

```
  ● loom         0.1.0 on python 3.12.10
  ● project      /Users/you/my-project, .env loaded
  ● store        sqlite:///Users/you/my-project/.loom/runs.db  [.loom/runs.db (project default)]
  ● model        claude-sonnet-5 via ANTHROPIC_API_KEY
  ● workflows    1 registered from 1 module(s): quickstart
  ● toolsets     27 reachable from this process
  ● extras       cli, tui, api and mcp all installed
  ready
```

## Where your runs are kept

A project keeps its runs; a scratch directory does not.

| Source | Example |
|---|---|
| `--store` | `loom run x --store postgres://…` |
| `$LOOM_STORE` | `LOOM_STORE=sqlite://runs.db loom run x` |
| `[tool.loom] store` in `pyproject.toml` | `store = "postgres://user@host/loom"` |
| the project default | `.loom/runs.db` beside `pyproject.toml` |
| nothing | `memory://` — only with no project anywhere above you |

Most people never set any of them: a directory with a `pyproject.toml` gets a
SQLite store beside it, which is what makes `loom runs`, `loom watch` and
`loom approve` find the run you started in the previous command.

The last row is the exception rather than a fallback. With no project there is
nowhere the CLI has been invited to write, so runs are kept in memory and lost
when the command exits — and `loom run --detach` says so rather than handing
you an identifier for something already gone.

`.env` is read from the project root, and a real environment variable always
wins over a line in it.

## Writing a workflow

```bash
loom author "watch a folder and summarise new PDFs" -o flows/digest.py
```

The agent searches the toolset and node catalogues, resolves the entities your
spec names against the real services, writes the code, and then verifies it —
sixteen stages ending in a smoke run and a replay. All of that streams as it
happens:

```
  ⏺ search_nodes("pdf")                       0.9s
  ⏺ node_contract("transform.parse_document") 0.4s
  ◐ writing code            turn 6/20 · 24.1k tok · $0.07 · 14s

  ✓ compile  ✓ static  ✓ grants  ! coverage  ✓ resolution  ✓ smoke  ✓ replay
  ↻ repair 1/3 — coverage: spec says "all", code caps the fetch at 50
```

Worth knowing:

- **`-i` gives the verification run a real input.** Without one the harness
  invents a value, and an empty result becomes impossible to judge.
- **`--package NAME`** states what the target environment has. Generated code
  importing anything else is rejected.
- **`--turns N`** raises the discovery budget. A spec naming several systems
  needs more; a run that ends "exceeded its budget" produced nothing and spent
  everything.
- **The agent may ask you a question.** Answer it, or press Enter to take its
  recommendation. `--save-answers answers.json` records them and `--answers`
  replays them, which is what makes an authoring run that asked anything
  repeatable in CI. `--no-ask` never asks.
- **`@spec.txt`** reads the spec from a file. A useful spec outgrows a shell
  argument quickly.

Progress goes to stderr, so `loom author "…" > flow.py` puts the code in the
file.

## Changing one

```bash
loom edit flows/digest.py "also skip PDFs under one page"
```

The same verification runs on the result, so a change that breaks entity
resolution or silently caps a fetch is caught before it is offered. You are
shown the diff and asked before anything is written:

```
  edit flows/digest.py  +1 -1
  --- a/digest.py
  +++ b/digest.py
  -    if pdf.page_count == 0:
  +    if pdf.page_count <= 1:
  graph: unchanged

  Apply this?
    1. Yes
    2. Yes, and don't ask again for digest.py
    3. No
```

`--dry-run` shows it and writes nothing. `--yes` writes without asking, which
is what a script wants — and is required there, because a confirmation that
cannot be given is refused rather than assumed.

"Unchanged" is a valid answer: the instructions tell the model to decline
rather than guess, so the file you have is the one that still works.

## Running

```bash
loom run digest                      # by name, from [tool.loom] modules
loom run flows/digest.py::digest     # by path — always works
loom run digest -i '{"folder": "~/in"}'
loom run digest -i @payload.json
loom run digest -i "just a string"
loom run digest --follow             # stream steps as they complete
loom run digest --detach             # start it and return
```

Omitting `-i` uses the workflow's own declared default, if it has one.

`--follow` streams: each step appears as it completes, along with anything the
run narrated through `ctx.report`.

### Exit codes are the contract

| Code | Meaning |
|---|---|
| `0` | completed |
| `1` | failed |
| `2` | usage — bad arguments, unknown workflow, refused input |
| `3` | **suspended** — parked on a timer or a person |
| `4` | cancelled |
| `130` / `143` | interrupted (Ctrl+C / SIGTERM) |

`3` is the one that matters. A run waiting three weeks on an approval has
neither succeeded nor failed, and collapsing it into either makes calling
scripts do the wrong thing. It also covers giving up: `loom watch --timeout 60`
on a run still going exits `3`, because that command reported nothing about
whether the run succeeds.

A suspended run prints the command that unparks it.

## Looking at runs

```bash
loom runs                            # everything, newest first
loom runs --status failed
loom runs --workflow digest -n 20
loom show <run>                      # the run and its journal
loom watch <run>                     # follow one until it settles
loom artifacts                       # what runs produced
```

## Runs waiting on a person

```bash
loom pending                         # every parked run, across workflows
loom approve <run> refund            # or --reject
loom respond <run> choose '{"pick": "b"}'
loom send <run> webhook '{"token": "x"}'
```

`loom pending` is the one that turns a parked run into a queue item rather than
a mystery — otherwise finding one means already knowing it exists. Its
`delivered` column is worth reading: `no` means the request was journaled and
nobody was notified, which leaves a run looking patient.

## Acting on a run

```bash
loom pause <run>                     # hold at the next durable step
loom unpause <run>
loom cancel <run>                    # terminal; unwinds compensations
loom retry <run>                     # from the failure, against current code
loom replay <run>                    # from the journal, repeating nothing
loom pin <run> -o tests/test_regression.py
```

`retry` and `replay` answer different questions. `retry` prunes the failure and
does the work again; `replay` rehearses what happened and must reproduce it.

`pin` turns a run into a pytest file built from its own journal, so a
production failure becomes a committed test that fails for the same reason and
passes when it is fixed.

## Committing the graph

```bash
loom check flows/digest.py                # writes .graph.json and .description.md
loom check flows/digest.py --fail-on-change   # CI: fail if the committed graph is stale
loom graph flows/digest.py --format mermaid
loom describe flows/digest.py
```

Both artifacts are meant to be committed. The graph makes structural changes
reviewable in a diff; the description makes them readable by someone who does
not read Python.

## The catalogue

```bash
loom toolsets                        # integrations this process can reach
loom toolset jira                    # operations, effects, and the import line
loom nodes --category human
loom node human.approval             # the code to write to call it
```

These read manifests only, so listing costs no vendor imports and no
credentials.

## Credentials

```bash
loom providers                       # OAuth providers with endpoints built in
loom connect gmail
loom whoami                          # what is stored, and whether it is ok/due/expired
loom refresh                         # every stored credential; exit 1 if any failed
loom disconnect gmail
```

What `loom connect` stores is what a workflow's toolsets read, so a credential
you connect here is one your runs can use.

## The session

`loom` with no subcommand, at a terminal, opens an interactive session.

```
  ✻ Loom  my-project  sqlite://.loom/runs.db  2 parked

> watch a folder and summarise new PDFs
  … writes flows/watch_folder_summarise.py, and works on it from here

> also skip PDFs under one page
  … edits that file

> /run watch_folder_summarise
> /pending
```

Type what you want. With a file in focus that changes it; with none it writes a
new one. `/open flows/other.py` moves focus, `/new` drops it, and `/author` and
`/edit` say which explicitly.

Every slash command is the subcommand — `/run digest -i @x.json` is
`loom run digest -i @x.json`, down to the exit code — so there is nothing to
learn twice. `/help` lists them. `/command`, `#workflow` and `@file` complete
on Tab.

Piped, redirected, or in CI, `loom` prints help and exits `0` exactly as it
always has. The session is for a person at a keyboard.

## Serving

```bash
loom serve --port 8000               # HTTP API, needs [api]
loom mcp                             # MCP, needs [mcp]
loom ui                              # terminal UI, needs [tui]
loom workflows
loom publish digest
```

`loom serve` refuses to bind a non-loopback address with no identity
configured, because that serves every workflow to anyone who can reach the
port.

## Using loom from an AI coding tool

```bash
loom setup claude-code               # writes .mcp.json at the project root
loom setup all                       # + claude-desktop, cursor, codex
loom setup cursor --dry-run          # print the config without writing it
```

Each writes the client's own config file, reading it back first so other
servers already listed there survive.

`claude` is kept as an alias for `claude-desktop`, which is what it has always
configured. Claude Code is `claude-code`.

## Output for programs

Every command takes `--json`, so output pipes into `jq`:

```bash
loom runs --status failed --json | jq -r '.[].run_id'
loom show "$RUN" --json | jq .output
loom doctor --json | jq -e .ok
```

`--quiet` suppresses human output entirely, leaving the exit code and errors.
`--debug` prints the traceback when something fails unexpectedly.

Human output uses `rich` when stdout is a terminal and strips styling when it
is not, so redirecting to a file captures text rather than escape codes.

## Interrupting

Ctrl+C and `docker stop` behave the same: the command is cancelled, its cleanup
runs, and the Runtime settles the lease on whatever it was driving — so an
interrupted run is picked up by the next orphan recovery instead of being
stranded. The command says which runs survived and how to find them. A second
signal re-raises, because a cleanup path that cannot itself be interrupted is a
hang with extra steps.
