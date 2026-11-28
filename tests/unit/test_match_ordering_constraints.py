"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, match, arbitration, ordering]
owner: engine-team
status: active
--- /L9_META ---

ND-ARB / ND-TIE (campaign ceg-scoring-audit, lane L3, operator decision D4).

``decision_arbitration.constraints`` used to run in Python on the list Cypher
had already cut to ``top_n``: a hard-rejected candidate could hold a slot an
admissible one lost, and a soft penalty re-scored rows without re-ranking them.
Under ``match_arbitration_in_cypher_enabled`` a hard constraint is a ``WHERE``
on the scoring ``WITH`` and a soft penalty multiplies ``score`` in a following
``WITH``, both before ``ORDER BY`` / ``LIMIT``. Under
``match_candidate_tiebreak_enabled`` the final ``ORDER BY`` breaks score ties
on the pack's declared candidate identity and the per-candidate path collapse
is ordered by the projected dimensions after ``score``.

The fake driver records the Cypher; these tests pin the composition contract.
The ranking behaviour itself is proven against Neo4j in
tests/integration/test_match_arbitration_order.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import (
    ArbitrationConstraintSpec,
    DecisionArbitrationSpec,
    DomainSpec,
)
from engine.config.settings import settings
from engine.handlers import handle_match, init_dependencies
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"

HARD = ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.5, hard=True)
SOFT = ArbitrationConstraintSpec(dimension="port_capacity", threshold=0.4, hard=False, penalty=0.25)


