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
be replayed against the base revision to record ``MAIN_FLAG_OFF_CANDIDATES``.
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


def _flag_off_contract(response: dict[str, Any]) -> list[dict[str, Any]]:
    """What pareto_precut_enabled off promises about the response, and nothing else.

    Each candidate's id, its position, its score and its dimension_scores as
    the driver returned them. Annotation fields other contracts own
    (confidence_flag, feature_contributions, missing_evidence, rank, ...) are
    deliberately not compared here: they change with those contracts.
    """
    return [
        {"id": c["candidate"]["depot_id"], "score": c["score"], "dimension_scores": c["dimension_scores"]}
        for c in response["candidates"]
    ]


# The flag-off contract of handle_match's response for this scenario (top_n=2),
# as recorded on the base revision 8224b333 (origin/main before SI-06).
MAIN_FLAG_OFF_CANDIDATES: list[dict[str, Any]] = [
    {"id": "a", "score": 0.9, "dimension_scores": {"depot_reliability": 0.9, "port_capacity": 0.9}},
    {"id": "b", "score": 0.8, "dimension_scores": {"depot_reliability": 0.8, "port_capacity": 0.8}},
]


@pytest.mark.unit
@pytest.mark.finding("SI-06")
class TestFlagOff:
    async def test_response_is_identical_to_main(self) -> None:
        """Flag off: main's candidates, scores and dimension scores, from a query that keeps the flag-off shape.

        The Cypher is asserted structurally (CTO directive, scoring audit): the
        composite score expression (L2, D5 clamp) and the ORDER BY text (L3, D4)
        belong to other contracts and are not pinned here. What flag-off
        promises is pinned: the candidate pool is the requested top_n (no
        pool expansion), the cut is the query's own LIMIT $top_n, and the
        returned rows keep the driver's score order (no Pareto front reorder,
        no precut receipt).
        """
        assert settings.pareto_precut_enabled is False  # shipped default
        driver = _wire(_spec())
        response = await _match()
        assert _flag_off_contract(response) == MAIN_FLAG_OFF_CANDIDATES
        assert response["status"] == "success"
        assert response["total_candidates"] == 2

        assert len(driver.calls) == 1
        call = driver.calls[0]
        # No pool expansion: top_n is bound to the requested value, not 4 x top_n.
        assert call["parameters"]["top_n"] == 2
        cypher = call["cypher"]
        # The cut is the query's own final LIMIT $top_n, applied once: no
        # earlier LIMIT narrows the candidates before it.
        lines = cypher.rstrip().splitlines()
        assert lines[-1] == "LIMIT $top_n"
        assert cypher.count("$top_n") == 1
        assert sum(line.lstrip().startswith("LIMIT") for line in lines) == 1
        assert " LIMIT " not in cypher
        # The final ordering, just before the cut, is by descending score first
        # (any tie-break keys after it belong to the ordering contract, L3/D4).
        assert lines[-2].startswith("ORDER BY score DESC")
        # Rows are returned in the driver's score order: a > b, and the
        # non-dominated but lower-scoring d is not promoted into the cut.
        assert _ids(response) == ["a", "b"]
        assert [c["score"] for c in response["candidates"]] == [0.9, 0.8]
        assert "precut" not in response["pareto"]["arbitration"]

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
        assert driver.calls[0]["cypher"].endswith("ORDER BY score DESC, candidate.depot_id ASC\nLIMIT $top_n")
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

    async def test_hard_constraint_applies_to_the_pool_before_the_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The Python pool pass is the rollback path of ND-ARB: with
        # match_arbitration_in_cypher_enabled off it still rejects in the pool.
        monkeypatch.setattr(settings, "match_arbitration_in_cypher_enabled", False)
        constrained = arbitration_spec(
            constraints=[ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.5, hard=True)]
        )
        _wire(_spec(constrained))
        response = await _match(top_n=2)
        # d (reliability 0.2) is rejected; b is the best of what remains.
        assert _ids(response) == ["a", "b"]
        assert response["pareto"]["arbitration"]["precut"]["pool_size"] == 3

    async def test_cypher_arbitration_is_not_reapplied_to_the_pool(self) -> None:
        # ND-ARB (D4): the hard constraint is a WHERE in the pool query itself, so
        # the pool Neo4j returns is already admissible; Python must not re-filter
        # or re-penalise it. This fake driver ignores the WHERE on purpose: d
        # surviving proves no second pass ran.
        constrained = arbitration_spec(
            constraints=[ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.5, hard=True)]
        )
        driver = _wire(_spec(constrained))
        response = await _match(top_n=2)
        cypher = driver.calls[0]["cypher"]
        assert "WHERE NOT coalesce(depot_reliability < $arb_hard_threshold_0, false)" in cypher
        assert driver.calls[0]["parameters"]["arb_hard_threshold_0"] == 0.5
        assert _ids(response) == ["a", "d"]
        assert response["pareto"]["arbitration"]["precut"]["pool_size"] == 4

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
