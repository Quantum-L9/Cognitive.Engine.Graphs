"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, identity, outcomes, resolve, enrich]
owner: engine-team
status: active
--- /L9_META ---

RU-002 / CEG-COR-002: every non-match node lookup uses the declared id property.

Stage 2 made ``handle_match`` identify candidates by the domain's declared id
(sync ``idproperty``, else first required property). The outcome edges, the
legacy resolve path, enrich-by-id and admin ``evaluate`` still read a hardcoded
``entity_id`` — a property no served pack declares — so outcome edges never
linked, legacy resolve never matched, enrich-by-id enriched nothing and
evaluate scored every case 0. The fake driver captures the generated Cypher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.handlers import (
    handle_admin,
    handle_enrich,
    handle_outcomes,
    handle_resolve,
    handle_sync,
    init_dependencies,
)

DOMAINS = Path(__file__).resolve().parents[2] / "domains"

# domain -> (candidate label, declared id property, a match direction)
PACKS = {
    "plasticos": ("Facility", "facility_id", "supply_opportunity_to_buyer_facility"),
    "freight-matching": ("Carrier", "carrierid", "loadtocarrier"),
}

# RU-010 (R1a-DEC-03): each pack's required gate parameters for its direction.
_REQUIRED_QUERY: dict[str, dict[str, Any]] = {
    "plasticos": {"polymer_type": "HDPE"},
    "freight-matching": {"weightlbs": 1000.0, "equipmenttype": "van"},
}


class _Capture:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append((cypher, parameters))
        return [dict(r) for r in self.rows]


def _load(domain: str) -> DomainSpec:
    return DomainPackLoader(config_path=str(DOMAINS)).load_domain(domain)


def _wire(spec: DomainSpec, rows: list[dict[str, Any]] | None = None) -> _Capture:
    driver = _Capture(rows)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [spec.domain.id]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


@pytest.fixture(params=sorted(PACKS), ids=str)
def pack(request: pytest.FixtureRequest) -> tuple[str, str, str, str]:
    label, id_prop, direction = PACKS[request.param]
    return request.param, label, id_prop, direction


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-002")
class TestDeclaredNodeIdentity:
    async def test_outcomes_link_by_declared_id_scoped_to_direction(self, pack) -> None:
        domain, label, id_prop, direction = pack
        driver = _wire(_load(domain))
        await handle_outcomes(
            domain,
            {
                "match_id": "m1",
                "candidate_id": "C-1",
                "outcome": "success",
                "fingerprint": {"match_direction": direction},
            },
        )
        cypher = driver.calls[0][0]
        assert f"(c:{label} AND c.{id_prop} = $candidate_id)" in cypher
        assert "entity_id" not in cypher

    async def test_outcomes_without_direction_cover_every_candidate_label(self) -> None:
        driver = _wire(_load("plasticos"))
        await handle_outcomes("plasticos", {"match_id": "m1", "candidate_id": "C-1", "outcome": "success"})
        cypher = driver.calls[0][0]
        assert "(c:Facility AND c.facility_id = $candidate_id) OR " in cypher
        assert "(c:SupplyOpportunity AND c.opportunity_id = $candidate_id)" in cypher
        assert "entity_id" not in cypher
        # The outcome node itself is still written when no candidate matches.
        assert "CREATE (o:" in cypher
        assert "OPTIONAL MATCH (c)" in cypher

    async def test_sync_outcome_links_by_declared_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "feedback_enabled", True)
        spec = _load("plasticos")
        spec = spec.model_copy(update={"feedbackloop": spec.feedbackloop.model_copy(update={"enabled": True})})
        driver = _wire(spec)
        await handle_sync(
            "plasticos",
            {
                "entity_type": "outcome",
                "batch": [{"match_id": "m", "chosen_candidate_id": "F1", "outcome": "positive"}],
            },
        )
        cypher = driver.calls[0][0]
        assert "(c:Facility AND c.facility_id = $candidate_id)" in cypher
        assert "entity_id" not in cypher

    async def test_legacy_resolve_matches_declared_id(self, pack) -> None:
        domain, label, id_prop, _ = pack
        driver = _wire(_load(domain), rows=[{"resolution_id": "res_x"}])
        await handle_resolve(domain, {"entity_type": label, "source_id": "a", "target_id": "b"})
        cypher = driver.calls[0][0]
        assert f"MATCH (source:{label} {{{id_prop}: $source_id}})" in cypher
        assert f"MATCH (target:{label} {{{id_prop}: $target_id}})" in cypher
        assert "entity_id" not in cypher

    async def test_enrich_by_ids_filters_declared_id(self, pack) -> None:
        domain, label, id_prop, _ = pack
        driver = _wire(_load(domain), rows=[{"enriched_count": 2}])
        await handle_enrich(
            domain,
            {"entity_type": label, "entity_ids": ["a", "b"], "enrichments": [{"property": "x", "expression": "1"}]},
        )
        cypher = driver.calls[0][0]
        assert f"WHERE n.{id_prop} IN $entity_ids" in cypher
        assert "entity_id " not in cypher

    async def test_evaluate_reads_declared_candidate_id(self, pack) -> None:
        domain, _label, id_prop, direction = pack
        rows = [
            {"candidate": {id_prop: "GOOD"}, "score": 0.9, "dimension_scores": {}},
            {"candidate": {id_prop: "OTHER"}, "score": 0.5, "dimension_scores": {}},
        ]
        _wire(_load(domain), rows=rows)
        result = await handle_admin(
            domain,
            {
                "subaction": "evaluate",
                "domain_id": domain,
                "k": 2,
                "test_set": [
                    {"query": _REQUIRED_QUERY[domain], "match_direction": direction, "expected_ids": ["GOOD"]}
                ],
            },
        )
        assert result["per_case"][0].get("error") is None, result["per_case"][0]
        assert result["aggregate"]["ndcg_at_k"] == 1.0

    async def test_flag_off_restores_entity_id_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "spec_declared_candidate_id", False)
        spec = _load("plasticos")
        driver = _wire(spec, rows=[{"resolution_id": "res_x", "enriched_count": 1}])
        await handle_outcomes("plasticos", {"match_id": "m1", "candidate_id": "C-1", "outcome": "success"})
        await handle_resolve("plasticos", {"entity_type": "Facility", "source_id": "a", "target_id": "b"})
        await handle_enrich(
            "plasticos",
            {"entity_type": "Facility", "entity_ids": ["a"], "enrichments": [{"property": "x", "expression": "1"}]},
        )
        outcomes, resolve, enrich = (c[0] for c in driver.calls)
        assert "OPTIONAL MATCH (c {entity_id: $candidate_id})" in outcomes
        assert "MATCH (source:Facility {entity_id: $source_id})" in resolve
        assert "WHERE n.entity_id IN $entity_ids" in enrich
