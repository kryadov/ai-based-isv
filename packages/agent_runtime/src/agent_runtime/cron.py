"""Five-field cron expressions (minute hour day-of-month month day-of-week), UTC.

Supports ``*``, numbers, lists (``1,15``), ranges (``9-17``) and steps
(``*/15``, ``9-17/4``); day-of-week 0 and 7 are Sunday. As in classic cron,
when both day fields are restricted a day matches if EITHER matches. A small
typed evaluator instead of a dependency: automation schedules only need
"what was the latest tick at or before now".
"""

from __future__ import annotations

from datetime import datetime, timedelta

_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
LOOKBACK = timedelta(days=62)  # covers monthly schedules


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"bad cron step in {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
            if step_text:
                end = high
        if not (low <= start <= end <= high):
            raise ValueError(f"cron value out of range in {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse(expr: str) -> tuple[frozenset[int], ...]:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields: {expr!r}")
    try:
        fields = tuple(_field(p, lo, hi) for p, (lo, hi) in zip(parts, _BOUNDS, strict=True))
    except ValueError as exc:
        raise ValueError(f"invalid cron {expr!r}: {exc}") from exc
    return fields


def _matches(fields: tuple[frozenset[int], ...], both_days: bool, when: datetime) -> bool:
    minute, hour, dom, month, dow = fields
    weekday = (when.weekday() + 1) % 7  # Monday=1 … Sunday=0
    dom_ok, dow_ok = when.day in dom, weekday in dow
    day_ok = (dom_ok or dow_ok) if both_days else (dom_ok and dow_ok)
    return when.minute in minute and when.hour in hour and when.month in month and day_ok


def _compile(expr: str) -> tuple[tuple[frozenset[int], ...], bool]:
    minute, hour, dom, month, dow = parse(expr)
    sunday = frozenset(0 if d == 7 else d for d in dow)
    parts = expr.split()
    return (minute, hour, dom, month, sunday), parts[2] != "*" and parts[4] != "*"


def cron_matches(expr: str, when: datetime) -> bool:
    fields, both_days = _compile(expr)
    return _matches(fields, both_days, when)


def latest_tick(expr: str, now: datetime) -> datetime | None:
    """The latest minute ≤ ``now`` matching ``expr`` (within ``LOOKBACK``)."""
    fields, both_days = _compile(expr)
    moment = now.replace(second=0, microsecond=0)
    floor = moment - LOOKBACK
    while moment >= floor:
        if _matches(fields, both_days, moment):
            return moment
        moment -= timedelta(minutes=1)
    return None
