# Phase 11 — Testing Infrastructure & Developer Experience

**Goal:** Comprehensive test infrastructure (property-based tests, fuzzing, load tests, chaos tests), developer experience tooling (interactive playground, getting-started guides, API reference generation, error diagnostics), and CI/CD pipeline that gates all 10 prior phases.

**Prerequisites:** Phase 1-3 (core SDK features), Phase 7 (eval framework). Benefits from all other phases being complete.

**System Design References:** Chapters 10 (observability), 14 (cross-phase test suite), 15 (extension points).

---

## 1. Exit Criteria & Success Metrics

| Metric | Gate | Target |
|--------|------|--------|
| Code coverage (line) | >= 80% | >= 90% |
| Code coverage (branch) | >= 70% | >= 85% |
| Property-based tests for core engine | >= 20 properties | >= 40 properties |
| Time to "Hello World" workflow | <= 5 min | <= 2 min |
| API reference covers all public symbols | 100% | 100% |
| CI pipeline runs in | <= 15 min | <= 8 min |
| Error messages include fix suggestions | >= 50% | >= 80% |

**"Done" means:** A new developer can go from `pip install workflow-builder` to running their first workflow in under 5 minutes, guided by interactive examples. The test suite includes property-based tests that catch edge cases unit tests miss. CI catches regressions across all phases. Error messages tell you what went wrong and how to fix it.

---

## 2. HLD — Testing & DX Architecture

```
+------------------------------- Phase 11 Scope --------------------------------+
|                                                                                 |
|  Testing Infrastructure                                                         |
|  +----------------------------------------------------------------------+      |
|  | Property-Based Tests  | Fuzzing        | Load Tests   | Chaos Tests  |      |
|  | (Hypothesis)          | (journal input | (concurrent  | (kill mid-   |      |
|  |                       |  mutation)     |  workflows)  |  step, OOM)  |      |
|  +----------------------------------------------------------------------+      |
|  | Store Conformance Suite (parameterized across Memory/SQLite/Postgres/Mongo) |
|  +----------------------------------------------------------------------+      |
|  | Cross-Phase Integration Tests (Phases 1-10 combined)                       |
|  +----------------------------------------------------------------------+      |
|  | CI/CD Pipeline (GitHub Actions)                                             |
|  |  lint → type-check → unit → integration → e2e → eval → publish             |
|  +----------------------------------------------------------------------+      |
|                                                                                 |
|  Developer Experience                                                           |
|  +----------------------------------------------------------------------+      |
|  | Interactive Playground    | Getting Started Guides | Cookbooks       |      |
|  | (loom playground / REPL) | (5-minute quickstart)  | (patterns +     |      |
|  |                          |                         |  recipes)       |      |
|  +----------------------------------------------------------------------+      |
|  | API Reference Generator  | Error Diagnostics      | Migration Guide |      |
|  | (from docstrings + types)| (actionable error msgs)| (upgrade paths) |      |
|  +----------------------------------------------------------------------+      |
|  | Starter Templates        | VS Code Snippets       | Type Stubs      |      |
|  | (cookiecutter / copier)  | (@workflow, @step)     | (py.typed)      |      |
|  +----------------------------------------------------------------------+      |
+---------------------------------------------------------------------------------+
```

---

## 3. LLD — Subsystem Details

### 3.1 Property-Based Testing (Hypothesis)

Test invariants that must hold for any valid input, not just hand-picked cases.

