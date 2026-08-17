"""``loom events`` — operability for the event backbone.

Read-only by default, and deliberately so. The one thing here that changes
anything is ``replay``, which rewinds a checkpoint; everything else answers a
question an operator has at 3am and currently has no way to ask:

``topics``
    What exists, and how much is on it.
``tail``
    What actually arrived — the answer to "is the webhook even reaching us?"
``subscriptions``
    Who is reading, and how far behind.
``status``
    Anything unhealthy, and **exit 1 when there is**, so this is usable from a
    monitoring check rather than only by eye.
``dead``
    What could not be processed. A dead-letter that nobody can read is a log
    line with extra steps.
``replay``
    The bounded backfill that ``start_at=EARLIEST`` is refused in favour of.

There is deliberately **no** ``loom events install`` to register a webhook with
a provider. Shipping one means owning N provider admin APIs forever, for an
operation performed once per deployment; provider registration stays in the
host's deployment, and what LOOM offers is the read-only view of whether it is
still working.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import timedelta
from typing import Any

from loom.cli.commands import printer_for
from loom.cli.output import Exit

__all__ = ["cmd_events"]


def cmd_events(args: argparse.Namespace) -> int:
    """Dispatch one ``loom events`` subcommand."""
    handler = _SUBCOMMANDS.get(getattr(args, "events_command", None) or "")
    if handler is None:
        printer_for(args).error(
            "usage: loom events {topics|tail|subscriptions|status|dead|replay}"
        )
        return Exit.USAGE
    return asyncio.run(handler(args))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _wire(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """The runtime, its event log, and a subscription manager over both.

    Falls back to :class:`~loom.events.log.StoreBackedEventLog` when the
    Runtime has none configured — which is not a guess: it is the reference
    implementation, it needs nothing beyond the store already in play, and it
    is where events are unless somebody deliberately put them elsewhere. A host
    with a Kafka adapter runs this against its own Runtime, where ``rt.events``
    is set and this branch never fires.
    """
    from loom.cli import targets
    from loom.events.log import StoreBackedCheckpoints, StoreBackedEventLog
    from loom.events.manager import SubscriptionManager

    target = targets.resolve(None, modules=getattr(args, "module", None))
    runtime = getattr(target.backend, "runtime", None)
    if runtime is None:
        raise SystemExit(
            "loom events needs a local Runtime: the event log is read from the "
            "store directly, and a --server proxy has no way to expose it yet"
        )

    # The Runtime's clock reaches all three, because they disagree otherwise:
    # the checkpoints stamp `updated_at` and the manager reads it back to
    # report idle time, so a writer and a reader on two clocks would make
    # `loom events status` report an idle age that no subscriber ever had.
    clock = getattr(runtime, "clock", None)
    log = getattr(runtime, "events", None) or StoreBackedEventLog(
        runtime.store, clock=clock
    )
    marks = getattr(runtime, "checkpoints", None) or StoreBackedCheckpoints(
        runtime.store, clock=clock
    )
    manager = SubscriptionManager(
        runtime.store, log=log, checkpoints=marks, clock=clock
    )
    return runtime, log, manager


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _topics(args: argparse.Namespace) -> int:
    out = printer_for(args)
    _, log, manager = _wire(args)

    rows = []
    for topic in await manager.topics():
        head = await log.head(topic)
        rows.append({"topic": topic, "head": head, "empty": head is None})

    out.json(rows)
    if not rows:
        out.line("  no topics — nothing has been appended to this log")
        return Exit.OK
    out.table(
        ["topic", "head"],
        [[row["topic"], str(row["head"] or "—")] for row in rows],
    )
    return Exit.OK


async def _tail(args: argparse.Namespace) -> int:
    """Recent events on one topic.

    Reads from the beginning and shows the last *n*, because ``read`` takes a
    position to read *after* and positions are opaque — there is no "seek to
    the end minus twenty" that works on every backend, and inventing one here
    would be the arithmetic on positions the contract forbids.
    """
    out = printer_for(args)
    _, log, _ = _wire(args)

    events = list(await log.read(args.topic, after=None, limit=max(args.limit * 10, 100)))
    events = events[-args.limit :]

    payload = [
        {
            "position": event.position,
            "event_id": event.event_id,
            "type": event.type,
            "key": event.record.key,
            "source": event.record.source,
            "appended_at": event.appended_at.isoformat(),
            "payload": dict(event.payload),
        }
        for event in events
    ]
    out.json(payload)
    if not events:
        out.line(f"  nothing on '{args.topic}'")
        return Exit.OK
    out.table(
        ["pos", "type", "key", "appended"],
        [
            [
                row["position"],
                row["type"][:30],
                (row["key"] or "—")[:20],
                row["appended_at"][:19],
            ]
            for row in payload
        ],
    )
    return Exit.OK


async def _subscriptions(args: argparse.Namespace) -> int:
    out = printer_for(args)
    _, _, manager = _wire(args)

    rows = [row.as_dict() for row in await manager.health(getattr(args, "topic", None))]
    out.json(rows)
    if not rows:
        out.line("  no subscriptions — nothing is reading this log")
        return Exit.OK
    out.table(
        ["subscriber", "topic", "position", "lag", "state"],
        [
            [
                row["subscriber"],
                row["topic"][:32],
                str(row["position"] or "—"),
                "?" if row["lag"] is None else str(row["lag"]),
                _state(row),
            ]
            for row in rows
        ],
    )
    return Exit.OK


async def _status(args: argparse.Namespace) -> int:
    """Everything unhealthy, and a non-zero exit when there is any.

    The exit code is the point. A status command that always succeeds is one
    that can only be read by a person, and the failure this whole subsystem
    exists to catch is precisely the one nobody is looking at.
    """
    out = printer_for(args)
    _, log, manager = _wire(args)

    health = await manager.health()
    unhealthy = [row for row in health if not row.healthy]
    lagging = [
        row
        for row in health
        if row.healthy and row.lag is not None and row.lag >= args.max_lag
    ]

    dead: list[dict[str, Any]] = []
    for topic in await manager.topics():
        if not topic.endswith(".dead"):
            continue
        head = await log.head(topic)
        if head is not None:
            dead.append({"topic": topic, "head": head})

    payload = {
        "subscriptions": len(health),
        "unhealthy": [row.as_dict() for row in unhealthy],
        "lagging": [row.as_dict() for row in lagging],
        "dead_letters": dead,
        "ok": not unhealthy and not lagging and not dead,
    }
    out.json(payload)

    if payload["ok"]:
        out.line(f"  {len(health)} subscription(s), all current")
        return Exit.OK

    for row in unhealthy:
        out.line(f"  [red]✗[/red] {row.subscriber} on {row.topic}: {row.reason or 'quarantined'}")
    for row in lagging:
        out.line(f"  [yellow]![/yellow] {row.subscriber} on {row.topic}: {row.lag} behind")
    for entry in dead:
        out.line(f"  [yellow]![/yellow] {entry['topic']} has undeliverable events")
    return Exit.FAILED


async def _dead(args: argparse.Namespace) -> int:
    out = printer_for(args)
    _, log, manager = _wire(args)

    topics = (
        [args.topic if args.topic.endswith(".dead") else f"{args.topic}.dead"]
        if args.topic
        else [t for t in await manager.topics() if t.endswith(".dead")]
    )

    rows: list[dict[str, Any]] = []
    for topic in topics:
        for event in await log.read(topic, after=None, limit=args.limit):
            payload = dict(event.payload)
            rows.append(
                {
                    "topic": topic,
                    "event_id": payload.get("event_id", event.event_id),
                    "subscriber": payload.get("subscriber", ""),
                    "workflow": payload.get("workflow", ""),
                    "error": payload.get("error", ""),
                    "original": payload.get("original", {}),
                }
            )

    out.json(rows)
    if not rows:
        out.line("  nothing dead-lettered")
        return Exit.OK
    out.table(
        ["subscriber", "workflow", "error"],
        [
            [row["subscriber"][:20], row["workflow"][:20], row["error"][:56]]
            for row in rows
        ],
    )
    return Exit.OK


async def _replay(args: argparse.Namespace) -> int:
    """Rewind a subscriber so its next pass re-reads a bounded window.

    Prints the plan first and requires ``--yes`` to act, because the number is
    the whole safeguard: replaying a week of Slack into a workflow that replies
    means a week of replies, sent at once, to real people. That is also why
    ``start_at=EARLIEST`` cannot be declared in a workflow file — the author
    cannot see the number, and here it is printed before anything happens.
    """
    out = printer_for(args)
    _, _, manager = _wire(args)

    since = _duration(args.since) if args.since else None
    plan = await manager.plan_replay(
        args.subscriber, args.topic, since=since, max_events=args.max_events
    )

    if not args.yes:
        out.json({**plan.as_dict(), "applied": False})
        out.line(
            f"  would replay {plan.events} event(s) on '{plan.topic}' for "
            f"'{plan.subscriber}'"
            + (" (truncated by --max-events)" if plan.truncated else "")
        )
        out.line("  events this subscriber already handled will deduplicate away")
        out.line("  re-run with --yes to apply")
        return Exit.OK

    applied = await manager.replay(
        args.subscriber, args.topic, since=since, max_events=args.max_events
    )
    out.json({**applied.as_dict(), "applied": True})
    out.line(
        f"  rewound '{applied.subscriber}' to replay {applied.events} event(s); "
        "the dispatcher will pick them up on its next pass"
    )
    return Exit.OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    """Register ``loom events`` and its subcommands."""
    from loom.cli import _add_backend, _add_output

    events = sub.add_parser(
        "events", help="Inspect the event log, its subscribers, and their lag"
    )
    inner = events.add_subparsers(dest="events_command", metavar="<subcommand>")

    topics = inner.add_parser("topics", help="List topics and their head position")
    _add_backend(topics)
    _add_output(topics)

    tail = inner.add_parser("tail", help="Show recent events on a topic")
    tail.add_argument("topic")
    tail.add_argument("--limit", type=int, default=20)
    _add_backend(tail)
    _add_output(tail)

    subs = inner.add_parser(
        "subscriptions", help="List subscribers, their positions, and their lag"
    )
    subs.add_argument("--topic", help="Only this topic")
    _add_backend(subs)
    _add_output(subs)

    status = inner.add_parser(
        "status", help="Report anything unhealthy; exit 1 when there is"
    )
    status.add_argument(
        "--max-lag",
        type=int,
        default=1000,
        help="Events behind before a healthy subscriber is reported as lagging",
    )
    _add_backend(status)
    _add_output(status)

    dead = inner.add_parser("dead", help="Show dead-lettered events")
    dead.add_argument("topic", nargs="?", help="A topic, with or without .dead")
    dead.add_argument("--limit", type=int, default=20)
    _add_backend(dead)
    _add_output(dead)

    replay = inner.add_parser(
        "replay", help="Rewind a subscriber to re-read a bounded window"
    )
    replay.add_argument("--subscriber", required=True)
    replay.add_argument("--topic", required=True)
    replay.add_argument("--since", help="Only events this recent, e.g. 7d, 12h, 30m")
    replay.add_argument("--max-events", type=int, default=1000)
    replay.add_argument(
        "--yes", action="store_true", help="Apply it; without this, print the plan only"
    )
    _add_backend(replay)
    _add_output(replay)


_SUBCOMMANDS = {
    "topics": _topics,
    "tail": _tail,
    "subscriptions": _subscriptions,
    "status": _status,
    "dead": _dead,
    "replay": _replay,
}


def _state(row: dict[str, Any]) -> str:
    if row["quarantined"]:
        return "quarantined"
    if row["reason"]:
        return "stale"
    if row["position"] is None:
        return "new"
    return "ok"


_DURATION = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _duration(value: str) -> timedelta:
    """``7d``, ``12h``, ``30m``. Refuses anything else rather than guessing.

    A bare number would have to mean *something*, and every choice is wrong for
    somebody — ``--since 7`` meaning seconds when the writer meant days turns a
    deliberate backfill into a no-op that reports success.
    """
    match = _DURATION.match(value.strip().lower())
    if match is None:
        raise SystemExit(
            f"--since {value!r} is not a duration. Use a number and a unit: "
            "30m, 12h, 7d, 2w."
        )
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
