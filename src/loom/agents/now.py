"""What time it is, told to a model that has no way to know.

A model's sense of "now" is the end of its training data, and it is confidently
wrong about everything after that. Asked to build a workflow that reports the
winner of a tournament held three months ago, it refused — the season "hasn't
happened yet" — and wrote the refusal into the file where the workflow should
have been. Nothing in the pipeline could catch that: the code compiled, ran, and
answered, and the answer was a considered explanation of why the question was
impossible.

That is the failure this module exists for, and it is not fixable by prompting
harder. A model cannot reason its way to the date. It has to be told.

So every agent surface states the moment it is running in, and states it the
same way, from one renderer:

``time_block()``
    The block that goes in a system prompt: the local date and time, the zone
    it is in, and the same instant in UTC.

**It is one block per agent run, never per turn.** ``build_system_prompt()`` is
called once for a coding job and ``execute()`` builds its messages once for a
turn loop, so the rendered minute is stable for as long as the conversation is —
which is what keeps a system prompt eligible for provider-side caching. A block
recomputed per turn would invalidate that cache on every turn of every agent, to
report a minute nobody reads.

**Time comes from the ``Clock`` port**, not from ``datetime.now()``, for the
reason the rest of the runtime reads it there: a test under ``ManualClock``
pins what the agent is told, so "does the agent know the date" is an assertion
rather than something that drifts with the wall clock.

**The zone is the machine's**, resolved once per call:

1. ``$LOOM_TIMEZONE`` — an explicit IANA name, for a deployment whose host
   clock is UTC and whose users are not.
2. ``$TZ`` — the same question, already answered by the shell.
3. ``/etc/localtime``, which on every platform that has it is a symlink into
   the zoneinfo database and so carries the IANA name that ``tzname()`` does
   not.
4. Whatever ``astimezone()`` reports, which is an offset and an abbreviation.

The IANA name matters more than it looks: ``IST`` is Indian, Irish, and Israeli
Standard Time, and an offset alone cannot say which side of a DST change a
future date falls on. Where the name cannot be recovered the offset is still
printed — a zone reported honestly as ``UTC+05:30`` is better than one named
wrongly.

>>> from datetime import UTC, datetime
>>> from loom.runtime.clock import ManualClock
>>> block = time_block(ManualClock(datetime(2026, 8, 21, 9, 2, tzinfo=UTC)),
...                    tz=UTC)
>>> "Friday 21 August 2026" in block
True
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from loom.runtime.clock import Clock, SystemClock

__all__ = ["local_timezone", "time_block", "timezone_label"]


#: Where a zoneinfo path stops being a directory and starts being a zone name.
_ZONEINFO_MARKER = "zoneinfo/"


def _zone(name: str | None) -> tzinfo | None:
    """``ZoneInfo(name)``, or ``None`` for anything it will not accept.

    A bad ``$TZ`` is somebody's typo, not a reason to fail an agent run — the
    next candidate in the ladder is still better than nothing.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # any zoneinfo failure means "try the next candidate"
        return None


def _from_etc_localtime() -> tzinfo | None:
    """The IANA name behind ``/etc/localtime``, when it is a symlink.

    This is the only place the system stores the *name* of the local zone on a
    Unix host that has not set ``$TZ``. ``datetime.astimezone()`` knows the
    offset and the abbreviation and has never known the name.
    """
    link = Path("/etc/localtime")
    try:
        target = str(link.readlink()) if link.is_symlink() else ""
    except OSError:
        return None
    _, marker, name = target.partition(_ZONEINFO_MARKER)
    return _zone(name) if marker else None


def local_timezone() -> tzinfo:
    """The zone this process should report times in.

    Never raises: the last rung of the ladder is the offset ``astimezone()``
    reports, and the one below that is UTC.
    """
    return (
        _zone(os.environ.get("LOOM_TIMEZONE"))
        or _zone(os.environ.get("TZ"))
        or _from_etc_localtime()
        or datetime.now().astimezone().tzinfo
        or UTC
    )


def timezone_label(tz: tzinfo, at: datetime) -> str:
    """How to name *tz* to a reader: ``Asia/Kolkata (UTC+05:30)``.

    Both halves, because neither is sufficient. The name carries the DST rules
    a date in three weeks depends on; the offset is what makes the printed
    clock time checkable against the UTC one beside it.
    """
    moment = at.astimezone(tz)
    offset = moment.utcoffset()
    total = int(offset.total_seconds()) if offset else 0
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total) // 60, 60)
    stamp = f"UTC{sign}{hours:02d}:{minutes:02d}"

    name = getattr(tz, "key", None) or moment.tzname() or ""
    if not name or name == stamp or name in {"UTC", "Coordinated Universal Time"}:
        return stamp
    return f"{name} ({stamp})"


def _spell(moment: datetime) -> str:
    """``Friday 21 August 2026``, with no leading zero on the day.

    Spelled out rather than ``2026-08-21`` because the failure being fixed is a
    model reading a date and reasoning about it from memory, and prose is what
    it reasons in. The ISO form is printed too, for anything parsing this.
    """
    return f"{moment:%A} {moment.day} {moment:%B %Y}"


def time_block(
    clock: Clock | None = None,
    *,
    tz: tzinfo | None = None,
    note: str = "",
) -> str:
    """The current-time section of a system prompt.

    Parameters
    ----------
    clock:
        Where "now" comes from. Defaults to :class:`~loom.runtime.clock.SystemClock`;
        pass the Runtime's own so a test under ``ManualClock`` pins it.
    tz:
        The zone to report in. Defaults to :func:`local_timezone`.
    note:
        One surface-specific sentence appended to the block — what the coding
        agent must do differently with a date, which a chat agent has no use
        for.
    """
    now = (clock or SystemClock()).now()
    zone = tz if tz is not None else local_timezone()
    here = now.astimezone(zone)

    lines = [
        "## Current date and time",
        "",
        f"It is **{_spell(here)}, {here:%H:%M}** — {timezone_label(zone, now)}."
        f" In UTC: {now.astimezone(UTC):%Y-%m-%d %H:%M} ({now.astimezone(UTC):%A}).",
        "",
        "This is the truth about now; your training data ended before it. Treat"
        " anything you believe about what has or has not happened yet as out of"
        " date — an event you remember as upcoming may be months past, and a"
        " date you would call \"the future\" may not be one. When the answer"
        " turns on what has happened, look it up rather than reasoning from"
        " memory, and say what you could not check rather than declaring it"
        " impossible.",
    ]
    if note:
        lines += ["", note]
    return "\n".join(lines)
