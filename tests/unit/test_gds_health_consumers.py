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

CEG-NATIVE-CLOSURE-001 G16 and N3 action 3: what the admin trigger and the
boot lifecycle already guarantee.

* G16 — a trigger of a failing job is reported as an attempt (``triggered``)
  whose ``result.status`` is the job's own ``failed``; nothing claims success.
* One scheduler per domain across a restart of the lifecycle: the second
  startup registers each cron job once on a new scheduler and the first is
  stopped.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine import boot
from engine.config.settings import settings
from engine.handlers import handle_admin
from engine.state import get_state
from tests.unit.test_gds_scheduler_registry import CRON_JOB, DOMAIN_ID, _Probe, lifecycle_env, probe

__all__ = ["lifecycle_env", "probe"]  # fixtures re-used from the registry suite

pytestmark = pytest.mark.unit


async def _admin(subaction: str, **payload: Any) -> dict[str, Any]:
    return await handle_admin(DOMAIN_ID, {"subaction": subaction, "domain_id": DOMAIN_ID, **payload})


async def test_g16_failed_trigger_is_an_attempt_not_a_success(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
) -> None:
    monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
    lifecycle = boot.GraphLifecycle()
    await lifecycle.startup()
    try:
        scheduler = get_state().gds_schedulers[DOMAIN_ID]
        scheduler.graph_driver = MagicMock()
        scheduler.graph_driver.execute_query = AsyncMock(side_effect=RuntimeError("gds unavailable"))
        for subaction in ("gds_trigger", "trigger_gds"):
            response = await _admin(subaction, job_name=CRON_JOB)
            assert response["status"] == "triggered"
            assert response["result"]["status"] == "failed"
            assert "gds unavailable" in response["result"]["error"]
        status = await _admin("gds_status")
    finally:
        await lifecycle.shutdown()
    assert status["last_runs"][CRON_JOB]["status"] == "failed"


async def test_restart_keeps_one_scheduler_per_domain(
    monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
) -> None:
    monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
    first = boot.GraphLifecycle()
    await first.startup()
    await _admin("gds_status")
    await first.shutdown()
    second = boot.GraphLifecycle()
    await second.startup()
    try:
        await _admin("gds_status")
        await asyncio.sleep(0)  # AsyncIOScheduler.shutdown completes on the next loop iteration
        assert list(get_state().gds_schedulers) == [DOMAIN_ID]
        live = [s for s in probe.created if s.scheduler.running]
        assert live == [get_state().gds_schedulers[DOMAIN_ID]]
        assert probe.registrations[CRON_JOB] == 2  # once per startup, never twice in one
    finally:
        await second.shutdown()
    assert await probe.still_running() == 0