```python
# tests/property/test_journal_properties.py (NEW)

from hypothesis import given, strategies as st, assume, settings
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant
from workflow_builder.runtime.journal import Journal, JournalEntry, EntryKind

class JournalStateMachine(RuleBasedStateMachine):
    """Property-based state machine test for Journal."""

    def __init__(self):
        super().__init__()
        self.journal = Journal()
        self.expected_entries = []

    @rule(
        step_id=st.text(min_size=1, max_size=50),
        output=st.one_of(st.integers(), st.text(), st.none()),
    )
    def append_step_entry(self, step_id, output):
        """Appending a step entry should always succeed and be retrievable."""
        entry = JournalEntry(
            kind=EntryKind.STEP,
            step_id=step_id,
            output=output,
            status="completed",
        )
        self.journal.append(entry)
        self.expected_entries.append(entry)

    @invariant()
    def entries_are_ordered(self):
        """Journal entries must be in append order."""
        assert len(self.journal.entries) == len(self.expected_entries)
        for actual, expected in zip(self.journal.entries, self.expected_entries):
            assert actual.step_id == expected.step_id

    @invariant()
    def lookup_returns_latest(self):
        """Looking up a step_id returns the latest entry for that id."""
        seen = {}
        for entry in self.expected_entries:
            seen[entry.step_id] = entry
        for step_id, expected in seen.items():
            result = self.journal.lookup(step_id)
            assert result is not None
            assert result.output == expected.output


@given(
    steps=st.lists(st.tuples(st.text(min_size=1, max_size=20), st.integers()), min_size=1, max_size=50),
)
def test_journal_replay_produces_same_outputs(steps):
    """After recording N steps, replaying should produce the same outputs in order."""
    journal = Journal()
    for step_id, output in steps:
        journal.append(JournalEntry(
            kind=EntryKind.STEP, step_id=step_id, output=output, status="completed",
        ))

    for step_id, output in steps:
        entry = journal.lookup(step_id)
        # At least one entry should exist for each step_id
        assert entry is not None


@given(
    n=st.integers(min_value=1, max_value=100),
)
def test_journal_truncate_preserves_prefix(n):
    """Truncating to N entries keeps the first N entries intact."""
    journal = Journal()
    for i in range(n + 10):
        journal.append(JournalEntry(
            kind=EntryKind.STEP, step_id=f"step_{i}", output=i, status="completed",
        ))

    journal.truncate(n)
    assert len(journal.entries) == n
    for i in range(n):
        assert journal.entries[i].step_id == f"step_{i}"
```

### 3.2 Chaos Testing

Simulate crashes, slow stores, and resource exhaustion.

```python
# tests/chaos/test_crash_recovery.py (NEW)

import asyncio
import pytest
from workflow_builder import workflow, step, Context
from workflow_builder.runtime.engine import Runtime
from workflow_builder.state.memory import MemoryStore

class CrashingStore(MemoryStore):
    """Store that crashes after N operations for chaos testing."""

    def __init__(self, crash_after: int = 5):
        super().__init__()
        self._op_count = 0
        self._crash_after = crash_after

    async def save_journal(self, run_id, journal):
        self._op_count += 1
        if self._op_count == self._crash_after:
            raise ConnectionError("Simulated store crash")
        return await super().save_journal(run_id, journal)

class SlowStore(MemoryStore):
    """Store that adds latency to simulate slow I/O."""

    def __init__(self, latency_ms: int = 100):
        super().__init__()
        self._latency = latency_ms / 1000

    async def save_journal(self, run_id, journal):
        await asyncio.sleep(self._latency)
        return await super().save_journal(run_id, journal)


@pytest.mark.chaos
async def test_crash_mid_step_recovers():
    """Workflow should recover after store crash mid-step."""
    call_count = 0

    @step
    async def counted_step(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    @workflow
    async def crash_workflow(ctx: Context, n: int) -> list[int]:
        results = []
        for i in range(n):
            result = await ctx.step(counted_step, i)
            results.append(result)
        return results

    store = CrashingStore(crash_after=3)
    runtime = Runtime(store=store)

    # First run: crashes mid-way
    with pytest.raises(ConnectionError):
        await runtime.run("crash_workflow", 10)

    # Resume with a working store — should replay completed steps
    store._crash_after = 999  # disable crash
    result = await runtime.resume(store._last_run_id)

    assert result.status.value == "completed"
    assert len(result.output) == 10


@pytest.mark.chaos
async def test_concurrent_workflows_no_corruption():
    """Running many workflows concurrently should not corrupt shared state."""
    @step
    async def identity(x: int) -> int:
        return x

    @workflow
    async def simple(ctx: Context, x: int) -> int:
        return await ctx.step(identity, x)

    store = MemoryStore()
    runtime = Runtime(store=store)

    tasks = [runtime.run("simple", i) for i in range(50)]
    results = await asyncio.gather(*tasks)

    # Each workflow should get its own result
    outputs = sorted(r.output for r in results)
    assert outputs == list(range(50))
```