class Driver:
    """Records every query; returns the rows it was given unchanged."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "parameters": dict(parameters), "database": database})
        return [dict(r) for r in self.rows]


def _spec(*constraints: ArbitrationConstraintSpec, enabled: bool = True) -> DomainSpec:
    spec = synthetic_loader().load_domain(DOMAIN)
    arbitration = DecisionArbitrationSpec(enabled=enabled, constraints=list(constraints))
    return spec.model_copy(update={"decision_arbitration": arbitration})


def _wire(spec: DomainSpec, rows: list[dict[str, Any]] | None = None) -> Driver:
    driver = Driver(rows)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [DOMAIN]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


async def _cypher(spec: DomainSpec) -> tuple[str, dict[str, Any]]:
    driver = _wire(spec)
    await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": 3})
    call = driver.calls[0]
    return call["cypher"], call["parameters"]


def _row(depot_id: str, score: float, reliability: float, ports: float) -> dict[str, Any]:
    return {
        "candidate": {"depot_id": depot_id},
        "score": score,
        "dimension_scores": {"depot_reliability": reliability, "port_capacity": ports},
        "absent_sources": [],
    }


class TestShippedDefaults:
    def test_flags_ship_on(self) -> None:
        assert settings.match_arbitration_in_cypher_enabled is True
        assert settings.match_candidate_tiebreak_enabled is True


class TestHardConstraintIsAWhereBeforeTheCut:
    async def test_hard_constraint_filters_the_scoring_rows(self) -> None:
        cypher, params = await _cypher(_spec(HARD))
        lines = cypher.split("\n")
        where_at = lines.index("WHERE NOT coalesce(depot_reliability < $arb_hard_threshold_0, false)")
        # The WHERE belongs to the scoring WITH that binds `score`, whatever the
        # composite expression is (lane L2 owns it): structure, not text.
        scoring_with = lines[where_at - 1]
        assert scoring_with.startswith("WITH candidate, ")
        assert scoring_with.endswith(" AS score")
        assert params["arb_hard_threshold_0"] == 0.5

    async def test_hard_where_precedes_every_order_by_and_the_limit(self) -> None:
        cypher, _ = await _cypher(_spec(HARD))
        where_at = cypher.index("WHERE NOT coalesce(depot_reliability")
        assert where_at < cypher.index("ORDER BY")
        assert where_at < cypher.index("LIMIT $top_n")

    async def test_threshold_is_a_parameter_not_an_interpolated_value(self) -> None:
        cypher, _ = await _cypher(_spec(HARD))
        assert "< 0.5" not in cypher


class TestSoftPenaltyIsInTheScoreBeforeTheCut:
    async def test_soft_penalty_rescales_score(self) -> None:
        cypher, params = await _cypher(_spec(SOFT))
        penalised = (
            "WITH candidate, depot_reliability, port_capacity, score * "
            "(CASE WHEN coalesce(port_capacity < $arb_soft_threshold_0, false) "
            "THEN $arb_soft_penalty_0 ELSE 1.0 END) AS score\n"
        )
        assert penalised in cypher
        assert params["arb_soft_threshold_0"] == 0.4
        assert params["arb_soft_penalty_0"] == 0.25
        assert cypher.index(penalised) < cypher.index("ORDER BY")

    async def test_hard_and_soft_compose_in_declared_order(self) -> None:
        cypher, _ = await _cypher(_spec(SOFT, HARD))
        assert cypher.index("WHERE NOT coalesce(depot_reliability") < cypher.index("score * (CASE WHEN")

    async def test_several_soft_penalties_multiply(self) -> None:
        second = ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.9, penalty=0.5)
        cypher, params = await _cypher(_spec(SOFT, second))
        assert (
            "score * (CASE WHEN coalesce(port_capacity < $arb_soft_threshold_0, false) THEN $arb_soft_penalty_0 "
            "ELSE 1.0 END) * (CASE WHEN coalesce(depot_reliability < $arb_soft_threshold_1, false) "
            "THEN $arb_soft_penalty_1 ELSE 1.0 END) AS score"
        ) in cypher
        assert params["arb_soft_penalty_1"] == 0.5


class TestConstraintScope:
    async def test_disabled_arbitration_compiles_no_constraint(self) -> None:
        cypher, params = await _cypher(_spec(HARD, SOFT, enabled=False))
        assert "arb_" not in cypher
        assert not any(key.startswith("arb_") for key in params)

    async def test_constraint_on_a_dimension_inactive_for_the_direction_is_not_compiled(self) -> None:
        spec = _spec(HARD)
        dims = [
            d.model_copy(update={"matchdirections": ["some_other_direction"]}) if d.name == "depot_reliability" else d
            for d in spec.scoring.dimensions
        ]
        spec = spec.model_copy(update={"scoring": spec.scoring.model_copy(update={"dimensions": dims})})
        cypher, _ = await _cypher(spec)
        # Python skipped a constraint whose dimension had no value; Cypher must
        # not reference an alias the scoring WITH never bound.
        assert "arb_hard_threshold" not in cypher
        assert "depot_reliability <" not in cypher


class TestNoPythonPostCutRearbitration:
    async def test_rows_are_neither_dropped_nor_repenalised_in_python(self) -> None:
        # Cypher already applied the constraints; a returned row is final.
        rows = [_row("a", 0.42, 0.40, 0.10), _row("b", 0.30, 0.90, 0.10)]
        spec = _spec(HARD, SOFT)
        driver = _wire(spec, rows)
        result = await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": 3})
        assert len(driver.calls) == 1
        by_id = {c["candidate"]["depot_id"]: c["score"] for c in result["candidates"]}
        assert by_id == {"a": 0.42, "b": 0.30}


class TestTieBreak:
    async def test_final_order_breaks_ties_on_declared_identity(self) -> None:
        cypher, _ = await _cypher(_spec())
        assert cypher.endswith("ORDER BY score DESC, candidate.depot_id ASC\nLIMIT $top_n")

    async def test_path_collapse_is_ordered_by_dimensions_after_score(self) -> None:
        cypher, _ = await _cypher(_spec())
        collapse = (
            "ORDER BY score DESC, dimension_scores.depot_reliability DESC, dimension_scores.port_capacity DESC\n"
            "WITH candidate, head(collect("
        )
        assert collapse in cypher

    async def test_tiebreak_uses_the_sync_idproperty_of_the_candidate_label(self) -> None:
        spec = _spec()
        endpoints = [
            e.model_copy(update={"idproperty": "reliability"}) if e.targetnode == "OrbitalDepot" else e
            for e in spec.sync.endpoints
        ]
        spec = spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": endpoints})})
        cypher, _ = await _cypher(spec)
        assert cypher.endswith("ORDER BY score DESC, candidate.reliability ASC\nLIMIT $top_n")


class TestRollback:
    async def test_flags_off_restore_the_legacy_composition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_arbitration_in_cypher_enabled", False)
        monkeypatch.setattr(settings, "match_candidate_tiebreak_enabled", False)
        cypher, params = await _cypher(_spec(HARD, SOFT))
        assert "arb_" not in cypher
        assert not any(key.startswith("arb_") for key in params)
        assert cypher.endswith("ORDER BY score DESC\nLIMIT $top_n")
        assert "ORDER BY score DESC\nWITH candidate, head(collect(" in cypher

    async def test_flag_off_keeps_the_python_post_cut_constraints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_arbitration_in_cypher_enabled", False)
        rows = [_row("a", 0.42, 0.40, 0.10), _row("b", 0.30, 0.90, 0.10)]
        _wire(_spec(HARD, SOFT), rows)
        result = await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": 3})
        by_id = {c["candidate"]["depot_id"]: c["score"] for c in result["candidates"]}
        # Legacy: "a" hard-rejected after the cut, "b" soft-penalised in place.
        assert by_id == {"b": pytest.approx(0.075)}


class TestDeclaredIdentityResolution:
    """CTO answer (L3): the identity is the candidate label's sync ``idproperty``.

    Exactly one distinct value: tie-break on it. None, or endpoints that
    disagree: no tie-break key, one warning per pack revision and direction,
    and the pack is still served (not withheld).
    """

    @staticmethod
    def _with_endpoints(spec: DomainSpec, *idproperties: str | None) -> DomainSpec:
        base = next(e for e in spec.sync.endpoints if e.targetnode == "OrbitalDepot")
        others = [e for e in spec.sync.endpoints if e.targetnode != "OrbitalDepot"]
        depots = [
            base.model_copy(update={"idproperty": prop, "path": f"/v1/sync/orbital-depots-{i}"})
            for i, prop in enumerate(idproperties)
        ]
        return spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": [*others, *depots]})})

    @pytest.fixture(autouse=True)
    def _fresh_warning_cache(self) -> None:
        from engine.handlers import _warn_unordered_ties

        _warn_unordered_ties.cache_clear()

    def test_one_declared_idproperty_is_the_identity(self) -> None:
        from engine.handlers import _declared_candidate_identity

        assert _declared_candidate_identity(_spec(), "OrbitalDepot") == "depot_id"
        # Two endpoints agreeing on the same idproperty are still one identity.
        agreeing = self._with_endpoints(_spec(), "depot_id", "depot_id")
        assert _declared_candidate_identity(agreeing, "OrbitalDepot") == "depot_id"

    def test_no_sync_idproperty_resolves_to_none(self) -> None:
        from engine.handlers import _declared_candidate_identity

        # depot_id is a required ontology property, but only a sync idproperty
        # declares identity: no required-property or entity_id fallback.
        assert _declared_candidate_identity(_spec().model_copy(update={"sync": None}), "OrbitalDepot") is None

    def test_conflicting_idproperties_resolve_to_none(self) -> None:
        from engine.handlers import _declared_candidate_identity

        conflicting = self._with_endpoints(_spec(), "depot_id", "reliability")
        assert _declared_candidate_identity(conflicting, "OrbitalDepot") is None

    @pytest.mark.parametrize("case", ["none", "conflicting"])
    async def test_unresolved_identity_orders_by_score_alone_and_warns_once(
        self, case: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        spec = (
            _spec().model_copy(update={"sync": None})
            if case == "none"
            else self._with_endpoints(_spec(), "depot_id", "reliability")
        )
        with caplog.at_level("WARNING", logger="engine.handlers"):
            first, _ = await _cypher(spec)
            second, _ = await _cypher(spec)
        assert first.endswith("ORDER BY score DESC\nLIMIT $top_n")
        assert second == first
        warnings = [r for r in caplog.records if "score ties are unordered" in r.getMessage()]
        assert len(warnings) == 1
        assert "orbital-logistics" in warnings[0].getMessage()
        assert DIRECTION in warnings[0].getMessage()
        if case == "conflicting":
            assert "depot_id, reliability" in warnings[0].getMessage()


class TestParameterNamespace:
    async def test_arbitration_params_never_overwrite_a_declared_query_field(self) -> None:
        from engine.config.schema import QueryFieldSpec

        spec = _spec(HARD, SOFT)
        fields = [
            *spec.queryschema.fields,
            QueryFieldSpec(name="arb_hard_threshold_0", type="float"),
            QueryFieldSpec(name="arb_soft_penalty_0", type="float"),
        ]
        spec = spec.model_copy(update={"queryschema": spec.queryschema.model_copy(update={"fields": fields})})
        driver = _wire(spec)
        query = {"arb_hard_threshold_0": 0.99, "arb_soft_penalty_0": 0.77}
        await handle_match(DOMAIN, {"query": query, "match_direction": DIRECTION, "top_n": 3})
        cypher, params = driver.calls[0]["cypher"], driver.calls[0]["parameters"]
        # The caller's values are untouched; the constraint binds its own names.
        assert params["arb_hard_threshold_0"] == 0.99
        assert params["arb_soft_penalty_0"] == 0.77
        assert "depot_reliability < $arb_hard_threshold_0_1," in cypher
        assert params["arb_hard_threshold_0_1"] == 0.5
        assert "THEN $arb_soft_penalty_0_1 ELSE" in cypher
        assert params["arb_soft_penalty_0_1"] == 0.25
        assert params["arb_soft_threshold_0"] == 0.4
