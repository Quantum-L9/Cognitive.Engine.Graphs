"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, health, staleness]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 F06 / F07 (cases G14, G15, G17, G18): a job is healthy
only when its latest attempt succeeded and its latest success is fresh.

``engine.gds.staleness.assess_gds_health`` is the shared judgement the admin
``gds_health`` subaction and the boot W6-03 probe are to consume (that wiring
is integration work in engine/handlers.py and engine/boot.py, outside this
lane).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.config.schema import GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.gds.staleness import StalenessPolicy, assess_gds_health

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
POLICY = StalenessPolicy(max_staleness=timedelta(hours=25), grace=timedelta(minutes=60), per_schedule=True)


def _job(name: str, algorithm: str = "louvain", cron: str | None = "0 * * * *") -> GDSJobSpec:
    return GDSJobSpec(
        name=name,
        algorithm=algorithm,
        schedule=GDSJobScheduleSpec(type="cron" if cron else "manual", cron=cron),
        projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["LINKS"]),
    )


def _entry(job: str, status: str, minutes_ago: int, algorithm: str = "louvain") -> dict[str, str]:
    return {
        "job": job,
        "algorithm": algorithm,
        "status": status,
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }


class TestAssessGdsHealth:
    def test_g14_recent_failure_after_fresh_success_is_not_healthy(self) -> None:
        history = [_entry("hourly", "success", 30), _entry("hourly", "failed", 5)]
        health = assess_gds_health([_job("hourly")], history, NOW, POLICY)
        verdict = health["hourly"]
        assert not verdict.healthy
        assert verdict.last_attempt_status == "failed"
        assert not verdict.stale  # the success is fresh; the failure alone degrades it
        assert verdict.last_success == history[0]["timestamp"]

    def test_g15_fresh_skip_is_not_completed_execution(self) -> None:
        history = [_entry("sim", "skipped", 1, algorithm="nodeSimilarity")]
        verdict = assess_gds_health([_job("sim", "nodeSimilarity")], history, NOW, POLICY)["sim"]
        assert not verdict.healthy
        assert verdict.last_attempt_status == "skipped"
        assert verdict.stale  # no success ever recorded
        assert verdict.last_success is None

    @pytest.mark.parametrize("status", ["blocked", "refused", "partial"])
    def test_non_success_attempts_are_unhealthy(self, status: str) -> None:
        history = [_entry("hourly", "success", 20), _entry("hourly", status, 2)]
        assert not assess_gds_health([_job("hourly")], history, NOW, POLICY)["hourly"].healthy

    def test_g17_empty_history_after_boot_claims_nothing(self) -> None:
        health = assess_gds_health([_job("hourly"), _job("weekly", cron="0 2 * * 0")], [], NOW, POLICY)
        assert {name: v.last_attempt_status for name, v in health.items()} == {
            "hourly": "never_run",
            "weekly": "never_run",
        }
        assert not any(v.healthy for v in health.values())

    def test_g18_manual_job_failure_after_valid_run_is_not_silently_healthy(self) -> None:
        manual = _job("on_demand", cron=None)
        history = [_entry("on_demand", "success", 60 * 24 * 30), _entry("on_demand", "failed", 10)]
        verdict = assess_gds_health([manual], history, NOW, POLICY)["on_demand"]
        assert not verdict.stale  # manual age is irrelevant
        assert not verdict.healthy  # but the failed last attempt is never healthy

    def test_manual_job_with_successful_last_attempt_is_healthy(self) -> None:
        verdict = assess_gds_health([_job("m", cron=None)], [_entry("m", "success", 60 * 24 * 90)], NOW, POLICY)["m"]
        assert verdict.healthy

    def test_fresh_success_is_healthy(self) -> None:
        verdict = assess_gds_health([_job("hourly")], [_entry("hourly", "success", 10)], NOW, POLICY)["hourly"]
        assert verdict.healthy
        assert not verdict.stale

    def test_stale_success_is_unhealthy(self) -> None:
        verdict = assess_gds_health([_job("hourly")], [_entry("hourly", "success", 60 * 5)], NOW, POLICY)["hourly"]
        assert verdict.stale
        assert not verdict.healthy

    def test_two_jobs_sharing_one_algorithm_are_judged_separately(self) -> None:
        jobs = [_job("louvain_a"), _job("louvain_b")]
        history = [_entry("louvain_a", "success", 5), _entry("louvain_b", "failed", 3)]
        health = assess_gds_health(jobs, history, NOW, POLICY)
        assert health["louvain_a"].healthy
        assert not health["louvain_b"].healthy

    def test_history_for_an_undeclared_job_is_reported(self) -> None:
        health = assess_gds_health([], [_entry("retired", "success", 5)], NOW, POLICY)
        assert health["retired"].healthy  # global age, fresh

    def test_as_dict_is_json_ready(self) -> None:
        verdict = assess_gds_health([_job("hourly")], [_entry("hourly", "success", 10)], NOW, POLICY)["hourly"]
        payload = verdict.as_dict()
        assert payload["status"] == "success"
        assert payload["healthy"] is True
        assert isinstance(payload["stale_after"], str)