### 3.3 Actionable Error Messages

Enhance exceptions with fix suggestions and documentation links.

```python
# core/diagnostics.py (NEW)

from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Diagnostic:
    """An actionable error diagnostic with fix suggestion."""
    code: str               # e.g., "LOOM-D001"
    message: str            # what went wrong
    fix: str                # how to fix it
    location: str = ""      # file:line if available
    docs_url: str = ""      # link to relevant docs

DIAGNOSTICS: dict[str, Diagnostic] = {
    "LOOM-D001": Diagnostic(
        code="LOOM-D001",
        message="datetime.now() called in workflow body — nondeterministic on replay",
        fix="Replace with `ctx.now()` which is journaled and deterministic on replay.",
    ),
    "LOOM-D002": Diagnostic(
        code="LOOM-D002",
        message="uuid.uuid4() called in workflow body — nondeterministic on replay",
        fix="Replace with `ctx.uuid4()` which is journaled and deterministic on replay.",
    ),
    "LOOM-D003": Diagnostic(
        code="LOOM-D003",
        message="random.* called in workflow body — nondeterministic on replay",
        fix="Replace with `ctx.random()` which is journaled and deterministic on replay.",
    ),
    "LOOM-D004": Diagnostic(
        code="LOOM-D004",
        message="Direct I/O call in workflow body — not journaled, will double-execute on replay",
        fix="Move the I/O call into a @step function and call it via `await ctx.step(fn, args)`.",
    ),
    "LOOM-D005": Diagnostic(
        code="LOOM-D005",
        message="Step function is not async — @step functions must be async",
        fix="Add `async` keyword: `async def my_step(...)`",
    ),
    "LOOM-E001": Diagnostic(
        code="LOOM-E001",
        message="Store connection failed",
        fix="Check your store URL in LOOM_STORE_URL or the `store_url` parameter. "
            "For SQLite: 'sqlite:///path/to/db'. For PostgreSQL: 'postgresql://...'",
    ),
    "LOOM-E002": Diagnostic(
        code="LOOM-E002",
        message="Workflow not found in registry",
        fix="Ensure the workflow is decorated with @workflow and the module is imported. "
            "Run `loom list` to see registered workflows.",
    ),
    "LOOM-E003": Diagnostic(
        code="LOOM-E003",
        message="Agent model provider returned error",
        fix="Check your API key (OPENAI_API_KEY, ANTHROPIC_API_KEY) and model name. "
            "Run `loom check` to validate configuration.",
    ),
    "LOOM-A001": Diagnostic(
        code="LOOM-A001",
        message="Agent budget exceeded (tokens or cost limit reached)",
        fix="Increase the budget in AgentLimits or reduce the task complexity. "
            "Current usage is logged at DEBUG level.",
    ),
}

def format_error(code: str, **context) -> str:
    """Format an error with diagnostic information."""
    diag = DIAGNOSTICS.get(code)
    if not diag:
        return f"[{code}] Unknown error code"

    parts = [
        f"\n{'='*60}",
        f"  {code}: {diag.message}",
        f"{'='*60}",
        f"\n  How to fix:",
        f"    {diag.fix}",
    ]
    if diag.location or context.get("location"):
        parts.append(f"\n  Location: {context.get('location', diag.location)}")
    if diag.docs_url:
        parts.append(f"\n  Docs: {diag.docs_url}")
    parts.append("")
    return "\n".join(parts)
```

### 3.4 Interactive Playground

A REPL-like environment for experimenting with workflows.

