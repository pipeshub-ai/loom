"""A dependency-free cron expression evaluator.

Supports standard 5-field expressions (``minute hour day month weekday``) and 6-field
expressions with a leading seconds column, plus ``*``, ranges, lists, steps, and the
common ``@hourly``/``@daily``/``@weekly``/``@monthly``/``@yearly`` aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ALIASES: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
    "@minutely": "* * * * *",
}

_FIELD_RANGES: list[tuple[int, int]] = [
    (0, 59),  # second
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week, Sunday = 0
]

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_DAY_NAMES = {
    name: index
    for index, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"], 0)
}


class CronError(ValueError):
    """Raised for a malformed cron expression."""


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A parsed cron expression that can compute its next fire time."""

    expression: str
    seconds: frozenset[int]
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool
    timezone: str = "UTC"

    @classmethod
    def parse(cls, expression: str, *, timezone: str = "UTC") -> CronSchedule:
        raw = ALIASES.get(expression.strip().lower(), expression).strip()
        parts = raw.split()
        if len(parts) == 5:
            parts = ["0", *parts]
        if len(parts) != 6:
            raise CronError(
                f"cron expression must have 5 or 6 fields, got {len(parts)}: {expression!r}"
            )

        fields = [
            _parse_field(part, low, high, index)
            for index, (part, (low, high)) in enumerate(zip(parts, _FIELD_RANGES, strict=True))
        ]
        return cls(
            expression=expression,
            seconds=fields[0],
            minutes=fields[1],
            hours=fields[2],
            days=fields[3],
            months=fields[4],
            weekdays=fields[5],
            day_restricted=parts[3] != "*",
            weekday_restricted=parts[5] != "*",
            timezone=timezone,
        )

    def matches(self, moment: datetime) -> bool:
        if moment.second not in self.seconds:
            return False
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        # Cron's historical quirk: when both day-of-month and day-of-week are restricted,
        # a match on *either* fires the job.
        day_ok = moment.day in self.days
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        if self.day_restricted:
            return day_ok
        if self.weekday_restricted:
            return weekday_ok
        return True

    def next_after(self, after: datetime | None = None) -> datetime:
        """First moment strictly after ``after`` that matches this schedule.

        Pass one. Defaulting to the wall clock is the fallback for a caller
        holding no moment at all, and it is why the dispatcher, the facade, and
        ``Schedule.describe`` all supply ``clock.now()`` explicitly: a schedule
        computed from the wall clock inside a run on virtual time answers a
        question about a different day, and a trigger whose next fire is a year
        away looks like one that is simply broken.
        """
        tz = ZoneInfo(self.timezone) if self.timezone != "UTC" else UTC
        start = (after or datetime.now(UTC)).astimezone(tz)
        candidate = start.replace(microsecond=0) + timedelta(seconds=1)

        # Four years covers every leap-year edge case (for example "0 0 29 2 *").
        limit = candidate + timedelta(days=366 * 4)
        while candidate < limit:
            if candidate.month not in self.months:
                candidate = _advance_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0, second=0)
                continue
            if candidate.minute not in self.minutes:
                candidate = (candidate + timedelta(minutes=1)).replace(second=0)
                continue
            if candidate.second not in self.seconds:
                candidate += timedelta(seconds=1)
                continue
            return candidate.astimezone(UTC)

        raise CronError(f"no fire time found within four years for {self.expression!r}")

    def _day_matches(self, moment: datetime) -> bool:
        day_ok = moment.day in self.days
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        if self.day_restricted:
            return day_ok
        if self.weekday_restricted:
            return weekday_ok
        return True


def _advance_month(moment: datetime) -> datetime:
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0, second=0)
    return moment.replace(month=moment.month + 1, day=1, hour=0, minute=0, second=0)


def _parse_field(field: str, low: int, high: int, index: int) -> frozenset[int]:
    values: set[int] = set()
    for chunk in field.split(","):
        step = 1
        body = chunk
        if "/" in chunk:
            body, _, step_text = chunk.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"invalid step '{step_text}' in {field!r}") from exc
            if step < 1:
                raise CronError(f"step must be positive in {field!r}")

        if body in ("*", "?"):
            start, end = low, high
        elif "-" in body:
            start_text, _, end_text = body.partition("-")
            start, end = _to_int(start_text, index), _to_int(end_text, index)
        else:
            start = end = _to_int(body, index)

        if start < low or end > high or start > end:
            raise CronError(f"value out of range [{low},{high}] in field {field!r}")
        values.update(range(start, end + 1, step))

    if index == 5:
        # Both 0 and 7 mean Sunday.
        values = {0 if value == 7 else value for value in values}
    return frozenset(values)


def _to_int(text: str, index: int) -> int:
    normalized = text.strip().lower()
    if index == 4 and normalized in _MONTH_NAMES:
        return _MONTH_NAMES[normalized]
    if index == 5:
        if normalized in _DAY_NAMES:
            return _DAY_NAMES[normalized]
        if normalized == "7":
            return 0
    try:
        return int(normalized)
    except ValueError as exc:
        raise CronError(f"invalid cron value: {text!r}") from exc
