"""Minimal 5-field cron support for api/scheduler.py — no croniter/
python-crontab dependency, matching this project's convention of
stdlib-only where a small hand-rolled implementation suffices
(host_stats.py, discovery.py, etc.).

Two entry points:
  - build_cron(): turns the dashboard's simple recurrence picker
    (daily/weekly/interval_minutes/interval_hours) into a cron string
    server-side — the only four shapes ever produced, so next_fire()
    below doesn't need to handle cron's DOM+DOW-both-restricted "OR"
    quirk (every generated expression leaves at least one of the two
    as '*').
  - next_fire(): brute-force minute-stepping forward from a given
    timestamp to the next matching minute. Cheap at minute granularity
    (at most 525,600 steps for a once-a-year match, typically far
    fewer) and correct without needing a closed-form "next occurrence"
    solver.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class InvalidCron(ValueError):
    pass


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(lo, hi + 1))
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise InvalidCron(f"bad step in cron field: {field!r}")
            if step <= 0:
                raise InvalidCron(f"step must be positive: {field!r}")
        if part == "*":
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                rng = range(int(a), int(b) + 1)
            except ValueError:
                raise InvalidCron(f"bad range in cron field: {field!r}")
        else:
            try:
                rng = range(int(part), int(part) + 1)
            except ValueError:
                raise InvalidCron(f"bad value in cron field: {field!r}")
        for v in rng:
            if v < lo or v > hi:
                raise InvalidCron(f"value {v} out of range [{lo},{hi}] in {field!r}")
            if (v - lo) % step == 0:
                values.add(v)
    if not values:
        raise InvalidCron(f"cron field produced no values: {field!r}")
    return values


def _parse(cron_expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = cron_expr.split()
    if len(fields) != 5:
        raise InvalidCron(f"expected 5 fields (min hour dom month dow), got {cron_expr!r}")
    minute, hour, dom, month, dow = fields
    return (
        _parse_field(minute, 0, 59),
        _parse_field(hour, 0, 23),
        _parse_field(dom, 1, 31),
        _parse_field(month, 1, 12),
        _parse_field(dow, 0, 6),  # 0 = Sunday, matching cron convention
    )


def next_fire(cron_expr: str, after: datetime) -> datetime:
    """Next minute matching cron_expr strictly after `after`, truncated
    to whole minutes. Only handles the "at least one of dom/dow is '*'"
    case (see module docstring) — every shape build_cron() produces
    satisfies that."""
    minutes, hours, doms, months, dows = _parse(cron_expr)
    dom_restricted = len(doms) < 31
    dow_restricted = len(dows) < 7

    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    # Bounded search: at most ~5.5 years of minutes before giving up,
    # long enough for any real schedule, short enough to never hang.
    limit = candidate + timedelta(days=365 * 5 + 30)
    while candidate < limit:
        if candidate.month not in months:
            # Jump to the 1st of the next month rather than stepping
            # minute-by-minute through a whole excluded month.
            year, month = candidate.year, candidate.month
            month += 1
            if month > 12:
                month = 1
                year += 1
            candidate = candidate.replace(
                year=year, month=month, day=1, hour=0, minute=0)
            continue
        dom_ok = candidate.day in doms
        # Python weekday(): Monday=0..Sunday=6 -> cron dow: Sunday=0..Saturday=6
        cron_dow = (candidate.weekday() + 1) % 7
        dow_ok = cron_dow in dows
        if dom_restricted and dow_restricted:
            day_ok = dom_ok or dow_ok
        elif dom_restricted:
            day_ok = dom_ok
        elif dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True
        if not day_ok:
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in hours:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        if candidate.minute not in minutes:
            candidate = candidate + timedelta(minutes=1)
            continue
        return candidate
    raise InvalidCron(f"no matching fire time found for {cron_expr!r} within 5 years")


def build_cron(mode: str, **params) -> str:
    """Build a cron string from the dashboard's simple picker output.
    Raises InvalidCron on a bad/missing param rather than silently
    defaulting — a schedule that never fires because of a swallowed
    typo is worse than a rejected create."""
    def _int(name: str, lo: int, hi: int) -> int:
        if name not in params:
            raise InvalidCron(f"mode {mode!r} requires {name!r}")
        try:
            v = int(params[name])
        except (TypeError, ValueError):
            raise InvalidCron(f"{name!r} must be an integer")
        if not (lo <= v <= hi):
            raise InvalidCron(f"{name!r} must be between {lo} and {hi}")
        return v

    if mode == "daily":
        hour = _int("hour", 0, 23)
        minute = _int("minute", 0, 59)
        return f"{minute} {hour} * * *"
    if mode == "weekly":
        hour = _int("hour", 0, 23)
        minute = _int("minute", 0, 59)
        weekday = _int("weekday", 0, 6)  # 0=Sunday
        return f"{minute} {hour} * * {weekday}"
    if mode == "interval_minutes":
        n = _int("minutes", 1, 59)
        return f"*/{n} * * * *"
    if mode == "interval_hours":
        n = _int("hours", 1, 23)
        return f"0 */{n} * * *"
    raise InvalidCron(f"unknown recurrence mode: {mode!r}")