```python
# cli/playground.py (NEW)

"""loom playground — interactive workflow experimentation."""

from __future__ import annotations
import asyncio
import sys
from workflow_builder.runtime.engine import Runtime
from workflow_builder.state.memory import MemoryStore

BANNER = """
╔══════════════════════════════════════════════════════╗
║           LOOM Workflow Playground                   ║
║  Type workflow code, press Enter twice to run.       ║
║  Commands: /run, /status, /journal, /help, /quit     ║
╚══════════════════════════════════════════════════════╝
"""

class Playground:
    """Interactive playground for testing workflows."""

    def __init__(self):
        self._runtime = Runtime(store=MemoryStore())
        self._history: list[str] = []

    async def run_code(self, code: str) -> str:
        """Execute workflow code in the playground."""
        # Execute the code to register decorators
        namespace = {}
        exec(code, namespace)

        # Find the workflow function
        from workflow_builder.runtime.workflow import WorkflowDefinition
        workflows = {
            k: v for k, v in namespace.items()
            if isinstance(v, WorkflowDefinition)
        }

        if not workflows:
            return "No @workflow found in the code. Add @workflow decorator to your function."

        # Run the first workflow found
        name, wf = next(iter(workflows.items()))
        result = await self._runtime.run(name, {})
        return f"Run complete.\nStatus: {result.status}\nOutput: {result.output}"

    async def interactive(self):
        """Run interactive REPL loop."""
        print(BANNER)
        buffer = []

        while True:
            try:
                line = input("loom> " if not buffer else "...> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if line.startswith("/"):
                await self._handle_command(line)
                continue

            if line == "" and buffer:
                code = "\n".join(buffer)
                buffer.clear()
                try:
                    result = await self.run_code(code)
                    print(result)
                except Exception as e:
                    print(f"Error: {e}")
            else:
                buffer.append(line)

    async def _handle_command(self, cmd: str):
        parts = cmd.split()
        if parts[0] == "/quit":
            sys.exit(0)
        elif parts[0] == "/help":
            print("Commands: /run <file>, /status <run_id>, /journal <run_id>, /quit")
        elif parts[0] == "/run" and len(parts) > 1:
            with open(parts[1]) as f:
                result = await self.run_code(f.read())
            print(result)
        else:
            print(f"Unknown command: {cmd}")
```

### 3.5 Getting Started Template

```python
# cli/init_template.py (NEW)

"""loom init — scaffold a new workflow project."""

QUICKSTART_TEMPLATE = '''"""My first LOOM workflow."""

from workflow_builder import workflow, step, Context, Retry

# Steps do the actual work — API calls, I/O, computation
@step(retry=Retry(max_attempts=3))
async def fetch_data(url: str) -> dict:
    """Fetch data from an external API."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

@step
async def process_data(data: dict) -> str:
    """Process the fetched data."""
    return f"Processed {len(data)} fields"

# The workflow orchestrates steps — must be deterministic
@workflow
async def my_workflow(ctx: Context, url: str) -> str:
    """Fetch data and process it."""
    data = await ctx.step(fetch_data, url)
    result = await ctx.step(process_data, data)
    return result

# Run with: loom run my_workflow --input '{"url": "https://api.example.com/data"}'
'''

def scaffold_project(directory: str):
    """Create a new LOOM workflow project."""
    import os
    os.makedirs(directory, exist_ok=True)
    os.makedirs(f"{directory}/workflows", exist_ok=True)
    os.makedirs(f"{directory}/tests", exist_ok=True)

    with open(f"{directory}/workflows/my_workflow.py", "w") as f:
        f.write(QUICKSTART_TEMPLATE)

    with open(f"{directory}/tests/test_my_workflow.py", "w") as f:
        f.write('''"""Tests for my_workflow."""
import pytest
from workflows.my_workflow import my_workflow, fetch_data, process_data
from workflow_builder import Runtime
from workflow_builder.state.memory import MemoryStore

@pytest.mark.asyncio
async def test_workflow_runs():
    runtime = Runtime(store=MemoryStore())
    result = await runtime.run("my_workflow", {"url": "https://httpbin.org/json"})
    assert result.status.value == "completed"
''')

    with open(f"{directory}/pyproject.toml", "w") as f:
        f.write('''[project]
name = "my-loom-project"
version = "0.1.0"
dependencies = ["workflow-builder"]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]
''')

    print(f"Project scaffolded at {directory}/")
    print(f"  workflows/my_workflow.py — your first workflow")
    print(f"  tests/test_my_workflow.py — tests")
    print(f"\nNext steps:")
    print(f"  cd {directory}")
    print(f"  pip install -e '.[dev]'")
    print(f"  pytest")
```

