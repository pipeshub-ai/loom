"""Who is reading what, and from where.

A subscription is the pairing of a **subscriber identity** with a topic, a
filter, and a starting position. Two of those three carry decisions that are
easy to get wrong and expensive to change later, so both are spelled out here
rather than left to the dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from loom.triggers.filter import FilterSpec

__all__ = ["StartAt", "Subscription"]


class StartAt(StrEnum):
    """Where a subscriber with no checkpoint begins."""

    LATEST = "latest"
    """Only what arrives from now on. The default, and the only one a workflow
    may declare."""

    EARLIEST = "earliest"
    """Everything still retained.

    Reachable programmatically — that is how a replay is driven — but
    **refused in a workflow declaration**. See
    :meth:`Subscription.validate_declarable`.
    """


@dataclass(frozen=True, slots=True)
class Subscription:
    """One reader of one topic.

    **Identity is a stable name and never includes the filter.** That is the
    load-bearing decision here. The tempting alternative — hashing the filter
    into the identity, so that "the subscription changed" means "a new
    subscription" — turns every filter edit into a duplicate storm: the dispatch
    key is ``{event_id}#{subscriber}``, so changing the subscriber changes every
    historical event's key, nothing deduplicates, and everything already handled
    runs again.

    With a stable identity the two intents separate cleanly:

    *Widen a filter going forward* — the common case — is just an edit. The
    checkpoint is kept, and only new events are considered.

    *Widen it retroactively* is an explicit replay, and it is safe by
    construction: events this subscriber already handled re-derive their
    original dispatch key and deduplicate away, so only the newly-matching ones
    start runs. No diffing engine required; it falls out of the key.
    """

    subscriber: str
    """Stable across filter edits. Defaults to the workflow name; a workflow
    declaring more than one subscription **must** name them, or they share a
    checkpoint and silently consume each other's backlog."""
    topic: str
    workflow: str
    filter: FilterSpec | None = None
    start_at: StartAt = StartAt.LATEST
    max_attempts: int = 3
    """Deliveries one event gets before it is dead-lettered and stepped over.
    Without a ceiling a single unprocessable event stalls this subscriber
    forever; without a dead letter it vanishes. Both are needed."""

    def validate_declarable(self) -> None:
        """Refuse what a workflow file must not be able to say.

        ``EARLIEST`` is not declarable because its blast radius depends on data
        the author cannot see. Backfilling a week of Slack into a workflow that
        replies means a week of replies, sent at once, to real people — and the
        dispatch key does *not* protect against it, because a genuinely new
        subscriber has seen none of those events, so every one of them is
        legitimately new.

        That makes backfill an operational act with bounds and a dry run, not a
        line in a workflow file.
        """
        if self.start_at is StartAt.EARLIEST:
            raise ValueError(
                f"subscription '{self.subscriber}' declares start_at=EARLIEST, "
                "which is not declarable: replaying a retained backlog into a "
                "workflow with side effects performs all of them at once. Use "
                "start_at=LATEST here, and run the backfill explicitly with "
                "bounds — `loom events replay --subscriber "
                f"{self.subscriber} --since 7d --max-events 1000`."
            )
        if self.filter is not None:
            # The check `accepts()` below has always claimed happened here. It
            # did not, so a mistyped operator reached evaluation instead — where
            # it raises, outside the dead-letter path, and stalls the subscriber
            # on its first event forever.
            self.filter.check()

    def accepts(self, payload: object) -> bool:
        """Whether this subscription wants an event with *payload*.

        Evaluated by :class:`~loom.triggers.filter.FilterSpec`, which is also
        what validates a filter when it is declared. One evaluator, used twice:
        two would let a filter validate and then never match, which is a bug
        that presents as "the workflow simply never runs".
        """
        if self.filter is None:
            return True
        if not isinstance(payload, dict):
            return False
        return self.filter.matches(payload)
