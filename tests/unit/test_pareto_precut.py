"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, match, pareto, arbitration]
owner: engine-team
status: active
--- /L9_META ---

SI-06: ``handle_match`` arbitrates by Pareto front *before* the ``top_n`` cut.

The match query used to rank and ``LIMIT $top_n`` first; decision arbitration
then ran on the cut list and only produced metadata, so a dominated candidate
could keep a slot a non-dominated one lost. With ``pareto_precut_enabled`` the
same Cypher binds a bounded pool, arbitration orders it, and the cut follows.

The fake driver below behaves like Neo4j for the RETURN this suite reads:
rows ordered by ``score DESC`` and truncated to the bound ``$top_n``.

Module-level imports name only symbols that predate SI-06, so the scenario can
be replayed against the base revision to record ``MAIN_FLAG_OFF_RESPONSE``.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import (
    ArbitrationConstraintSpec,
    DecisionArbitrationSpec,
    DomainSpec,
    ParetoConfigSpec,
    ParetoObjectiveSpec,
)
from engine.config.settings import settings
from engine.handlers import handle_match, init_dependencies
from tests.fixtures.domain_contracts import synthetic_loader

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"
OBJECTIVES = ("depot_reliability", "port_capacity")

# Score order a > b > c > d. b and c are dominated by a on both objectives;
# d scores lowest but has the most port capacity, so it is non-dominated.
ROWS: list[dict[str, Any]] = [
    {"candidate": {"depot_id": "a"}, "score": 0.90, "dims": {"depot_reliability": 0.9, "port_capacity": 0.9}},
    {"candidate": {"depot_id": "b"}, "score": 0.80, "dims": {"depot_reliability": 0.8, "port_capacity": 0.8}},
    {"candidate": {"depot_id": "c"}, "score": 0.70, "dims": {"depot_reliability": 0.7, "port_capacity": 0.7}},
    {"candidate": {"depot_id": "d"}, "score": 0.50, "dims": {"depot_reliability": 0.2, "port_capacity": 0.95}},
]


