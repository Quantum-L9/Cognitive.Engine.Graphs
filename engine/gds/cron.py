"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [gds]
tags: [gds, scheduler, cron, staleness]
owner: engine-team
status: active
--- /L9_META ---

engine/gds/cron.py
The one constructor for a GDS job's cron trigger (GDS-CRON-01).

APScheduler 3.x ``CronTrigger.from_crontab`` numbers ``day_of_week`` from
Monday (0 = Monday, 6 = Sunday). Its own docstring calls this a historical
mistake, fixed in 4.x (issue 286). Standard crontab, and every cron a domain
spec declares, numbers from Sunday: 0 and 7 are Sunday, 1-6 are Monday to
Saturday. Read through ``from_crontab``, ``0 2 * * 0`` ("Sunday 2am") fires on
Monday, so every weekly job written with a numeric weekday runs a day late.

With ``gds_cron_posix_weekday_enabled`` on, ``crontab_trigger`` reads the
weekday field with crontab semantics and hands APScheduler weekday *names*,
which it cannot misnumber:

* a numeric term (single value ``0``, list item, range ``1-5``, ``5-7``, step
  ``*/2``, ``1-5/2``, ``N/S``) is expanded over crontab numbering and emitted as
  APScheduler names; ``*/S`` and ``N/S`` step over 0-7, as Vixie cron does, and
  a day named twice (``0`` and ``7``) is emitted once;
* a term containing a letter (``mon``, ``sat-sun``) is passed through unchanged;
* a bare ``*`` is left alone;
* the minute, hour, day-of-month and month fields are passed through unchanged.

A malformed expression raises ``ValueError``, as ``from_crontab`` does: wrong
field count, an empty term, a weekday above 7, a descending range, a zero or
non-numeric step.

Off: ``CronTrigger.from_crontab`` exactly.

``GDSScheduler._register_cron_job`` and ``engine.gds.staleness`` both build
their trigger here, so the staleness deadline always matches the schedule the
scheduler actually runs.
"""

from __future__ import annotations

from datetime import tzinfo

from apscheduler.triggers.cron import CronTrigger

# APScheduler weekday names, indexed by crontab number (0 and 7 are Sunday).
_POSIX_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
# APScheduler's own order (Monday first), used to emit a stable list.
_APS_WEEKDAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_POSIX_WEEKDAY_MAX = 7
_CRONTAB_FIELDS = 5


def _weekday_number(text: str, term: str) -> int:
    if not text.isdigit():
        msg = f"Invalid day_of_week term {term!r}: {text!r} is not a weekday number"
        raise ValueError(msg)
    number = int(text)
    if number > _POSIX_WEEKDAY_MAX:
        msg = f"Invalid day_of_week term {term!r}: {number} is outside 0-7"
        raise ValueError(msg)
    return number


def _expand_numeric_term(term: str) -> set[str]:
    """Expand one numeric crontab weekday term to APScheduler weekday names."""
    base, slash, step_text = term.partition("/")
    step = 1
    if slash:
        if not step_text.isdigit() or int(step_text) == 0:
            msg = f"Invalid day_of_week term {term!r}: step must be a positive integer"
            raise ValueError(msg)
        step = int(step_text)
    if base == "*":
        first, last = 0, _POSIX_WEEKDAY_MAX
    elif "-" in base:
        first_text, _, last_text = base.partition("-")
        first = _weekday_number(first_text, term)
        last = _weekday_number(last_text, term)
        if first > last:
            msg = f"Invalid day_of_week term {term!r}: range is descending"
            raise ValueError(msg)
    else:
        first = _weekday_number(base, term)
        last = _POSIX_WEEKDAY_MAX if slash else first
    return {_POSIX_WEEKDAY_NAMES[day] for day in range(first, last + 1, step)}


def posix_day_of_week(field: str) -> str:
    """Translate a crontab ``day_of_week`` field to an APScheduler 3.x field.

    Numeric terms take crontab numbering (0 and 7 Sunday) and become weekday
    names; name terms and a bare ``*`` pass through unchanged.
    """
    if field == "*":
        return field
    names: set[str] = set()
    passthrough: list[str] = []
    for term in field.split(","):
        if not term:
            msg = f"Invalid day_of_week field {field!r}: empty term"
            raise ValueError(msg)
        if any(char.isalpha() for char in term):
            passthrough.append(term)
        else:
            names |= _expand_numeric_term(term)
    ordered = [name for name in _APS_WEEKDAY_ORDER if name in names]
    return ",".join([*ordered, *passthrough])


def crontab_trigger(
    expr: str,
    timezone: tzinfo | str | None = None,
    *,
    posix_weekday: bool | None = None,
) -> CronTrigger:
    """Build the APScheduler trigger for a 5-field crontab expression.

    ``posix_weekday`` defaults to ``settings.gds_cron_posix_weekday_enabled``.
    Off returns ``CronTrigger.from_crontab(expr, timezone)`` unchanged.
    """
    if posix_weekday is None:
        from engine.config.settings import settings

        posix_weekday = settings.gds_cron_posix_weekday_enabled
    if not posix_weekday:
        return CronTrigger.from_crontab(expr, timezone)
    values = expr.split()
    if len(values) != _CRONTAB_FIELDS:
        msg = f"Wrong number of fields; got {len(values)}, expected {_CRONTAB_FIELDS}"
        raise ValueError(msg)
    return CronTrigger(
        minute=values[0],
        hour=values[1],
        day=values[2],
        month=values[3],
        day_of_week=posix_day_of_week(values[4]),
        timezone=timezone,
    )
