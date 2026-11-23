"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [gds, health]
tags: [gds, scheduler, staleness, health]
owner: engine-team
status: active
--- /L9_META ---

engine/gds/staleness.py
When a GDS job's last result counts as stale (GDS-STALE-01).

With ``gds_staleness_per_schedule_enabled`` on, staleness follows each job's
own schedule instead of one global age: a cron job is stale once ``now`` is
past the next fire time after its last run plus
``gds_staleness_grace_minutes``. A weekly job is therefore healthy for a week
and an hourly job is stale after about two hours. A manual job has no
schedule to miss and is never stale by age. A job that has never run stays
stale, as before. A history entry naming a job the spec no longer declares,
and a schedule type other than ``cron`` or ``manual``, keep the global
``gds_max_staleness_hours`` age.

The next fire time is computed with ``engine.gds.cron.crontab_trigger``, the
same constructor ``GDSScheduler._register_cron_job`` uses, so the deadline
matches what the scheduler will actually run (including the weekday numbering
``gds_cron_posix_weekday_enabled`` selects, GDS-CRON-01). An unparsable cron expression or timestamp is stale: health
never reports a job fresh on evidence it could not read.

Off: every job uses the global ``gds_max_staleness_hours`` age.

``assess_gds_health`` (CEG-NATIVE-CLOSURE-001 F06/F07) is the one judgement
of a job's health from scheduler history: a job is healthy only when its
latest attempt is ``success`` and its latest *success* is not stale. A failed,
skipped, partial, blocked or refused latest attempt is never healthy however
recent, and a declared job with no history is ``never_run``, not healthy.
Staleness is measured from the latest success, so a failure cannot reset the
clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from engine.gds.cron import crontab_trigger

if TYPE_CHECKING:
    from engine.config.schema import GDSJobSpec

# Just past the last run, so a run that started exactly on a fire time measures
# against the following fire, never the one it was. One microsecond, not one
# second: APScheduler rounds the search start up to a whole second, so a full
# second skipped a fire less than a second after the reference.
_PAST_LAST_RUN = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class StalenessPolicy:
    """The thresholds one health read applies to every job."""

    max_staleness: timedelta
    grace: timedelta
    per_schedule: bool

    @classmethod
    def from_settings(cls) -> StalenessPolicy:
        from engine.config.settings import settings

        return cls(
            max_staleness=timedelta(hours=settings.gds_max_staleness_hours),
            grace=timedelta(minutes=settings.gds_staleness_grace_minutes),
            per_schedule=settings.gds_staleness_per_schedule_enabled,
        )


@dataclass(frozen=True, slots=True)
class JobStaleness:
    """One job's verdict and the instant it becomes (or became) stale."""

    stale: bool
    deadline: datetime | None


def parse_history_timestamp(value: object) -> datetime | None:
    """A history ``timestamp`` as an aware UTC datetime, or None when unreadable.

    A naive timestamp is read as UTC, the convention every consumer already
    follows (``gds_history_utc_timestamps``).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def job_staleness(
    job: GDSJobSpec | None,
    last_run: datetime | None,
    now: datetime,
    policy: StalenessPolicy,
) -> JobStaleness:
    """Whether ``job``, last run at ``last_run``, is stale at ``now``."""
    if last_run is None:
        return JobStaleness(stale=True, deadline=None)
    if not policy.per_schedule or job is None:
        return _by_age(last_run, now, policy)
    schedule_type = job.schedule.type
    if schedule_type == "manual":
        return JobStaleness(stale=False, deadline=None)
    if schedule_type != "cron":
        return _by_age(last_run, now, policy)
    next_fire = next_fire_after(job.schedule.cron, last_run)
    if next_fire is None:
        return JobStaleness(stale=True, deadline=None)
    deadline = next_fire + policy.grace
    return JobStaleness(stale=now > deadline, deadline=deadline)


def _by_age(last_run: datetime, now: datetime, policy: StalenessPolicy) -> JobStaleness:
    deadline = last_run + policy.max_staleness
    return JobStaleness(stale=now > deadline, deadline=deadline)


def next_fire_after(cron: str | None, last_run: datetime) -> datetime | None:
    """The first fire of ``cron`` strictly after ``last_run``, or None when unreadable.

    Built with ``crontab_trigger`` exactly as the scheduler registers the job,
    so a deadline and a due check here agree with when APScheduler fires.
    """
    if not cron:
        return None
    try:
        trigger = crontab_trigger(cron)
    except ValueError:
        return None
    fire = trigger.get_next_fire_time(None, last_run + _PAST_LAST_RUN)
    return fire if isinstance(fire, datetime) else None


SUCCESS = "success"
NEVER_RUN = "never_run"


@dataclass(frozen=True, slots=True)
class JobHealth:
    """One job's health: its latest attempt and the freshness of its latest success."""

    job: str
    healthy: bool
    last_attempt_status: str
    last_attempt: str | None
    last_success: str | None
    stale: bool
    deadline: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form; ``status``/``last_run`` keep the existing admin field names."""
        return {
            "status": self.last_attempt_status,
            "last_run": self.last_attempt,
            "last_success": self.last_success,
            "healthy": self.healthy,
            "stale": self.stale,
            "stale_after": self.deadline.isoformat() if self.deadline else None,
        }


def assess_gds_health(
    jobs: list[GDSJobSpec],
    history: list[dict[str, Any]],
    now: datetime,
    policy: StalenessPolicy,
) -> dict[str, JobHealth]:
    """Per-job health for every declared job and every job named in ``history``.

    ``history`` is oldest first (``GDSScheduler.get_job_history``). Entries are
    keyed by ``job`` (falling back to ``algorithm`` for an entry without one),
    so two jobs sharing an algorithm are judged separately. Declared jobs come
    first in declaration order.
    """
    declared = {job.name: job for job in jobs}
    latest: dict[str, dict[str, Any]] = {}
    latest_success: dict[str, dict[str, Any]] = {}
    for entry in history:
        key = entry.get("job") or entry.get("algorithm") or "unknown"
        latest[key] = entry
        if entry.get("status") == SUCCESS:
            latest_success[key] = entry

    names = list(declared) + [name for name in latest if name not in declared]
    verdicts: dict[str, JobHealth] = {}
    for name in names:
        attempt = latest.get(name)
        success = latest_success.get(name)
        success_ts = success.get("timestamp") if success else None
        staleness = job_staleness(declared.get(name), parse_history_timestamp(success_ts), now, policy)
        status = str(attempt.get("status")) if attempt else NEVER_RUN
        verdicts[name] = JobHealth(
            job=name,
            healthy=status == SUCCESS and not staleness.stale,
            last_attempt_status=status,
            last_attempt=attempt.get("timestamp") if attempt else None,
            last_success=success_ts if isinstance(success_ts, str) else None,
            stale=staleness.stale,
            deadline=staleness.deadline,
        )
    return verdicts
