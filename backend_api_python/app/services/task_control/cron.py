"""Small dependency-free standard five-field Cron evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_MONTHS = {name: index for index, name in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_WEEKDAYS = {name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}


@dataclass(frozen=True, slots=True)
class CronExpression:
    expression: str
    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    dom_wildcard: bool
    dow_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        fields = str(expression or "").split()
        if len(fields) != 5:
            raise ValueError("Cron must contain exactly five fields")
        dom_wildcard = fields[2] == "*"
        dow_wildcard = fields[4] == "*"
        return cls(
            expression=" ".join(fields),
            minute=frozenset(_parse_field(fields[0], 0, 59)),
            hour=frozenset(_parse_field(fields[1], 0, 23)),
            day_of_month=frozenset(_parse_field(fields[2], 1, 31)),
            month=frozenset(_parse_field(fields[3], 1, 12, names=_MONTHS)),
            day_of_week=frozenset(_parse_field(fields[4], 0, 7, names=_WEEKDAYS, normalize_sunday=True)),
            dom_wildcard=dom_wildcard,
            dow_wildcard=dow_wildcard,
        )

    def next_after(self, after: datetime, timezone: str = "Asia/Shanghai") -> datetime:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown Cron timezone: {timezone}") from exc
        cursor = after.astimezone(zone) if after.tzinfo else after.replace(tzinfo=zone)
        cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Five years is enough to find every valid standard Cron expression;
        # the bound prevents malformed calendars from creating an infinite loop.
        for _ in range(5 * 366 * 24 * 60):
            if self._matches(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        raise ValueError(f"Cron expression has no occurrence within five years: {self.expression}")

    def _matches(self, value: datetime) -> bool:
        if value.minute not in self.minute or value.hour not in self.hour or value.month not in self.month:
            return False
        dom_match = value.day in self.day_of_month
        # Python Monday=0; Cron Sunday=0/7, so convert to Cron numbering.
        cron_dow = (value.weekday() + 1) % 7
        dow_match = cron_dow in self.day_of_week
        if self.dom_wildcard or self.dow_wildcard:
            return dom_match and dow_match
        return dom_match or dow_match


def _parse_field(
    field: str,
    minimum: int,
    maximum: int,
    *,
    names: dict[str, int] | None = None,
    normalize_sunday: bool = False,
) -> set[int]:
    values: set[int] = set()
    for part in field.upper().split(","):
        if not part:
            raise ValueError("Cron contains an empty list item")
        base, separator, step_text = part.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"Invalid Cron step: {step_text}") from exc
            if step <= 0:
                raise ValueError("Cron step must be positive")
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = _parse_atom(left, names), _parse_atom(right, names)
            if start > end:
                raise ValueError("Cron range must be ascending")
        else:
            if separator:
                raise ValueError("Cron step requires * or a range")
            start = end = _parse_atom(base, names)
        for value in range(start, end + 1, step):
            if value < minimum or value > maximum:
                raise ValueError(f"Cron value out of range: {value}")
            if normalize_sunday and value == 7:
                value = 0
            values.add(value)
    if not values:
        raise ValueError("Cron field cannot be empty")
    return values


def _parse_atom(value: str, names: dict[str, int] | None) -> int:
    if names and value in names:
        return names[value]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid Cron value: {value}") from exc
