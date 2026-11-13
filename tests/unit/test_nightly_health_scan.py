"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, health, nightly-scan]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_nightly_health_scan.py
Unit tests for engine/health/nightly_health_scan.run_nightly_health_scan.

Findings covered:
  CEG-COR-101  de-dup state is local to each run (C-24); a concurrent run
               must not see another run's entity ids
  CEG-COR-102  total_enriched + total_skipped == total_assessed, entities beyond
               MAX_ENRICHMENT_QUEUE counted as skipped

Candidate entities come from ``entity_loader`` callbacks; the checked-in
plasticos pack supplies the spec. No Neo4j.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.health import nightly_health_scan
from engine.health.health_schemas import HealthScanProfile
from engine.health.nightly_health_scan import run_nightly_health_scan
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

DOMAIN = "plasticos"


@pytest.fixture(autouse=True)
def _design_pack_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan builds its own DomainPackLoader; name the repository design packs as its root."""
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(DESIGN_PACKS_ROOT))


def _entity(entity_id: str) -> dict[str, Any]:
    return {"entity_id": entity_id, "node_label": "Facility", "data": {"facility_id": entity_id}}


@pytest.mark.finding("CEG-COR-101")
class TestDedupIsLocalToEachRun:
    async def test_concurrent_runs_each_assess_their_own_candidates(self) -> None:
        b_loading = asyncio.Event()
        a_finished = asyncio.Event()

        async def loader_a(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            await b_loading.wait()  # B has started (and, before the fix, cleared the shared set)
            return [_entity("shared-1")]

        async def loader_b(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            b_loading.set()
            await a_finished.wait()  # A has assessed shared-1 before B looks at it
            return [_entity("shared-1")]

        async def run_a() -> dict[str, Any]:
            try:
                return await run_nightly_health_scan(DOMAIN, entity_loader=loader_a)
            finally:
                a_finished.set()

        result_a, result_b = await asyncio.gather(run_a(), run_nightly_health_scan(DOMAIN, entity_loader=loader_b))

        assert result_a["total_assessed"] == 1
        assert result_b["total_assessed"] == 1

    async def test_within_run_dedup_and_exclusions_hold(self) -> None:
        async def loader(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            return [_entity("e1"), _entity("e1"), _entity("e2"), _entity("e3")]

        result = await run_nightly_health_scan(
            DOMAIN, scan_profile=HealthScanProfile(exclude_entity_ids=["e3"]), entity_loader=loader
        )
        assert result["total_candidates"] == 4
        assert result["total_assessed"] == 2

    async def test_sequential_runs_do_not_leak_state(self) -> None:
        async def loader(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            return [_entity("e1"), _entity("e2")]

        first = await run_nightly_health_scan(DOMAIN, entity_loader=loader)
        second = await run_nightly_health_scan(DOMAIN, entity_loader=loader)
        assert first["total_assessed"] == second["total_assessed"] == 2


@pytest.mark.finding("CEG-COR-102")
class TestCountersPartitionTheAssessedSet:
    async def test_entities_beyond_the_queue_are_skipped(self) -> None:
        async def loader(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            return [_entity(f"e{i}") for i in range(150)]

        result = await run_nightly_health_scan(DOMAIN, cost_ceiling_usd=1e9, entity_loader=loader)

        assert result["total_assessed"] == 150
        assert result["total_enriched"] + result["total_skipped"] == 150
        assert len(result["enrichment_queue"]) <= nightly_health_scan.MAX_ENRICHMENT_QUEUE

    async def test_ceiling_branch_arithmetic_unchanged(self) -> None:
        async def loader(domain: str, profile: HealthScanProfile) -> list[dict[str, Any]]:
            return [_entity(f"e{i}") for i in range(150)]

        result = await run_nightly_health_scan(DOMAIN, cost_ceiling_usd=0.0, entity_loader=loader)

        assert result["total_assessed"] == 150
        assert result["total_enriched"] == 0
        assert result["total_skipped"] == 150


@pytest.fixture(autouse=True)
def _design_pack_settings_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the design packs as Settings.domains_root (DOMAINS_ROOT), the loader's only root.

    The scan's loader is built with an explicit root, which wins over the
    DOMAIN_SPECS_PATH alias the fixture above sets; this one names that root.
    """
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "domains_root", DESIGN_PACKS_ROOT)