### 3.6 CI Pipeline Configuration

```yaml
# .github/workflows/ci.yml (NEW)

name: LOOM CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  PYTHON_VERSION: "3.11"

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: ruff check src tests
      - run: ruff format --check src tests

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: mypy

  test-unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: pytest tests/unit/ -v --tb=short
      - run: pytest tests/property/ -v --tb=short

  test-integration:
    runs-on: ubuntu-latest
    needs: test-unit
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: pytest tests/integration/ -v --tb=short

  test-e2e:
    runs-on: ubuntu-latest
    needs: test-integration
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev,api]"
      - run: pytest tests/e2e/ -v --tb=short

  test-stores:
    runs-on: ubuntu-latest
    needs: test-unit
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: test
          POSTGRES_DB: loom_test
        ports: ["5432:5432"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: pytest tests/integration/test_store_conformance.py -v
        env:
          LOOM_TEST_POSTGRES_URL: "postgresql://postgres:test@localhost:5432/loom_test"

  coverage:
    runs-on: ubuntu-latest
    needs: [test-unit, test-integration, test-e2e]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - run: pip install -e ".[dev]"
      - run: pytest --cov=workflow_builder --cov-report=xml tests/
      - uses: codecov/codecov-action@v4
```

---

## 4. Directory Structure

```
src/workflow_builder/
├── core/
│   └── diagnostics.py          # NEW: Actionable error messages
├── cli/
│   ├── playground.py            # NEW: Interactive playground
│   └── init_template.py         # NEW: loom init scaffolding
tests/
├── unit/                        # Existing (extended)
├── integration/                 # Existing (extended)
├── e2e/                         # Existing (extended)
├── property/                    # NEW: Hypothesis property-based tests
│   ├── test_journal_properties.py
│   ├── test_context_properties.py
│   ├── test_store_properties.py
│   └── test_retry_properties.py
├── chaos/                       # NEW: Chaos/fault injection tests
│   ├── test_crash_recovery.py
│   ├── test_slow_store.py
│   └── test_concurrent_workflows.py
├── load/                        # NEW: Load/stress tests
│   ├── test_throughput.py
│   └── test_memory_pressure.py
├── conftest.py                  # Shared fixtures
└── fixtures/                    # Shared test data
    ├── sample_workflows.py
    └── mock_responses.py
.github/
└── workflows/
    └── ci.yml                   # NEW: CI pipeline
docs/
├── quickstart.md                # NEW: 5-minute getting started
├── cookbook.md                   # NEW: Common patterns & recipes
└── migration.md                 # NEW: Version upgrade guide
```

---

## 5. Implementation Steps

| Step | Task | Depends On |
|------|------|------------|
| 11.1 | Implement `core/diagnostics.py` with error codes LOOM-D001 through LOOM-E003 | — |
| 11.2 | Wire diagnostics into existing exceptions (NondeterminismError, etc.) | 11.1 |
| 11.3 | Write property-based tests for Journal | Phase 1 |
| 11.4 | Write property-based tests for Context and Store | Phase 1 |
| 11.5 | Implement chaos test fixtures (CrashingStore, SlowStore) | Phase 1 |
| 11.6 | Write crash recovery and concurrency chaos tests | 11.5 |
| 11.7 | Implement `loom playground` interactive REPL | Phase 1 |
| 11.8 | Implement `loom init` project scaffolding | — |
| 11.9 | Write getting-started guide (quickstart.md) | 11.8 |
| 11.10 | Write cookbook with 10+ common patterns | Phase 8 reference workflows |
| 11.11 | Set up CI pipeline (GitHub Actions) | 11.3-11.6 |
| 11.12 | Add coverage reporting and quality gates | 11.11 |
| 11.13 | Write load tests for throughput benchmarking | Phase 5 stores |

