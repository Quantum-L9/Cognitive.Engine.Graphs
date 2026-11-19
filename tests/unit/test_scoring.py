"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for scoring dimension assembly.
Target Coverage: 75%+
"""

import asyncio
import math
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import ComputationType, DomainSpec, ScoringDimensionSpec, ScoringSpec
from engine.scoring.assembler import ScoringAssembler


def make_mock_domain_spec(dimensions: list[dict] | None = None) -> MagicMock:
    """Create a mock DomainSpec with scoring config."""
    spec = MagicMock(spec=DomainSpec)
    spec.scoring = MagicMock(spec=ScoringSpec)

    mock_dims = []
    for dim_dict in dimensions or []:
        dim = MagicMock(spec=ScoringDimensionSpec)
        dim.name = dim_dict.get("name", "test_dim")
        dim.candidateprop = dim_dict.get("candidateprop")
        dim.queryprop = dim_dict.get("queryprop")
        dim.candidateprop_lon = dim_dict.get("candidateprop_lon")
        dim.queryprop_lon = dim_dict.get("queryprop_lon")
        dim.computation = ComputationType(dim_dict.get("computation", "candidateproperty"))
        dim.weightkey = dim_dict.get("weightkey", "w")
        dim.defaultweight = dim_dict.get("defaultweight", 1.0)
        dim.matchdirections = dim_dict.get("matchdirections") or dim_dict.get("directionscoped")
        dim.minvalue = dim_dict.get("minvalue")
        dim.maxvalue = dim_dict.get("maxvalue")
        dim.defaultwhennull = dim_dict.get("defaultwhennull", 0.0)
        dim.expression = dim_dict.get("expression")
        dim.alias = dim_dict.get("alias")
        dim.decayconstant = dim_dict.get("decayconstant")
        dim.bias = dim_dict.get("bias")
        mock_dims.append(dim)

    spec.scoring.dimensions = mock_dims
    return spec


@pytest.mark.unit
class TestScoringAssembler:
    """Test scoring dimension compilation."""

    def test_geodecay_compiles(self) -> None:
        """Geodecay dimension compiles distance formula."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "distance_score",
                    "computation": "geodecay",
                    "candidateprop": "site_lat",
                    "candidateprop_lon": "site_lon",
                    "weightkey": "wgeo",
                    "defaultweight": 0.35,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="buyertosupplier", weights={})

        assert "distance" in cypher.lower()

    def test_inverselinear_compiles(self) -> None:
        """Inverse linear dimension compiles formula."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "price_score",
                    "candidateprop": "priceperlb",
                    "computation": "inverselinear",
                    "minvalue": 0.5,
                    "maxvalue": 2.0,
                    "weightkey": "wprice",
                    "defaultweight": 0.30,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="buyertosupplier", weights={})

        assert "priceperlb" in cypher

    def test_weighted_aggregation(self) -> None:
        """Multiple dimensions aggregate with weights."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
                {
                    "name": "score2",
                    "candidateprop": "prop2",
                    "computation": "candidateproperty",
                    "weightkey": "w2",
                    "defaultweight": 0.5,
                },
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(
            match_direction="querytocandidate", weights={"w1": 0.6, "w2": 0.4}
        )

        assert "score1" in cypher
        assert "score2" in cypher
        assert "0.6" in cypher
        assert "0.4" in cypher

    def test_direction_scoped_filtering(self) -> None:
        """Direction-scoped dimensions only apply to specific directions."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "directional_score",
                    "candidateprop": "score",
                    "computation": "candidateproperty",
                    "weightkey": "wscore",
                    "defaultweight": 1.0,
                    "matchdirections": ["buyertosupplier"],
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        # Should include in buyertosupplier
        cypher1, _ = assembler.assemble_scoring_clause(match_direction="buyertosupplier", weights={})
        assert "directional_score" in cypher1

        # Should exclude in other direction
        cypher2, _ = assembler.assemble_scoring_clause(match_direction="suppliertobuyer", weights={})
        assert "directional_score" not in cypher2

    def test_empty_dimensions_returns_zero_score(self) -> None:
        """Empty dimensions list returns 0.0 score."""
        domain_spec = make_mock_domain_spec([])
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "0.0" in cypher

    def test_candidateproperty_computation(self) -> None:
        """CandidateProperty computation uses coalesce."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "prop_score",
                    "candidateprop": "rating",
                    "computation": "candidateproperty",
                    "defaultwhennull": 0.5,
                    "weightkey": "wrating",
                    "defaultweight": 1.0,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "coalesce" in cypher
        assert "rating" in cypher

    def test_variantdiscovery_compiles(self) -> None:
        """VariantDiscovery dimension reads pre-computed beam search score."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "variant_score",
                    "candidateprop": "variant_discovery_score",
                    "computation": "variantdiscovery",
                    "defaultwhennull": 0.0,
                    "weightkey": "wvariant",
                    "defaultweight": 0.15,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "variant_discovery_score" in cypher
        assert "coalesce" in cypher
        assert "variant_score" in cypher

    def test_variantdiscovery_default_prop(self) -> None:
        """VariantDiscovery uses default property name when not specified."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "variant_score",
                    "computation": "variantdiscovery",
                    "defaultwhennull": 0.5,
                    "weightkey": "wvariant",
                    "defaultweight": 0.1,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "variant_discovery_score" in cypher
        assert "0.5" in cypher

    def test_ensembleconfidence_compiles(self) -> None:
        """EnsembleConfidence dimension reads pre-computed fusion confidence."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "ensemble_conf",
                    "candidateprop": "ensemble_confidence",
                    "computation": "ensembleconfidence",
                    "defaultwhennull": 0.5,
                    "weightkey": "wensemble",
                    "defaultweight": 0.10,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "ensemble_confidence" in cypher
        assert "coalesce" in cypher
        assert "ensemble_conf" in cypher

    def test_ensembleconfidence_default_prop(self) -> None:
        """EnsembleConfidence uses default property name when not specified."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "ensemble_conf",
                    "computation": "ensembleconfidence",
                    "defaultwhennull": 0.5,
                    "weightkey": "wensemble",
                    "defaultweight": 0.1,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "ensemble_confidence" in cypher
        assert "0.5" in cypher

    def test_kge_scoring_dimensions_combined(self) -> None:
        """All 3 KGE scoring dimensions (kge, variantdiscovery, ensembleconfidence) work together."""
        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "kge_score",
                    "candidateprop": "kge_score",
                    "computation": "kge",
                    "defaultwhennull": 0.0,
                    "weightkey": "wkge",
                    "defaultweight": 0.40,
                },
                {
                    "name": "variant_score",
                    "candidateprop": "variant_discovery_score",
                    "computation": "variantdiscovery",
                    "defaultwhennull": 0.0,
                    "weightkey": "wvariant",
                    "defaultweight": 0.15,
                },
                {
                    "name": "ensemble_conf",
                    "candidateprop": "ensemble_confidence",
                    "computation": "ensembleconfidence",
                    "defaultwhennull": 0.5,
                    "weightkey": "wensemble",
                    "defaultweight": 0.10,
                },
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        cypher, _ = assembler.assemble_scoring_clause(
            match_direction="any",
            weights={"wkge": 0.5, "wvariant": 0.3, "wensemble": 0.2},
        )

        assert "kge_score" in cypher
        assert "variant_discovery_score" in cypher
        assert "ensemble_confidence" in cypher
        assert "0.5" in cypher
        assert "0.3" in cypher
        assert "0.2" in cypher

    def test_pareto_metadata_returned_when_enabled(self) -> None:
        """Pareto metadata is returned when pareto_candidates provided and enabled."""
        from unittest.mock import patch

        from engine.scoring.pareto import ParetoCandidate

        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 1.0,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        candidates = [
            ParetoCandidate(candidate_id="c1", dimension_scores={"geo": 0.9, "price": 0.3}),
            ParetoCandidate(candidate_id="c2", dimension_scores={"geo": 0.3, "price": 0.9}),
            ParetoCandidate(candidate_id="c3", dimension_scores={"geo": 0.2, "price": 0.2}),
        ]

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.pareto_enabled = True

            _, pareto_meta = assembler.assemble_scoring_clause(
                match_direction="any",
                weights={},
                pareto_candidates=candidates,
            )

        assert pareto_meta is not None
        assert pareto_meta["front_size"] == 2
        assert pareto_meta["total_candidates"] == 3
        assert set(pareto_meta["non_dominated_ids"]) == {"c1", "c2"}

    def test_pareto_disabled_returns_none_metadata(self) -> None:
        """Pareto metadata is None when pareto_enabled=False."""
        from unittest.mock import patch

        from engine.scoring.pareto import ParetoCandidate

        domain_spec = make_mock_domain_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 1.0,
                }
            ]
        )
        assembler = ScoringAssembler(domain_spec)

        candidates = [
            ParetoCandidate(candidate_id="c1", dimension_scores={"geo": 0.9}),
            ParetoCandidate(candidate_id="c2", dimension_scores={"geo": 0.3}),
        ]

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.pareto_enabled = False

            _, pareto_meta = assembler.assemble_scoring_clause(
                match_direction="any",
                weights={},
                pareto_candidates=candidates,
            )

        assert pareto_meta is None


