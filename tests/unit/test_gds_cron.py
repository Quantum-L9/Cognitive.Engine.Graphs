"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, scheduler, cron, staleness]
owner: engine-team
status: active
--- /L9_META ---

GDS-CRON-01: a GDS job's cron reads day_of_week as standard crontab (0 = Sunday).
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.config.settings import settings
from engine.gds import cron as gds_cron
from engine.gds import staleness as gds_staleness
from engine.gds.cron import crontab_trigger, posix_day_of_week
from engine.gds.scheduler import GDSScheduler
from engine.gds.staleness import StalenessPolicy, job_staleness

pytestmark = pytest.mark.unit

# 2026-11-14 is a Saturday.
SATURDAY = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
SUNDAY = 6  # datetime.weekday()
WEEKEND = {5, 6}


def _next_fires(trigger: CronTrigger, start: datetime, count: int) -> list[datetime]:
    fires: list[datetime] = []
    previous: datetime | None = None
    now = start
    for _ in range(count):
        fire = trigger.get_next_fire_time(previous, now)
        assert isinstance(fire, datetime)
        fires.append(fire)
        previous = fire
        now = fire + timedelta(seconds=1)
    return fires


def _job(cron: str) -> GDSJobSpec:
    return GDSJobSpec(
        name="weekly_job",
        algorithm="louvain",
        schedule=GDSJobScheduleSpec(type="cron", cron=cron),
        projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["INTERACTED_WITH"]),
    )


class TestWeekdayMapping:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("*", "*"),
            ("0", "sun"),
            ("7", "sun"),
            ("1", "mon"),
            ("6", "sat"),
            ("1-5", "mon,tue,wed,thu,fri"),
            ("5-7", "fri,sat,sun"),
            ("0-6", "mon,tue,wed,thu,fri,sat,sun"),
            ("0,7", "sun"),
            ("1,3,5", "mon,wed,fri"),
            ("*/2", "tue,thu,sat,sun"),
            ("*/7", "sun"),
            ("1-5/2", "mon,wed,fri"),
            ("1/3", "mon,thu,sun"),
            ("mon", "mon"),
            ("sun", "sun"),
            ("mon-fri", "mon-fri"),
            ("0,sat", "sun,sat"),
        ],
    )
    def test_mapping(self, field: str, expected: str) -> None:
        assert posix_day_of_week(field) == expected

    @pytest.mark.parametrize("field", ["8", "3-1", "1-9", "*/0", "1,,2", "", "-1", "1-"])
    def test_malformed_weekday_raises(self, field: str) -> None:
        with pytest.raises(ValueError):
            posix_day_of_week(field)

    @pytest.mark.parametrize("field", ["*/x", "funday"])
    def test_malformed_name_term_is_rejected_by_apscheduler(self, field: str) -> None:
        with pytest.raises(ValueError):
            crontab_trigger(f"0 2 * * {field}", posix_weekday=True)

    @pytest.mark.parametrize("expr", ["0 2 * *", "0 2 * * 0 0", ""])
    def test_wrong_field_count_raises(self, expr: str) -> None:
        with pytest.raises(ValueError, match="Wrong number of fields"):
            crontab_trigger(expr, posix_weekday=True)

    def test_other_fields_pass_through(self) -> None:
        trigger = crontab_trigger("*/15 1-3 2,9 6 *", posix_weekday=True)
        reference = CronTrigger.from_crontab("*/15 1-3 2,9 6 *")
        assert str(trigger) == str(reference)


class TestFireTimes:
    def test_sunday_expression_fires_on_sunday(self) -> None:
        trigger = crontab_trigger("0 2 * * 0", "UTC", posix_weekday=True)
        for fire in _next_fires(trigger, SATURDAY, 4):
            assert fire.weekday() == SUNDAY
            assert (fire.hour, fire.minute) == (2, 0)

    def test_seven_is_sunday_too(self) -> None:
        trigger = crontab_trigger("0 2 * * 7", "UTC", posix_weekday=True)
        assert {fire.weekday() for fire in _next_fires(trigger, SATURDAY, 4)} == {SUNDAY}

    def test_weekdays_expression_never_fires_on_a_weekend(self) -> None:
        trigger = crontab_trigger("0 2 * * 1-5", "UTC", posix_weekday=True)
        fires = _next_fires(trigger, SATURDAY, 15)
        assert {fire.weekday() for fire in fires} == {0, 1, 2, 3, 4}
        assert not {fire.weekday() for fire in fires} & WEEKEND

    def test_from_crontab_is_the_defect(self) -> None:
        """Documents the APScheduler 3 numbering the flag corrects."""
        fire = CronTrigger.from_crontab("0 3 * * 0", "UTC").get_next_fire_time(None, SATURDAY)
        assert fire is not None
        assert fire.weekday() == 0  # Monday

    def test_default_follows_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_cron_posix_weekday_enabled", True)
        on = crontab_trigger("0 2 * * 0", "UTC").get_next_fire_time(None, SATURDAY)
        monkeypatch.setattr(settings, "gds_cron_posix_weekday_enabled", False)
        off = crontab_trigger("0 2 * * 0", "UTC").get_next_fire_time(None, SATURDAY)
        assert on is not None
        assert on.weekday() == SUNDAY
        assert off is not None
        assert off.weekday() == 0