---

## 6. Data Flow Diagram

### CI Pipeline Flow

```mermaid
flowchart TD
    A[Push / PR] --> B[Lint + Format Check]
    A --> C[Type Check mypy]
    B --> D{Pass?}
    C --> D
    D -->|No| E[Fail — block merge]
    D -->|Yes| F[Unit Tests]
    F --> G[Property Tests Hypothesis]
    F --> H[Integration Tests]
    G --> I{All pass?}
    H --> I
    I -->|No| E
    I -->|Yes| J[E2E Tests]
    J --> K[Store Conformance Memory+SQLite+Postgres]
    K --> L{All pass?}
    L -->|No| E
    L -->|Yes| M[Coverage Report]
    M --> N{>= 80%?}
    N -->|No| E
    N -->|Yes| O[Green — allow merge]
```

---

## 7. Multi-Angle Review

### Correctness
- Property-based tests catch edge cases that hand-written tests miss (empty inputs, unicode, large values).
- Chaos tests verify the core invariant: no duplicate effects after crash recovery.
- Store conformance suite ensures all backends behave identically.

### Security
- Playground executes user code — must run in a sandboxed environment, not in production.
- CI secrets (API keys for live tests) stored in GitHub Secrets, never logged.
- Scaffolded projects don't include hardcoded credentials.

### Performance
- Load tests establish baselines for throughput (steps/second, concurrent workflows).
- Slow store chaos tests verify graceful degradation under latency.
- CI pipeline parallelizes independent jobs (lint + typecheck + unit tests).

### User Perspective
- 5-minute quickstart gets users from zero to running workflow.
- Actionable error messages reduce debugging time.
- Playground lets users experiment without committing to a project structure.
- Cookbook provides copy-pasteable patterns for common use cases.

---

## 8. Test Plan

### Property Tests (20+ properties)
| Property | What |
|----------|------|
| Journal append order preserved | Entries always in append order |
| Journal lookup returns latest | Multiple entries for same step_id → latest wins |
| Journal truncate preserves prefix | First N entries unchanged |
| Context step is idempotent on replay | Same step_id → same output |
| Store create/get roundtrip | Create then get returns same record |
| Store list respects filters | Status and workflow_id filters work |
| Retry backoff is monotonic | Each attempt waits >= previous |
| Serialization roundtrip | encode(decode(x)) == x for all JSON-serializable x |

### Chaos Tests (5)
| Test | What |
|------|------|
| Crash mid-step → resume | No duplicate effects |
| Slow store → workflow completes | Graceful degradation |
| 50 concurrent workflows | No state corruption |
| OOM during step → retry | Step retry handles resource exhaustion |
| Store unavailable → suspend | Workflow parks, resumes when store returns |

### Load Tests (3)
| Test | What |
|------|------|
| 1000 sequential steps | Throughput baseline |
| 100 concurrent workflows | Concurrency scaling |
| 10000 journal entries | Store performance at scale |

---

## 9. Known Gaps & Mitigations

| Gap | Risk | Mitigation |
|-----|------|------------|
| Playground executes arbitrary code | Security risk | Warn users; playground is dev-only, not for production |
| Property tests are slow | CI takes longer | Run property tests with reduced max_examples in CI, full suite nightly |
| Load tests need consistent hardware | Results vary across CI runners | Benchmark against relative thresholds, not absolute |
| Coverage % can be gamed | High coverage != good tests | Pair coverage with mutation testing (future) |
| Getting-started guide may drift | Docs out of sync with code | Quickstart includes runnable test; CI fails if it breaks |