# G03-MATCH-SCORING. Independent expectations for the active PlasticOS dimensions.
# Grade, color, and form equality are not match dimensions: the spec declares
# form only as a non-blank capability-line fact. quantity_per_load_lbs stays a
# query parameter. The draft match-response schema stays unpublished.

BUYER = "supply_opportunity_to_buyer_facility"
SUPPLY = "buyer_demand_to_supply_opportunity"
MATCH_QUANTITY_TONS = 18.0
OPPORTUNITY_TONS = 22.0
LOAD_LBS = 40000


def _plasticos():
    from engine.config.loader import DomainPackLoader
    from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

    return DomainPackLoader(str(DESIGN_PACKS_ROOT)).load_domain("plasticos")


def _active(spec, direction: str) -> list[Any]:
    return [dim for dim in spec.scoring.dimensions if not dim.matchdirections or direction in dim.matchdirections]


class _Rows:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "parameters": parameters, "database": database})
        if "DimensionWeight" in cypher:
            return [{"name": "geo_proximity", "weight": 2.0}]
        return [dict(row) for row in self.rows]


def _match(payload: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], _Rows]:
    from engine.config.loader import DomainPackLoader
    from engine.handlers import handle_match, init_dependencies
    from engine.state import get_state
    from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

    driver = _Rows(rows)
    get_state().reset()
    init_dependencies(driver, DomainPackLoader(str(DESIGN_PACKS_ROOT)))
    try:
        result = asyncio.run(handle_match("plasticos", payload))
    finally:
        get_state().reset()
    return result, driver