class TestFlagOff:
    @pytest.mark.parametrize(
        "expr",
        ["0 2 * * 0", "0 2 * * 1-5", "*/5 * * * *", "0 3 * * mon", "0 3 1 */2 *", "0 2 * * */2"],
    )
    def test_off_is_from_crontab(self, expr: str) -> None:
        trigger = crontab_trigger(expr, "UTC", posix_weekday=False)
        reference = CronTrigger.from_crontab(expr, "UTC")
        assert type(trigger) is type(reference)
        assert str(trigger) == str(reference)
        assert repr(trigger) == repr(reference)
        assert _next_fires(trigger, SATURDAY, 5) == _next_fires(reference, SATURDAY, 5)

    def test_off_delegates_to_from_crontab(self) -> None:
        with patch.object(CronTrigger, "from_crontab", wraps=CronTrigger.from_crontab) as spy:
            crontab_trigger("0 2 * * 0", posix_weekday=False)
        spy.assert_called_once_with("0 2 * * 0", None)

    def test_off_raises_like_from_crontab(self) -> None:
        with pytest.raises(ValueError, match="Wrong number of fields"):
            crontab_trigger("0 2 * *", posix_weekday=False)


@pytest.fixture
def utc_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build staleness triggers in UTC: crontab_trigger defaults to the host zone, these deadlines are UTC."""
    monkeypatch.setattr(gds_staleness, "crontab_trigger", functools.partial(gds_cron.crontab_trigger, timezone=UTC))


class TestStalenessUsesTheSameRule:
    POLICY = StalenessPolicy(max_staleness=timedelta(hours=25), grace=timedelta(hours=1), per_schedule=True)

    @pytest.mark.usefixtures("utc_cron")
    def test_deadline_is_the_posix_sunday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_cron_posix_weekday_enabled", True)
        verdict = job_staleness(_job("0 2 * * 0"), SATURDAY, SATURDAY, self.POLICY)
        assert verdict.deadline == datetime(2026, 11, 15, 2, 0, tzinfo=UTC) + timedelta(hours=1)

    @pytest.mark.usefixtures("utc_cron")
    def test_deadline_off_matches_from_crontab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_cron_posix_weekday_enabled", False)
        verdict = job_staleness(_job("0 2 * * 0"), SATURDAY, SATURDAY, self.POLICY)
        assert verdict.deadline == datetime(2026, 11, 16, 2, 0, tzinfo=UTC) + timedelta(hours=1)

    def test_staleness_builds_through_the_helper(self) -> None:
        with patch("engine.gds.staleness.crontab_trigger", wraps=gds_cron.crontab_trigger) as spy:
            job_staleness(_job("0 2 * * 0"), SATURDAY, SATURDAY, self.POLICY)
        spy.assert_called_once_with("0 2 * * 0")


class TestSchedulerUsesTheHelper:
    def test_register_cron_job_builds_through_the_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_cron_posix_weekday_enabled", True)
        scheduler = GDSScheduler(MagicMock(spec=DomainSpec), MagicMock())
        with (
            patch("engine.gds.scheduler.crontab_trigger", wraps=gds_cron.crontab_trigger) as spy,
            patch.object(scheduler.scheduler, "add_job") as add_job,
        ):
            scheduler._register_cron_job(_job("0 2 * * 0"))
        spy.assert_called_once_with("0 2 * * 0")
        trigger = add_job.call_args.kwargs["trigger"]
        fire = trigger.get_next_fire_time(None, SATURDAY.astimezone(trigger.timezone))
        assert fire.weekday() == SUNDAY
