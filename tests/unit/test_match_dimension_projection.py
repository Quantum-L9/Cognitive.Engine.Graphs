"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, match, pareto, confidence]
owner: engine-team
status: active
--- /L9_META ---

RU-001 / CEG-COR-001: ``handle_match`` projects the active dimension scores.

The match query used to ``RETURN candidate, score`` only, so every consumer of
per-dimension values downstream of Neo4j — the Pareto front, decision
arbitration constraints and filter, and the confidence checker — evaluated
nothing. The fake driver below returns only the columns the captured RETURN
clause names, exactly as Neo4j would, so a dropped column is dropped here too.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import ArbitrationConstraintSpec, DecisionArbitrationSpec, DomainSpec
from engine.config.settings import settings
from engine.handlers import handle_match, init_dependencies
from tests.fixtures.domain_contracts import synthetic_loader

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"
DIMS = ("depot_reliability", "port_capacity")

# Rows "in the graph": candidate node, weighted score, and every dimension value
# the scoring WITH computes. Only what RETURN names reaches the handler.
GRAPH_ROWS: list[dict[str, Any]] = [
    {"candidate": {"depot_id": "d1"}, "score": 0.9, "dims": {"depot_reliability": 0.9, "port_capacity": 0.9}},
    {"candidate": {"depot_id": "d2"}, "score": 0.5, "dims": {"depot_reliability": 0.6, "port_capacity": 0.5}},
    {"candidate": {"depot_id": "d3"}, "score": 0.3, "dims": {"depot_reliability": 0.2, "port_capacity": 0.95}},
]


def _returned_dimension_aliases(cypher: str) -> list[str] | None:
    """Aliases inside the RETURN's `{...} AS dimension_scores` map, or None when not projected."""
    match = re.search(r"\{([^{}]*)\} AS dimension_scores", cypher)
    if match is None:
        return None
    return [part.split(":")[0].strip() for part in match.group(1).split(",") if part.strip()]


class _ProjectingDriver:
    """Fake GraphDriver: records each call and returns only the RETURNed columns."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "parameters": parameters, "database": database})
        aliases = _returned_dimension_aliases(cypher)
        out: list[dict[str, Any]] = []
        for row in self.rows:
            projected: dict[str, Any] = {"candidate": dict(row["candidate"]), "score": row["score"]}
            if aliases is not None:
                projected["dimension_scores"] = {a: row["dims"][a] for a in aliases}
            out.append(projected)
        return out


def _spec(**updates: Any) -> DomainSpec:
    spec = synthetic_loader().load_domain(DOMAIN)
    return spec.model_copy(update=updates) if updates else spec


def _wire(spec: DomainSpec, rows: list[dict[str, Any]] | None = None) -> _ProjectingDriver:
    driver = _ProjectingDriver(GRAPH_ROWS if rows is None else rows)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [DOMAIN]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


async def _match(**payload: Any) -> dict[str, Any]:
    return await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, **payload})


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-001")
class TestDimensionProjection:
    async def test_return_projects_every_active_dimension_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The RETURN line in isolation; RU-036's per-candidate collapse is covered below.
        monkeypatch.setattr(settings, "match_collapse_traversal_rows", False)
        driver = _wire(_spec())
        await _match()
        cypher = driver.calls[0]["cypher"]
        assert (
            "RETURN candidate, score, {depot_reliability: depot_reliability, "
            "port_capacity: port_capacity} AS dimension_scores, " in cypher
        )
        assert cypher.endswith("AS absent_sources\nORDER BY score DESC\nLIMIT $top_n")

    async def test_pareto_front_excludes_dominated_rows(self) -> None:
        _wire(_spec())
        result = await _match()
        pareto = result["pareto"]
        assert pareto["dimension_names"] == sorted(DIMS)
        assert "d2" not in pareto["non_dominated_ids"]
        assert set(pareto["non_dominated_ids"]) == {"d1", "d3"}

    async def test_hard_arbitration_constraint_removes_violator(self) -> None:
        arbitration = DecisionArbitrationSpec(
            enabled=True,
            constraints=[ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.5, hard=True)],
        )
        _wire(_spec(decision_arbitration=arbitration))
        result = await _match()
        ids = [c["candidate"]["depot_id"] for c in result["candidates"]]
        assert "d3" not in ids  # reliability 0.2 < 0.5
        assert ids == ["d1", "d2"]

    async def test_single_dominant_dimension_gets_monoculture_flag(self) -> None:
        rows = [
            {"candidate": {"depot_id": "m1"}, "score": 0.6, "dims": {"depot_reliability": 0.95, "port_capacity": 0.0}}
        ]
        _wire(_spec(), rows)
        result = await _match()
        flag = result["candidates"][0]["confidence_flag"]
        assert "monoculture" in (flag if isinstance(flag, list) else [flag])
        assert result["candidates"][0]["confidence_detail"]["dominant_dimension"] == "depot_reliability"

    async def test_candidates_carry_the_map_and_no_bare_dimension_keys(self) -> None:
        _wire(_spec())
        result = await _match()
        for cand in result["candidates"]:
            assert {"candidate", "score", "dimension_scores"} <= set(cand)
            assert not set(DIMS) & set(cand)
            assert set(cand["dimension_scores"]) == set(DIMS)

    async def test_projection_survives_the_per_candidate_collapse(self) -> None:
        driver = _wire(_spec())
        await _match()
        cypher = driver.calls[0]["cypher"]
        assert (
            "WITH candidate, score, {depot_reliability: depot_reliability, "
            "port_capacity: port_capacity} AS dimension_scores, " in cypher
        )
        assert cypher.endswith(
            "RETURN candidate, best.score AS score, best.dimension_scores AS dimension_scores, "
            "best.absent_sources AS absent_sources\nORDER BY score DESC\nLIMIT $top_n"
        )

    async def test_zero_active_dimensions_emit_an_empty_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_collapse_traversal_rows", False)
        spec = _spec()
        spec = spec.model_copy(update={"scoring": spec.scoring.model_copy(update={"dimensions": []})})
        driver = _wire(spec)
        await _match()
        assert "RETURN candidate, score, {} AS dimension_scores, [] AS absent_sources\n" in driver.calls[0]["cypher"]

    async def test_flag_off_restores_the_previous_return_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_dimension_projection", False)
        monkeypatch.setattr(settings, "match_collapse_traversal_rows", False)
        driver = _wire(_spec())
        result = await _match()
        cypher = driver.calls[0]["cypher"]
        assert "\nRETURN candidate, score\nORDER BY score DESC\nLIMIT $top_n" in cypher
        assert "dimension_scores" not in cypher
        # The defect the flag restores: no dimension data, so nothing is dominated.
        assert set(result["pareto"]["non_dominated_ids"]) == {"d1", "d2", "d3"}
