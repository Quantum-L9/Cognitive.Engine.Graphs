"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, health, admin, boot]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 F06 / F07 witnesses (G14, G15, G17, G18, shared
algorithm): admin ``gds_health`` and the boot W6-03 probe judge a job by its
latest attempt and the freshness of its latest success
(``engine.gds.staleness.assess_gds_health``), behind ``gds_health_last_attempt_enabled``. Each fails on 9d17d01.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine import boot
from engine.config.settings import settings
from engine.handlers import handle_admin
from engine.state import get_state
from tests.unit.test_gds_scheduler import _job, _minimal_spec
from tests.unit.test_gds_scheduler_registry import CRON_JOB, DOMAIN_ID, _Probe, lifecycle_env, probe

__all__ = ["lifecycle_env", "probe"]

pytestmark = pytest.mark.unit


def _entry(job: str, status: str, minutes_ago: float, algorithm: str = "louvain") -> dict[str, Any]:
    ts = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return {"job": job, "algorithm": algorithm, "status": status, "timestamp": ts.isoformat()}


async def _health() -> dict[str, Any]:
    return await handle_admin(DOMAIN_ID, {"subaction": "gds_health", "domain_id": DOMAIN_ID})


@pytest.fixture
def hourly(lifecycle_env: MagicMock) -> MagicMock:
    """The lifecycle loader, serving one hourly cron louvain job."""
    spec = _minimal_spec(
        [_job(CRON_JOB, "louvain", cron="0 * * * *", writeproperty="community_id")], domain_id=DOMAIN_ID
    )
    lifecycle_env.load_domain.return_value = spec
    return lifecycle_env


async def _started(monkeypatch: pytest.MonkeyPatch) -> boot.GraphLifecycle:
    monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
    lifecycle = boot.GraphLifecycle()
    await lifecycle.startup()
    return lifecycle


async def test_g14_recent_failure_after_fresh_success_is_degraded(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, hourly: MagicMock
) -> None:
    lifecycle = await _started(monkeypatch)
    try:
        history = get_state().gds_schedulers[DOMAIN_ID]._job_history
        history.extend([_entry(CRON_JOB, "success", 30), _entry(CRON_JOB, "failed", 5)])
        health = await _health()
        probe_result = await lifecycle._gds_health_check()
    finally:
        await lifecycle.shutdown()
    assert health["status"] == "degraded"
    assert health["algorithms"][CRON_JOB]["status"] == "failed"
    assert health["algorithms"][CRON_JOB]["healthy"] is False
    assert probe_result["gds"].startswith("degraded")
    assert CRON_JOB in probe_result["gds"]


async def test_g15_fresh_skip_is_not_completed_execution(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, hourly: MagicMock
) -> None:
    lifecycle = await _started(monkeypatch)
    try:
        get_state().gds_schedulers[DOMAIN_ID]._job_history.append(_entry(CRON_JOB, "skipped", 1))
        health = await _health()
        probe_result = await lifecycle._gds_health_check()
    finally:
        await lifecycle.shutdown()
    assert health["status"] == "degraded"
    assert health["algorithms"][CRON_JOB]["status"] == "skipped"
    assert probe_result["gds"] != "ok"


async def test_g17_boot_probe_with_empty_history_claims_no_completion(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, hourly: MagicMock
) -> None:
    lifecycle = await _started(monkeypatch)
    try:
        probe_result = await lifecycle._gds_health_check()
    finally:
        await lifecycle.shutdown()
    assert probe_result["gds"] != "ok"
    assert CRON_JOB in probe_result["gds"]


async def test_g18_manual_job_failed_after_earlier_success_is_degraded(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
) -> None:
    manual = _job("on_demand", "louvain", schedule_type="manual", writeproperty="community_id")
    lifecycle_env.load_domain.return_value = _minimal_spec([manual], domain_id=DOMAIN_ID)
    lifecycle = await _started(monkeypatch)
    try:
        scheduler = get_state().gds_schedulers[DOMAIN_ID]
        scheduler._job_history.extend([_entry("on_demand", "success", 60 * 24 * 30), _entry("on_demand", "failed", 10)])
        health = await _health()
    finally:
        await lifecycle.shutdown()
    assert health["status"] == "degraded"
    assert health["algorithms"]["on_demand"]["stale"] is False  # manual age is irrelevant
    assert health["algorithms"]["on_demand"]["healthy"] is False


async def test_two_jobs_sharing_an_algorithm_are_judged_separately(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
) -> None:
    jobs = [
        _job("louvain_a", "louvain", cron="0 * * * *", writeproperty="community_a"),
        _job("louvain_b", "louvain", cron="0 * * * *", writeproperty="community_b"),
    ]
    lifecycle_env.load_domain.return_value = _minimal_spec(jobs, domain_id=DOMAIN_ID)
    lifecycle = await _started(monkeypatch)
    try:
        history = get_state().gds_schedulers[DOMAIN_ID]._job_history
        history.extend([_entry("louvain_a", "success", 5), _entry("louvain_b", "failed", 3)])
        health = await _health()
        probe_result = await lifecycle._gds_health_check()
    finally:
        await lifecycle.shutdown()
    assert health["algorithms"]["louvain_a"]["healthy"] is True
    assert health["algorithms"]["louvain_b"]["healthy"] is False
    assert health["status"] == "degraded"
    assert "louvain_b" in probe_result["gds"]
    assert "louvain_a" not in probe_result["gds"]


async def test_flag_off_restores_newest_entry_verdict(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, hourly: MagicMock
) -> None:
    monkeypatch.setattr(settings, "gds_health_last_attempt_enabled", False)
    lifecycle = await _started(monkeypatch)
    try:
        get_state().gds_schedulers[DOMAIN_ID]._job_history.append(_entry(CRON_JOB, "failed", 5))
        health = await _health()
        probe_result = await lifecycle._gds_health_check()
    finally:
        await lifecycle.shutdown()
    assert health["status"] == "healthy"  # legacy: the newest entry's timestamp alone decides
    assert probe_result == {"gds": "ok"}


def test_flag_default_on() -> None:
    from engine.config.settings import Settings

    assert Settings.model_fields["gds_health_last_attempt_enabled"].default is True