class Driver:
    """Fake GraphDriver: ORDER BY score DESC then LIMIT $top_n, as the match Cypher does."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "parameters": dict(parameters), "database": database})
        ordered = sorted(self.rows, key=lambda r: -r["score"])[: parameters["top_n"]]
        return [
            {"candidate": dict(r["candidate"]), "score": r["score"], "dimension_scores": dict(r["dims"])}
            for r in ordered
        ]


def arbitration_spec(**updates: Any) -> DecisionArbitrationSpec:
    objectives = [ParetoObjectiveSpec(dimension=d) for d in OBJECTIVES]
    base = DecisionArbitrationSpec(enabled=True, pareto_config=ParetoConfigSpec(objectives=objectives))
    return base.model_copy(update=updates) if updates else base


def _spec(arbitration: DecisionArbitrationSpec | None = None) -> DomainSpec:
    spec = synthetic_loader().load_domain(DOMAIN)
    return spec.model_copy(update={"decision_arbitration": arbitration or arbitration_spec()})


def _wire(spec: DomainSpec, rows: list[dict[str, Any]] | None = None) -> Driver:
    driver = Driver(ROWS if rows is None else rows)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [DOMAIN]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


async def _match(top_n: int = 2) -> dict[str, Any]:
    return await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": top_n})


def _stable(response: dict[str, Any]) -> dict[str, Any]:
    """The response minus its per-call fields (random query_id, wall-clock time)."""
    out = copy.deepcopy(response)
    out.pop("query_id")
    out.pop("execution_time_ms")
    return out


def _ids(response: dict[str, Any]) -> list[str]:
    return [c["candidate"]["depot_id"] for c in response["candidates"]]


# handle_match's response for this scenario (top_n=2) recorded on the base
# revision 8224b333 (origin/main before SI-06), minus query_id and
# execution_time_ms, and the Cypher it sent. The flag-off path must reproduce
# both exactly.
MAIN_FLAG_OFF_RESPONSE: dict[str, Any] = {
    "candidates": [
        {
            "candidate": {"depot_id": "a"},
            "dimension_scores": {"depot_reliability": 0.9, "port_capacity": 0.9},
            "eligible": True,
            "failed_gates": [],
            "feature_contributions": [
                {"contribution": 0.9, "evidence_refs": [], "feature_id": "depot_reliability"},
                {"contribution": 0.9, "evidence_refs": [], "feature_id": "port_capacity"},
            ],
            "missing_evidence": [],
            "rank": 1,
            "score": 0.9,
            "score_scale": "0_to_1",
        },
        {
            "candidate": {"depot_id": "b"},
            "dimension_scores": {"depot_reliability": 0.8, "port_capacity": 0.8},
            "eligible": True,
            "failed_gates": [],
            "feature_contributions": [
                {"contribution": 0.8, "evidence_refs": [], "feature_id": "depot_reliability"},
                {"contribution": 0.8, "evidence_refs": [], "feature_id": "port_capacity"},
            ],
            "missing_evidence": [],
            "rank": 2,
            "score": 0.8,
            "score_scale": "0_to_1",
        },
    ],
    "direction": "window_to_depot",
    "match_direction": "window_to_depot",
    "pareto": {
        "arbitration": {"frontsize": 1, "nondominated_ids": ["a"], "pruned_pct": 50.0, "trimmed_ids": []},
        "dimension_names": ["depot_reliability", "port_capacity"],
        "front_size": 1,
        "non_dominated_ids": ["a"],
        "total_candidates": 2,
    },
    "scoring_meta": {"normalization_applied": False, "raw_max": 0.9, "raw_min": 0.8, "weights_used": {}},
    "status": "success",
    "total_candidates": 2,
}

MAIN_FLAG_OFF_CYPHER = (
    "MATCH (candidate:OrbitalDepot)\n"
    "\n"
    "WITH *\n"
    "WHERE ((candidate.accepts_hazmat IS NOT NULL AND (candidate.accepts_hazmat = true))) AND ((candidate.docking_ports IS NOT NULL AND $min_ports IS NOT NULL AND (candidate.docking_ports >= $min_ports)))\n"
    "WITH candidate, CASE WHEN (coalesce(candidate.reliability, 0.0)) IS NULL THEN 0.0 WHEN isNaN((coalesce(candidate.reliability, 0.0))) THEN 0.0 WHEN (coalesce(candidate.reliability, 0.0)) < 0.0 THEN 0.0 WHEN (coalesce(candidate.reliability, 0.0)) > 1.0 THEN 1.0 ELSE (coalesce(candidate.reliability, 0.0)) END AS depot_reliability, CASE WHEN (log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)) IS NULL THEN 0.0 WHEN isNaN((log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0))) THEN 0.0 WHEN (log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)) < 0.0 THEN 0.0 WHEN (log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)) > 1.0 THEN 1.0 ELSE (log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)) END AS port_capacity\n"
    "WITH candidate, depot_reliability, port_capacity, (0.6 * depot_reliability) + (0.3 * port_capacity) AS score\n"
    "WITH candidate, score, {depot_reliability: depot_reliability, port_capacity: port_capacity} AS dimension_scores, "
    "[x IN [CASE WHEN candidate.reliability IS NULL THEN $absent_src_depot_reliability ELSE NULL END, "
    "CASE WHEN candidate.docking_ports IS NULL THEN $absent_src_port_capacity ELSE NULL END] WHERE x IS NOT NULL | x] "
    "AS absent_sources\n"
    "ORDER BY score DESC\n"
    "WITH candidate, head(collect({score: score, dimension_scores: dimension_scores, absent_sources: absent_sources})) AS best\n"
    "RETURN candidate, best.score AS score, best.dimension_scores AS dimension_scores, "
    "best.absent_sources AS absent_sources\n"
    "ORDER BY score DESC\n"
    "LIMIT $top_n"
)


@pytest.mark.unit
@pytest.mark.finding("SI-06")
class TestFlagOff:
    async def test_response_is_identical_to_main(self) -> None:
        assert settings.pareto_precut_enabled is False  # shipped default
        driver = _wire(_spec())
        response = await _match()
        assert _stable(response) == MAIN_FLAG_OFF_RESPONSE
        assert driver.calls[0]["parameters"]["top_n"] == 2
        # D5 (CEG scoring audit L2): under score_clamp_enabled the composite is
        # clamped to [0, 1]. MAIN_FLAG_OFF_CYPHER stays main's text; the emitted
        # query differs from it in exactly that one expression.
        from engine.scoring.assembler import ScoringAssembler

        weighted = "(0.6 * depot_reliability) + (0.3 * port_capacity)"
        assert MAIN_FLAG_OFF_CYPHER.count(f"{weighted} AS score") == 1
        expected = MAIN_FLAG_OFF_CYPHER.replace(
            f"{weighted} AS score", f"{ScoringAssembler._clamp_expression(weighted)} AS score"
        )
        assert driver.calls[0]["cypher"] == expected

    async def test_flag_on_without_objectives_is_identical_to_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_objectives = arbitration_spec(pareto_config=ParetoConfigSpec())
        off_driver = _wire(_spec(no_objectives))
        off = _stable(await _match())
        monkeypatch.setattr(settings, "pareto_precut_enabled", True)
        on_driver = _wire(_spec(no_objectives))
        on = _stable(await _match())
        assert on == off
        assert on_driver.calls[0]["cypher"] == off_driver.calls[0]["cypher"]
        assert on_driver.calls[0]["parameters"] == off_driver.calls[0]["parameters"]


@pytest.mark.unit
@pytest.mark.finding("SI-06")
class TestFlagOn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pareto_precut_enabled", True)

    async def test_dominated_candidate_is_replaced_by_non_dominated_one(self) -> None:
        driver = _wire(_spec())
        response = await _match()
        # Flag off returns [a, b]; b is dominated by a, d is not.
        assert _ids(response) == ["a", "d"]
        assert driver.calls[0]["cypher"].endswith("ORDER BY score DESC\nLIMIT $top_n")
        assert driver.calls[0]["parameters"]["top_n"] == 8  # 4 x top_n
        arbitration = response["pareto"]["arbitration"]
        assert arbitration["nondominated_ids"] == ["a", "d"]
        assert arbitration["frontsize"] == 2
        assert arbitration["pruned_pct"] == 50.0
        assert arbitration["precut"] == {
            "pool_limit": 8,
            "pool_size": 4,
            "returned": 2,
            "cut_count": 2,
            "front_ranks": [0, 0],
            "unranked_ids": [],
        }
        # The response-level front is computed on the returned candidates.
        assert set(response["pareto"]["non_dominated_ids"]) == {"a", "d"}

    async def test_later_fronts_fill_in_rank_then_score_order(self) -> None:
        _wire(_spec())
        response = await _match(top_n=4)
        assert _ids(response) == ["a", "d", "b", "c"]
        assert response["pareto"]["arbitration"]["precut"]["front_ranks"] == [0, 0, 1, 2]

    async def test_ordering_is_deterministic_across_replays_and_row_order(self) -> None:
        tied = [
            {"candidate": {"depot_id": "z"}, "score": 0.6, "dims": {"depot_reliability": 0.9, "port_capacity": 0.1}},
            {"candidate": {"depot_id": "m"}, "score": 0.6, "dims": {"depot_reliability": 0.1, "port_capacity": 0.9}},
            {"candidate": {"depot_id": "k"}, "score": 0.6, "dims": {"depot_reliability": 0.5, "port_capacity": 0.5}},
            {"candidate": {"depot_id": "q"}, "score": 0.4, "dims": {"depot_reliability": 0.4, "port_capacity": 0.4}},
        ]
        orders = []
        for rows in (tied, list(reversed(tied)), [tied[2], tied[0], tied[3], tied[1]]):
            for _ in range(2):
                _wire(_spec(), rows)
                orders.append(_ids(await _match(top_n=4)))
        # Same front, same score: candidate id ascending breaks the tie.
        assert all(order == ["k", "m", "z", "q"] for order in orders)

    async def test_pool_is_bounded(self) -> None:
        from engine.scoring.pareto_integrator import PRECUT_POOL_CEILING, precut_pool_limit

        assert precut_pool_limit(1) == 4
        assert precut_pool_limit(10) == 40
        assert precut_pool_limit(250) == PRECUT_POOL_CEILING
        assert precut_pool_limit(1000) == 1000  # never fewer than top_n
        many = [
            {
                "candidate": {"depot_id": f"x{i:04d}"},
                "score": 1.0 - i / 5000,
                "dims": {"depot_reliability": 1.0 - i / 5000, "port_capacity": 1.0 - i / 5000},
            }
            for i in range(1500)
        ]
        driver = _wire(_spec(), many)
        response = await _match(top_n=300)
        assert driver.calls[0]["parameters"]["top_n"] == PRECUT_POOL_CEILING
        precut = response["pareto"]["arbitration"]["precut"]
        assert precut["pool_size"] == PRECUT_POOL_CEILING
        assert precut["returned"] == 300
        assert len(response["candidates"]) == 300

    async def test_hard_constraint_applies_to_the_pool_before_the_cut(self) -> None:
        constrained = arbitration_spec(
            constraints=[ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.5, hard=True)]
        )
        _wire(_spec(constrained))
        response = await _match(top_n=2)
        # d (reliability 0.2) is rejected; b is the best of what remains.
        assert _ids(response) == ["a", "b"]
        assert response["pareto"]["arbitration"]["precut"]["pool_size"] == 3

    async def test_missing_objective_is_unranked_not_zero(self) -> None:
        rows = [
            *ROWS,
            # Highest score, but port_capacity is unknown: it is neither on nor
            # off the front, and it is never compared as if it were 0.0.
            {"candidate": {"depot_id": "u"}, "score": 0.99, "dims": {"depot_reliability": 0.99}},
        ]
        _wire(_spec(), rows)
        response = await _match(top_n=5)
        assert _ids(response) == ["a", "d", "b", "c", "u"]
        precut = response["pareto"]["arbitration"]["precut"]
        assert precut["unranked_ids"] == ["u"]
        assert precut["front_ranks"] == [0, 0, 1, 2, None]
        # Had u's port_capacity been filled with 0.0 it would be non-dominated
        # (best reliability) and sit on front 0 ahead of a.
        assert "u" not in response["pareto"]["arbitration"]["nondominated_ids"]
        assert "port_capacity" not in response["candidates"][-1]["dimension_scores"]

    async def test_minimised_objective_is_honoured(self) -> None:
        minimise = arbitration_spec(
            pareto_config=ParetoConfigSpec(
                objectives=[
                    ParetoObjectiveSpec(dimension="depot_reliability"),
                    ParetoObjectiveSpec(dimension="port_capacity", direction="minimize"),
                ]
            )
        )
        _wire(_spec(minimise))
        response = await _match(top_n=4)
        # Minimising ports, a and b dominate d (more reliable, fewer ports).
        assert _ids(response) == ["a", "b", "c", "d"]
        assert response["pareto"]["arbitration"]["precut"]["front_ranks"] == [0, 0, 0, 1]


@pytest.mark.unit
@pytest.mark.finding("SI-06")
class TestIntegratorMissingDimensions:
    RECORDS: list[dict[str, Any]] = [
        {"candidate": {"entity_id": "full"}, "score": 0.5, "dimension_scores": {"x": 0.4, "y": 0.6}},
        {"candidate": {"entity_id": "gap"}, "score": 0.9, "dimension_scores": {"x": 0.9, "y": None}},
    ]

    def test_flag_off_keeps_the_legacy_zero_fill(self) -> None:
        from engine.scoring.pareto_integrator import build_pareto_candidates

        built = build_pareto_candidates(self.RECORDS, ["x", "y"])
        assert built[1].dimension_scores == {"x": 0.9, "y": 0.0}

    def test_flag_on_omits_the_missing_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.scoring.pareto_integrator import build_pareto_candidates

        monkeypatch.setattr(settings, "pareto_precut_enabled", True)
        built = build_pareto_candidates(self.RECORDS, ["x", "y"])
        assert built[0].dimension_scores == {"x": 0.4, "y": 0.6}
        assert built[1].dimension_scores == {"x": 0.9}

    def test_flag_on_filter_reports_unranked_instead_of_comparing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.scoring.pareto_integrator import apply_pareto_filter

        off = apply_pareto_filter(self.RECORDS, ["x", "y"])
        assert "unranked" not in off
        monkeypatch.setattr(settings, "pareto_precut_enabled", True)
        on = apply_pareto_filter(self.RECORDS, ["x", "y"])
        assert on["unranked"] == ["gap"]
        assert on["nondominated"] == ["full"]
        assert on["dominated"] == []

    def test_non_dominated_ranks(self) -> None:
        from engine.scoring.pareto import non_dominated_ranks

        vectors = [{"x": 1.0, "y": 1.0}, {"x": 0.5, "y": 0.5}, {"x": 0.2, "y": 2.0}, {"x": 0.1, "y": 0.1}]
        assert non_dominated_ranks(vectors, ["x", "y"]) == [0, 1, 0, 2]
        assert non_dominated_ranks(vectors, ["x", "y"], {"y": "minimize"}) == [0, 0, 1, 0]
        assert non_dominated_ranks([], ["x"]) == []
