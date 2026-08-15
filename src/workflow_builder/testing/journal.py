"""Test a workflow by seeding what already happened.

To test what a workflow does *after* an expensive step — an agent call, an LLM
step, a paid API — you have to stop that step from running. The usual answers
are a fake or a monkeypatch, both of which test the workflow against a stand-in
for the step rather than against the step's recorded result.

The replay engine already has the right seam. A journal entry *is* the
statement "this step returned that", and the engine already prefers a recorded
entry to running anything. So the test says what happened, and the workflow
runs against it:

    result = await run_with(
        onboard,
        {"email": "a@b.com"},
        given(research, returns={"summary": "canned"}),
        given(send_email, raises=TimeoutError("smtp down")),
    )

No mocking framework, no dependency injection, and nothing to keep in sync —
what a seeded entry means is exactly what a recorded one means.

The keys are positional, which is the same constraint replay itself lives
under: ``given()`` binds to the *nth* call of that step in the body. See
:func:`given` for what that does and does not survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workflow_builder.core.models import ErrorInfo, ExecutionResult
from workflow_builder.core.serde import encode
from workflow_builder.runtime.journal import EntryKind, EntryStatus, JournalEntry

__all__ = ["Given", "assert_replays", "given", "run_with"]


@dataclass(frozen=True)
class Given:
    """One step's recorded outcome, waiting to be seeded."""

    name: str
    kind: EntryKind = EntryKind.STEP
    returns: Any = None
    raises: BaseException | None = None
    occurrence: int = 0
    """Which call of this step in the body — 0 is the first."""


def given(
    target: Any,
    *,
    returns: Any = None,
    raises: BaseException | None = None,
    occurrence: int = 0,
    kind: EntryKind = EntryKind.STEP,
) -> Given:
    """Declare what a step already returned, or already raised.

    Args:
        target: A ``@step``, a workflow, or the name of one.
        returns: What the journal should say it produced.
        raises: What it should say it failed with. Mutually exclusive with
            ``returns``; a seeded failure replays as the failure the workflow
            originally saw, which is how error paths get tested without
            arranging for a real error.
        occurrence: Which call of this step, when the body calls it more than
            once. Zero-based.
        kind: The entry kind, for seeding something other than a step —
            ``EntryKind.AGENT`` for a ``ctx.agent()`` call.

    Positional binding is the sharp edge, and it is the engine's, not this
    helper's: a seeded entry lands at the position the step occupies when the
    body runs. Insert a durable call before it and the binding moves with it.
    Name the step rather than the position wherever that is possible — this
    resolves by name and kind, so it is only the *occurrence* that is ordinal.
    """
    if returns is not None and raises is not None:
        raise ValueError("given() takes returns= or raises=, not both")
    return Given(
        name=_name_of(target),
        kind=kind,
        returns=returns,
        raises=raises,
        occurrence=occurrence,
    )


def seed(*facts: Given) -> list[JournalEntry]:
    """Build journal entries from :func:`given` declarations.

    Exposed because a caller with its own Runtime wants the entries, not the
    driving — ``store.save_journal(run_id, seed(...))`` then ``rt.run(...)``
    with that run id.
    """
    entries: list[JournalEntry] = []
    for index, fact in enumerate(facts):
        entry = JournalEntry(
            path=str(index),
            kind=fact.kind,
            name=fact.name,
            status=EntryStatus.FAILED if fact.raises else EntryStatus.COMPLETED,
            output=None if fact.raises else encode(fact.returns),
            attempts=1,
            metadata={"seeded": True},
        )
        if fact.raises is not None:
            entry.error = ErrorInfo.from_exception(fact.raises, step_name=fact.name)
        entries.append(entry)
    return entries


async def run_with(
    workflow: Any,
    input: Any = None,
    *facts: Given,
    runtime: Any = None,
    **kwargs: Any,
) -> ExecutionResult:
    """Run *workflow* against a journal that already contains *facts*.

    Args:
        workflow: The workflow to run.
        input: Its input.
        facts: :func:`given` declarations, in the order the body reaches them.
        runtime: An existing Runtime. One over ``MemoryStore`` is built when
            omitted.
        kwargs: Passed through to ``Runtime.run``.

    The seeded entries are matched by name and kind, so a mismatch surfaces as
    the engine's own divergence error rather than as a silently ignored fact.
    """
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.state.memory import MemoryStore

    rt = runtime or Runtime(store=MemoryStore())
    rt.register(workflow)

    run_id = f"seeded_{id(facts)}"
    entries = seed(*facts)
    if entries:
        record = await rt._open_execution(
            workflow,
            input,
            trigger=_manual(),
            idempotency_key=None,
            parent_run_id=None,
            root_run_id=None,
            tags=(),
            metadata=None,
            run_id=run_id,
            credentials=None,
            env=None,
        )
        await rt.store.save_journal(record.run_id, entries)
        return await rt._drive(record.run_id, deps=kwargs.get("deps"))
    return await rt.run(workflow, input, **kwargs)


async def assert_replays(
    workflow: Any,
    input: Any = None,
    *,
    runtime: Any = None,
) -> ExecutionResult:
    """Run *workflow*, then replay it, and assert nothing changed.

    A workflow that reads a clock, a random source, or unjournaled state
    directly produces a different answer the second time — which is the failure
    replay exists to prevent, and the one that shows up as a crash-resumed run
    doing something the first attempt did not.

    This is the ``replay`` stage from the coding agent's verification pipeline,
    available to anyone writing a workflow by hand.

    Raises:
        AssertionError: If the replay produced a different output or executed
            work the first run had already recorded.
    """
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.state.memory import MemoryStore

    rt = runtime or Runtime(store=MemoryStore())
    rt.register(workflow)

    first = await rt.run(workflow, input)
    second = await rt.replay(first.run_id)

    if first.output != second.output:
        raise AssertionError(
            f"{workflow.name} is not deterministic: the first run returned "
            f"{first.output!r} and replaying its journal returned "
            f"{second.output!r}. Move clocks, randomness, and I/O into steps — "
            f"ctx.now(), ctx.uuid4(), ctx.random(), ctx.step() — so their "
            f"results are recorded rather than recomputed."
        )
    if first.status is not second.status:
        raise AssertionError(
            f"{workflow.name} replayed to {second.status.value} after running "
            f"to {first.status.value}."
        )
    return second


def _name_of(target: Any) -> str:
    for attribute in ("name", "__name__"):
        found = getattr(target, attribute, None)
        if isinstance(found, str):
            return found
    return str(target)


def _manual() -> Any:
    from workflow_builder.core.models import TriggerKind

    return TriggerKind.MANUAL
