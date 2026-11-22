"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, staleness, health]
owner: engine-team
status: active
--- /L9_META ---

GDS-STALE-01: a GDS job is stale when it misses its own schedule, not one global age.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime, timedelta

import pytest

from engine.config.schema import GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.config.settings import settings
from engine.gds import cron as gds_cron
from engine.gds import staleness as gds_staleness
from engine.gds.staleness import StalenessPolicy, job_staleness, parse_history_timestamp

pytestmark = pytest.mark.unit

GLOBAL_AGE = timedelta(hours=25)
GRACE = timedelta(hours=1)
PER_SCHEDULE = StalenessPolicy(max_staleness=GLOBAL_AGE, grace=GRACE, per_schedule=True)
BY_AGE = StalenessPolicy(max_staleness=GLOBAL_AGE, grace=GRACE, per_schedule=False)

# 2026-11-14 is a Saturday.
LAST_RUN = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)


def _job(schedule_type: str, cron: str | None = None) -> GDSJobSpec:
    return GDSJobSpec(
        name="louvain_community",
        algorithm="louvain",
        schedule=GDSJobScheduleSpec(type=schedule_type, cron=cron),
        projection=GDSProjectionSpec(nodelabels=["Facility"], edgetypes=["SUPPLIES"]),
    )


@pytest.fixture
def utc_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build triggers in UTC: crontab_trigger defaults to the host zone, these expectations are UTC."""
    monkeypatch.setattr(gds_staleness, "crontab_trigger", functools.partial(gds_cron.crontab_trigger, timezone=UTC))


@pytest.mark.usefixtures("utc_cron")
class TestCronSchedule:
    def test_hourly_job_is_fresh_until_one_missed_run_plus_grace(self) -> None:
        job = _job("cron", "0 * * * *")
        deadline = LAST_RUN + timedelta(hours=1) + GRACE
        assert job_staleness(job, LAST_RUN, deadline, PER_SCHEDULE).stale is False
        later = job_staleness(job, LAST_RUN, deadline + timedelta(seconds=1), PER_SCHEDULE)
        assert later.stale is True
        assert later.deadline == deadline

    def test_hourly_job_goes_stale_well_before_the_global_age(self) -> None:
        job = _job("cron", "0 * * * *")
        now = LAST_RUN + timedelta(hours=3)
        assert job_staleness(job, LAST_RUN, now, PER_SCHEDULE).stale is True
        assert job_staleness(job, LAST_RUN, now, BY_AGE).stale is False

    def test_weekly_job_stays_fresh_past_the_global_age(self) -> None:
        job = _job("cron", "0 3 * * 5")
        now = LAST_RUN + timedelta(days=3)
        assert job_staleness(job, LAST_RUN, now, PER_SCHEDULE).stale is False
        assert job_staleness(job, LAST_RUN, now, BY_AGE).stale is True

    def test_a_run_exactly_on_its_fire_time_is_measured_against_the_next_fire(self) -> None:
        job = _job("cron", "0 3 * * *")
        verdict = job_staleness(job, LAST_RUN, LAST_RUN + timedelta(hours=2), PER_SCHEDULE)
        assert verdict.stale is False
        assert verdict.deadline == LAST_RUN + timedelta(days=1) + GRACE

    def test_unparsable_cron_is_stale(self) -> None:
        job = _job("cron", "not a cron")
        verdict = job_staleness(job, LAST_RUN, LAST_RUN + timedelta(minutes=1), PER_SCHEDULE)
        assert verdict.stale is True
        assert verdict.deadline is None

    def test_cron_type_without_expression_is_stale(self) -> None:
        assert job_staleness(_job("cron"), LAST_RUN, LAST_RUN, PER_SCHEDULE).stale is True


class TestOtherSchedules:
    def test_manual_job_is_never_stale_by_age(self) -> None:
        verdict = job_staleness(_job("manual"), LAST_RUN, LAST_RUN + timedelta(days=365), PER_SCHEDULE)
        assert verdict.stale is False
        assert verdict.deadline is None

    def test_unknown_schedule_type_uses_the_global_age(self) -> None:
        job = _job("interval")
        assert job_staleness(job, LAST_RUN, LAST_RUN + timedelta(hours=24), PER_SCHEDULE).stale is False
        assert job_staleness(job, LAST_RUN, LAST_RUN + timedelta(hours=26), PER_SCHEDULE).stale is True

    def test_job_absent_from_the_spec_uses_the_global_age(self) -> None:
        assert job_staleness(None, LAST_RUN, LAST_RUN + timedelta(hours=26), PER_SCHEDULE).stale is True
        assert job_staleness(None, LAST_RUN, LAST_RUN + timedelta(hours=24), PER_SCHEDULE).stale is False

    def test_never_run_is_stale_under_both_policies(self) -> None:
        for policy in (PER_SCHEDULE, BY_AGE):
            assert job_staleness(_job("manual"), None, LAST_RUN, policy).stale is True


class TestFlagOff:
    def test_off_ignores_the_schedule(self) -> None:
        job = _job("manual")
        assert job_staleness(job, LAST_RUN, LAST_RUN + GLOBAL_AGE, BY_AGE).stale is False
        assert job_staleness(job, LAST_RUN, LAST_RUN + GLOBAL_AGE + timedelta(seconds=1), BY_AGE).stale is True

    def test_policy_reads_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_max_staleness_hours", 7)
        monkeypatch.setattr(settings, "gds_staleness_grace_minutes", 15)
        monkeypatch.setattr(settings, "gds_staleness_per_schedule_enabled", False)
        policy = StalenessPolicy.from_settings()
        assert policy == StalenessPolicy(
            max_staleness=timedelta(hours=7), grace=timedelta(minutes=15), per_schedule=False
        )


class TestHistoryTimestamp:
    def test_naive_timestamp_is_read_as_utc(self) -> None:
        assert parse_history_timestamp("2026-11-14T03:00:00") == LAST_RUN

    def test_aware_timestamp_is_kept(self) -> None:
        assert parse_history_timestamp(LAST_RUN.isoformat()) == LAST_RUN

    @pytest.mark.parametrize("value", [None, "", "yesterday", 42])
    def test_unreadable_timestamp_is_none(self, value: object) -> None:
        assert parse_history_timestamp(value) is None


def test_reference_just_before_a_fire_does_not_skip_it() -> None:
    """CodeRabbit on #543: APScheduler rounds the search start up to a whole second."""
    from engine.gds.staleness import next_fire_after

    # Every-minute cron: its fires are minute boundaries in any host zone (all
    # real UTC offsets are whole minutes), so the expected instants hold off UTC.
    reference = datetime(2026, 11, 22, 11, 59, 59, 500000, tzinfo=UTC)
    assert next_fire_after("* * * * *", reference) == datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    exact = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    assert next_fire_after("* * * * *", exact) == datetime(2026, 11, 22, 12, 1, tzinfo=UTC)