def _buyer_query() -> dict[str, Any]:
    return {
        "polymer_type": "HDPE",
        "density": 0.95,
        "mfi": 8.0,
        "contamination_pct": 0.0,
        "requires_food_grade": False,
        "volume_tons": MATCH_QUANTITY_TONS,
        "lat": 40.0,
        "lon": -75.0,
        "target_price": 1000.0,
        "community_id": 7,
        "form": "PALLETS",
        "color": "NATURAL",
        "material_grade": "A",
        "quantity_per_load_lbs": LOAD_LBS,
    }


@pytest.mark.unit
class TestPlasticosMatchScoringProof:
    """Active PlasticOS dimensions, hard gates, and the feature-on scoring paths."""

    def test_declared_dimensions_and_weight_ceiling(self) -> None:
        spec = _plasticos()
        buyer = _active(spec, BUYER)
        supply = _active(spec, SUPPLY)
        assert [dim.name for dim in buyer] == [
            "geo_proximity",
            "credit_score",
            "contamination_tolerance_score",
            "community_match",
            "rate_score",
            "price_alignment",
            "recency",
        ]
        assert [dim.name for dim in supply] == ["active_supply_geo", "active_supply_price"]
        assert sum(dim.defaultweight for dim in buyer) == pytest.approx(1.0)
        assert sum(dim.defaultweight for dim in supply) == pytest.approx(1.0)

        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match="exceeding 1.0"):
            _match(
                {"query": _buyer_query(), "match_direction": BUYER, "top_n": 5, "weights": {"w_geo": 0.9}},
            )
        accepted, driver = _match(
            {
                "query": _buyer_query(),
                "match_direction": BUYER,
                "top_n": 5,
                "weights": {
                    "w_geo": 0.2,
                    "w_credit": 0.1,
                    "w_contamination": 0.1,
                    "w_community": 0.1,
                    "w_rate": 0.1,
                    "w_price": 0.1,
                    "w_recency": 0.1,
                },
            },
        )
        assert accepted["status"] == "success"
        match_calls = [call for call in driver.calls if "LIMIT $top_n" in call["cypher"]]
        assert "(0.2 * geo_proximity)" in match_calls[0]["cypher"]
        with pytest.raises(ValidationError, match="not a finite number"):
            _match(
                {
                    "query": _buyer_query(),
                    "match_direction": BUYER,
                    "top_n": 5,
                    "weights": {"w_geo": math.nan},
                },
            )

    def test_hard_gates_reject_null_evidence_and_do_not_read_undeclared_facts(self) -> None:
        from engine.gates.compiler import GateCompiler
        from engine.sync.generator import SyncGenerator

        spec = _plasticos()
        where = GateCompiler(spec).compile_all_gates(BUYER)
        assert "candidate.contamination_tolerance IS NULL" in where
        assert "candidate.contamination_tolerance >= $contamination_pct" in where
        assert "candidate.contamination_tolerance IS NOT NULL" not in where
        assert "rel.direction = $gate_polymer_capability_gate_edge0" in where
        assert "rel.form IS NOT NULL AND rel.form <> $gate_polymer_capability_gate_edge1blank" in where
        assert "candidate.capacity_tons_month" not in where
        assert "material_grade" not in where
        assert "color" not in where
        assert "PALLETS" not in where
        assert "quantity_per_load_lbs" not in where
        assert "capacity_lbs_month" not in where

        supply_where = GateCompiler(spec).compile_all_gates(SUPPLY)
        assert "candidate.available_tons IS NOT NULL" in supply_where
        assert "candidate.active IS NOT NULL" in supply_where
        assert "candidate.polymer_type IS NOT NULL" in supply_where
        assert "quantity_per_load_lbs" not in supply_where

        result, driver = _match({"query": _buyer_query(), "match_direction": BUYER, "top_n": 5})
        assert result["status"] == "success"
        match_calls = [call for call in driver.calls if "LIMIT $top_n" in call["cypher"]]
        cypher = match_calls[0]["cypher"]
        where_at = cypher.index("WHERE ")
        score_at = cypher.index(" AS score")
        assert where_at < score_at
        assert "quantity_per_load_lbs" not in cypher
        assert match_calls[0]["parameters"]["volume_tons"] == MATCH_QUANTITY_TONS
        assert match_calls[0]["parameters"]["quantity_per_load_lbs"] == LOAD_LBS
        assert "capacity_tons_month" not in cypher
        assert "rate_per_ton" in cypher

        endpoint = next(item for item in spec.sync.endpoints if item.path == "/v1/sync/supply-opportunities")
        rows, dropped = SyncGenerator(spec).project_batch(
            endpoint,
            [
                {
                    "opportunity_id": "opp-approved",
                    "facility_id": "facility:buyer-a",
                    "polymer_type": "HDPE",
                    "available_tons": OPPORTUNITY_TONS,
                    "lat": 40.0,
                    "lon": -75.0,
                    "rate_per_ton": 500.0,
                    "active": True,
                    "quantity_per_load_lbs": LOAD_LBS,
                }
            ],
        )
        assert dropped["quantity_per_load_lbs"] == 1
        assert rows[0]["node"]["available_tons"] == OPPORTUNITY_TONS
        assert "quantity_per_load_lbs" not in rows[0]["node"]
        sync_cypher = SyncGenerator(spec).generate_sync_query(endpoint, [])
        assert "quantity_per_load_lbs" not in sync_cypher

        schema = (Path(__file__).resolve().parents[2] / "contracts/payloads/match-response.schema.yaml").read_text(
            encoding="utf-8"
        )
        assert "x-publication-status: draft-unpublished" in schema

    def test_feature_on_paths_are_not_inferred_from_the_disabled_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings
        from engine.scoring.pareto import ParetoCandidate

        spec = _plasticos()
        buyer_dims = _active(spec, BUYER)

        monkeypatch.setattr(settings, "communitymatch_membership_scoring_enabled", True)
        on_clause, _meta = ScoringAssembler(spec).assemble_scoring_clause(BUYER, {})
        assert "THEN 0.92" in on_clause
        assert "abs(candidate.community_id" not in on_clause

        monkeypatch.setattr(settings, "temporalproximity_declared_signals_enabled", True)
        declared = ScoringAssembler(spec)
        declared_clause, _meta = declared.assemble_scoring_clause(BUYER, {})
        assert "touch_count_30d" not in declared_clause
        assert "is_accelerating" not in declared_clause
        assert declared.last_query_params["tp_recency_w_recency"] == pytest.approx(1.0)
        assert "/ 180.0" in declared_clause

        monkeypatch.setattr(settings, "pricealignment_float_division", True)
        price_clause, _meta = ScoringAssembler(spec).assemble_scoring_clause(BUYER, {})
        assert "toFloat(candidate.rate_per_ton) / toFloat($target_price)" in price_clause

        pre = {dim.name: dim.defaultweight for dim in buyer_dims}
        post = dict(pre)
        post["geo_proximity"] = pre["geo_proximity"] * 2.0
        scale = sum(pre.values()) / sum(post.values())
        expected = {name: value * scale for name, value in post.items()}
        assert sum(expected.values()) == pytest.approx(sum(pre.values()))

        monkeypatch.setattr(settings, "feedback_enabled", False)
        _off, off_driver = _match({"query": _buyer_query(), "match_direction": BUYER, "top_n": 5})
        assert all("DimensionWeight" not in call["cypher"] for call in off_driver.calls)

        monkeypatch.setattr(settings, "feedback_enabled", True)
        _on, on_driver = _match({"query": _buyer_query(), "match_direction": BUYER, "top_n": 5})
        learned_calls = [call for call in on_driver.calls if "DimensionWeight" in call["cypher"]]
        match_calls = [call for call in on_driver.calls if "LIMIT $top_n" in call["cypher"]]
        assert learned_calls
        for name, weight in expected.items():
            assert f"({weight} * {name})" in match_calls[0]["cypher"]

        dominated = ParetoCandidate("low", {"geo_proximity": 0.2, "credit_score": 0.2})
        dominant = ParetoCandidate("high", {"geo_proximity": 0.9, "credit_score": 0.8})
        tradeoff = ParetoCandidate("other", {"geo_proximity": 0.1, "credit_score": 0.9})
        monkeypatch.setattr(settings, "pareto_enabled", True)
        front = ScoringAssembler(spec).assemble_scoring_clause(
            BUYER, {}, pareto_candidates=[dominant, dominated, tradeoff]
        )[1]
        assert front is not None
        assert set(front["non_dominated_ids"]) == {"high", "other"}
        monkeypatch.setattr(settings, "pareto_enabled", False)
        assert (
            ScoringAssembler(spec).assemble_scoring_clause(BUYER, {}, pareto_candidates=[dominant, dominated])[1]
            is None
        )

    def test_score_scale_null_nan_tie_singleton_and_confidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        dims = {
            "geo_proximity": 1.0,
            "credit_score": 0.0,
            "contamination_tolerance_score": 0.0,
            "community_match": 0.0,
            "rate_score": 0.0,
            "price_alignment": 0.0,
            "recency": 0.0,
        }
        payload = {"query": _buyer_query(), "match_direction": BUYER, "top_n": 5}

        monkeypatch.setattr(settings, "score_normalize", False)
        raw, _driver = _match(
            payload,
            [
                {"candidate": {"facility_id": "facility:a"}, "score": 0.4, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:b"}, "score": 0.4, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:nan"}, "score": math.nan, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:none"}, "score": None, "dimension_scores": {}},
            ],
        )
        by_id = {row["candidate"]["facility_id"]: row for row in raw["candidates"]}
        assert by_id["facility:a"]["score"] == pytest.approx(0.4)
        assert by_id["facility:b"]["score"] == pytest.approx(0.4)
        assert {by_id["facility:a"]["rank"], by_id["facility:b"]["rank"]} == {1, 2}
        assert by_id["facility:a"]["score_scale"] == "0_to_1"
        assert by_id["facility:nan"]["score"] is None
        assert by_id["facility:nan"]["eligible"] is False
        assert by_id["facility:none"]["rank"] is None
        assert raw["scoring_meta"]["normalization_applied"] is False
        raw_values = list(dims.values())
        raw_mean = sum(raw_values) / len(raw_values)
        raw_std = (sum((value - raw_mean) ** 2 for value in raw_values) / len(raw_values)) ** 0.5
        agreement = max(0.0, 1.0 - 2.0 * raw_std)
        assert agreement < 0.4
        assert by_id["facility:a"]["confidence_flag"] == ["monoculture", "low_dimensional_agreement"]
        assert by_id["facility:a"]["confidence_detail"]["dominant_dimension"] == "geo_proximity"
        assert by_id["facility:a"]["confidence_detail"]["contribution"] == pytest.approx(1.0)
        assert by_id["facility:a"]["confidence_detail"]["dimensional_agreement"] == pytest.approx(round(agreement, 4))

        monkeypatch.setattr(settings, "score_normalize", True)
        scaled, _driver = _match(
            payload,
            [
                {"candidate": {"facility_id": "facility:low"}, "score": 0.0, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:high"}, "score": 2.0, "dimension_scores": dict(dims)},
                {
                    "candidate": {"facility_id": "facility:nan"},
                    "score": math.nan,
                    "dimension_scores": {"credit_score": math.nan},
                },
            ],
        )
        scaled_ids = {row["candidate"]["facility_id"]: row for row in scaled["candidates"]}
        assert scaled["scoring_meta"]["normalization_applied"] is True
        assert scaled_ids["facility:low"]["score"] == pytest.approx(0.0)
        assert scaled_ids["facility:high"]["score"] == pytest.approx(1.0)
        assert scaled_ids["facility:high"]["score_scale"] == "0_to_1"
        assert scaled_ids["facility:nan"]["score"] is None
        assert "credit_score" in scaled_ids["facility:nan"]["missing_evidence"]
        assert all(item["feature_id"] != "credit_score" for item in scaled_ids["facility:nan"]["feature_contributions"])

        singleton, _driver = _match(
            payload,
            [{"candidate": {"facility_id": "facility:only"}, "score": 0.4, "dimension_scores": dict(dims)}],
        )
        assert singleton["candidates"][0]["score"] == pytest.approx(1.0)
        assert singleton["candidates"][0]["rank"] == 1
        assert singleton["scoring_meta"]["raw_max"] == pytest.approx(0.4)
        assert singleton["scoring_meta"]["normalization_applied"] is True

        tied, _driver = _match(
            payload,
            [
                {"candidate": {"facility_id": "facility:a"}, "score": 0.5, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:b"}, "score": 0.5, "dimension_scores": dict(dims)},
            ],
        )
        assert [row["score"] for row in tied["candidates"]] == [pytest.approx(0.5), pytest.approx(0.5)]
        assert tied["scoring_meta"]["normalization_applied"] is False

        unchanged, _driver = _match(
            payload,
            [
                {"candidate": {"facility_id": "facility:a"}, "score": 5.0, "dimension_scores": dict(dims)},
                {"candidate": {"facility_id": "facility:b"}, "score": 5.0, "dimension_scores": dict(dims)},
            ],
        )
        assert [row["score"] for row in unchanged["candidates"]] == [pytest.approx(5.0), pytest.approx(5.0)]
        assert unchanged["scoring_meta"]["normalization_applied"] is False
        assert unchanged["candidates"][0]["score_scale"] == "unnormalized_declared"

        gap_dims = dict(dims)
        gap_dims["contamination_tolerance_score"] = 0.0
        gapped, _driver = _match(
            payload,
            [
                {
                    "candidate": {"facility_id": "facility:gap", "capacity_tons_month": 10.0},
                    "score": 0.4,
                    "dimension_scores": gap_dims,
                    "absent_sources": ["contamination_tolerance_score"],
                }
            ],
        )
        gap = gapped["candidates"][0]
        assert "contamination_tolerance_score" in gap["missing_evidence"]
        assert all(item["feature_id"] != "contamination_tolerance_score" for item in gap["feature_contributions"])
        assert gap["capacity_evidence"]["capacity_tons_month"] == pytest.approx(10.0)
        assert gap["capacity_evidence"]["volume_tons"] == pytest.approx(MATCH_QUANTITY_TONS)
        assert gap["capacity_evidence"]["shortfall_tons"] == pytest.approx(MATCH_QUANTITY_TONS - 10.0)

        again, _driver = _match(
            payload,
            [{"candidate": {"facility_id": "facility:only"}, "score": 0.4, "dimension_scores": dict(dims)}],
        )
        assert again["candidates"][0]["score"] == singleton["candidates"][0]["score"]
        assert again["candidates"][0]["score_scale"] == singleton["candidates"][0]["score_scale"]
