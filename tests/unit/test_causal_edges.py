"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, causal, edges]
owner: engine-team
status: active
--- /L9_META ---

Tests for engine.causal — edge taxonomy, compiler, and attribution.

Covers:
- CausalEdgeType enum values and validation
- CausalEdgeValidator temporal precedence and property validation
- CausalCompiler generates valid Cypher patterns
- Chain depth limit enforcement
- AttributionCalculator models (first_touch, last_touch, linear, position_based)
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from engine.causal.attribution import AttributionCalculator, _temporal_decay_factor
from engine.causal.causal_compiler import CausalCompiler
from engine.causal.edge_taxonomy import CausalEdgeType, CausalEdgeValidator
from engine.config.schema import (
    CausalEdgeSpec,
    CausalSubgraphSpec,
    DomainSpec,
    FeedbackLoopSpec,
)

# ── CausalEdgeType Enum ────────────────────────────────


class TestCausalEdgeType:
    def test_has_10_members(self):
        assert len(CausalEdgeType) == 10

    def test_all_values(self):
        expected = {
            "CAUSED_BY",
            "TRIGGERED",
            "DROVE",
            "RESULTED_IN",
            "ACCELERATED_BY",
            "BLOCKED_BY",
            "ENABLED_BY",
            "PREVENTED_BY",
            "INFLUENCED_BY",
            "CONTRIBUTED_TO",
        }
        actual = {e.value for e in CausalEdgeType}
        assert actual == expected

    def test_is_str_enum(self):
        assert isinstance(CausalEdgeType.CAUSED_BY, str)
        assert CausalEdgeType.CAUSED_BY == "CAUSED_BY"


# ── CausalEdgeValidator ────────────────────────────────


class TestCausalEdgeValidator:
    def test_valid_properties(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.CAUSED_BY,
            {"confidence": 0.8, "mechanism": "direct_interaction"},
        )
        assert errors == []

    def test_missing_confidence(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.TRIGGERED,
            {"mechanism": "signal_propagation"},
        )
        assert any("confidence" in e for e in errors)

    def test_missing_mechanism(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.DROVE,
            {"confidence": 0.5},
        )
        assert any("mechanism" in e for e in errors)

    def test_confidence_out_of_range(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.RESULTED_IN,
            {"confidence": 1.5, "mechanism": "outcome"},
        )
        assert any("between 0.0 and 1.0" in e for e in errors)

    def test_confidence_negative(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.RESULTED_IN,
            {"confidence": -0.1, "mechanism": "outcome"},
        )
        assert any("between 0.0 and 1.0" in e for e in errors)

    def test_empty_mechanism_string(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.BLOCKED_BY,
            {"confidence": 0.5, "mechanism": "  "},
        )
        assert any("non-empty string" in e for e in errors)

    def test_non_numeric_confidence(self):
        errors = CausalEdgeValidator.validate_edge_properties(
            CausalEdgeType.INFLUENCED_BY,
            {"confidence": "high", "mechanism": "indirect"},
        )
        assert any("numeric" in e for e in errors)


class TestTemporalPrecedence:
    def test_valid_temporal_order(self):
        t1 = datetime(2024, 1, 1, tzinfo=UTC)
        t2 = datetime(2024, 6, 1, tzinfo=UTC)
        errors = CausalEdgeValidator.validate_temporal_precedence(
            t1,
            t2,
            CausalEdgeType.CAUSED_BY,
        )
        assert errors == []

    def test_invalid_temporal_order(self):
        t1 = datetime(2024, 6, 1, tzinfo=UTC)
        t2 = datetime(2024, 1, 1, tzinfo=UTC)
        errors = CausalEdgeValidator.validate_temporal_precedence(
            t1,
            t2,
            CausalEdgeType.TRIGGERED,
        )
        assert len(errors) == 1
        assert "Temporal precedence violated" in errors[0]

    def test_equal_timestamps_invalid(self):
        t1 = datetime(2024, 3, 15, tzinfo=UTC)
        errors = CausalEdgeValidator.validate_temporal_precedence(
            t1,
            t1,
            CausalEdgeType.DROVE,
        )
        assert len(errors) == 1

    def test_none_timestamps_error(self):
        errors = CausalEdgeValidator.validate_temporal_precedence(
            None,
            datetime(2024, 1, 1, tzinfo=UTC),
            CausalEdgeType.CAUSED_BY,
        )
        assert any("timestamps" in e for e in errors)

    def test_non_temporal_edge_no_validation(self):
        """INFLUENCED_BY and CONTRIBUTED_TO don't require temporal ordering."""
        errors = CausalEdgeValidator.validate_temporal_precedence(
            None,
            None,
            CausalEdgeType.INFLUENCED_BY,
        )
        assert errors == []

        errors = CausalEdgeValidator.validate_temporal_precedence(
            None,
            None,
            CausalEdgeType.CONTRIBUTED_TO,
        )
        assert errors == []


class TestIsValidEdgeType:
    def test_valid_types(self):
        for etype in CausalEdgeType:
            assert CausalEdgeValidator.is_valid_edge_type(etype.value) is True

    def test_invalid_type(self):
        assert CausalEdgeValidator.is_valid_edge_type("NOT_A_TYPE") is False


# ── CausalCompiler ──────────────────────────────────────


def _spec_with_causal(
    causal_edges: list[CausalEdgeSpec] | None = None,
    chain_depth: int = 5,
) -> DomainSpec:
    edges = causal_edges or [
        CausalEdgeSpec(
            edge_type="CAUSED_BY",
            source_label="Facility",
            target_label="Facility",
            temporal_validation=True,
        ),
        CausalEdgeSpec(
            edge_type="RESULTED_IN",
            source_label="Facility",
            target_label="TransactionOutcome",
            temporal_validation=True,
            confidence_threshold=0.5,
        ),
    ]
    return DomainSpec(
        domain={"id": "test", "name": "Test", "version": "0.0.1"},
        ontology={
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "facility_id", "type": "int", "required": True}],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
                {
                    "label": "TransactionOutcome",
                    "managedby": "api",
                    "properties": [{"name": "outcome_id", "type": "string", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "TRANSACTED_WITH",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                },
                {
                    "type": "CAUSED_BY",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "api",
                },
                {
                    "type": "RESULTED_IN",
                    "from": "Facility",
                    "to": "TransactionOutcome",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "api",
                },
            ],
        },
        matchentities={
            "candidate": [{"label": "Facility", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        queryschema={"matchdirections": ["d1"], "fields": []},
        gates=[],
        scoring={"dimensions": []},
        causal=CausalSubgraphSpec(
            enabled=True,
            causal_edges=edges,
            chain_depth_limit=chain_depth,
        ),
    )


class TestCausalCompiler:
    def test_compile_edge_create_basic(self):
        spec = _spec_with_causal()
        compiler = CausalCompiler(spec)
        edge_spec = spec.causal.causal_edges[0]  # CAUSED_BY

        cypher = compiler.compile_causal_edge_create(edge_spec)

        assert "MATCH (source:Facility" in cypher
        assert "MATCH (target:Facility" in cypher
        assert "CAUSED_BY" in cypher
        assert "$confidence" in cypher
        assert "$mechanism" in cypher
        assert "source.created_at < target.created_at" in cypher

    def test_compile_edge_create_with_confidence_threshold(self):
        spec = _spec_with_causal()
        compiler = CausalCompiler(spec)
        edge_spec = spec.causal.causal_edges[1]  # RESULTED_IN with threshold

        cypher = compiler.compile_causal_edge_create(edge_spec)

        assert "RESULTED_IN" in cypher
        # RU-068: the threshold is bound as a parameter (C-9); the former literal
        # `$confidence >= 0.5` assertion encoded the interpolation defect.
        assert "$confidence >= $confidence_threshold" in cypher
        assert "0.5" not in cypher

    def test_compile_edge_without_temporal_validation(self):
        spec = _spec_with_causal(
            causal_edges=[
                CausalEdgeSpec(
                    edge_type="INFLUENCED_BY",
                    source_label="Facility",
                    target_label="Facility",
                    temporal_validation=False,
                ),
            ]
        )
        compiler = CausalCompiler(spec)
        edge_spec = spec.causal.causal_edges[0]

        cypher = compiler.compile_causal_edge_create(edge_spec)

        assert "created_at < target.created_at" not in cypher

    def test_compile_chain_query(self):
        spec = _spec_with_causal(chain_depth=3)
        compiler = CausalCompiler(spec)

        cypher = compiler.compile_causal_chain_query("Facility")

        assert "MATCH path" in cypher
        assert "Facility" in cypher
        assert "*1..3" in cypher
        assert "chain_depth" in cypher

    def test_compile_chain_query_with_specific_types(self):
        spec = _spec_with_causal()
        compiler = CausalCompiler(spec)

        cypher = compiler.compile_causal_chain_query(
            "Facility",
            edge_types=["CAUSED_BY", "TRIGGERED"],
        )

        assert "CAUSED_BY|TRIGGERED" in cypher

    def test_compile_all_edge_creates(self):
        spec = _spec_with_causal()
        compiler = CausalCompiler(spec)

        compiled = compiler.compile_all_edge_creates()
        assert len(compiled) == 2
        assert all(isinstance(pair, tuple) for pair in compiled)
        assert all(len(pair) == 2 for pair in compiled)

    def test_compile_rejects_invalid_label(self):
        """Labels with injection attempts should be rejected by sanitize_label."""
        spec = _spec_with_causal(
            causal_edges=[
                CausalEdgeSpec(
                    edge_type="CAUSED_BY",
                    source_label="Facility",
                    target_label="Facility",
                ),
            ]
        )
        compiler = CausalCompiler(spec)

        # This should work fine with safe labels
        cypher = compiler.compile_causal_edge_create(spec.causal.causal_edges[0])
        assert "Facility" in cypher


# ── AttributionCalculator ──────────────────────────────


def _spec_with_attribution() -> DomainSpec:
    return DomainSpec(
        domain={"id": "test", "name": "Test", "version": "0.0.1"},
        ontology={
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "facility_id", "type": "int", "required": True}],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
                {
                    "label": "TransactionOutcome",
                    "managedby": "api",
                    "properties": [{"name": "outcome_id", "type": "string", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "TRANSACTED_WITH",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                },
                {
                    "type": "CONTRIBUTED_TO",
                    "from": "Facility",
                    "to": "TransactionOutcome",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "api",
                },
            ],
        },
        matchentities={
            "candidate": [{"label": "Facility", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        queryschema={"matchdirections": ["d1"], "fields": []},
        gates=[],
        scoring={"dimensions": []},
        feedbackloop=FeedbackLoopSpec(enabled=True),
        causal=CausalSubgraphSpec(
            enabled=True,
            attribution_enabled=True,
            causal_edges=[
                CausalEdgeSpec(
                    edge_type="CONTRIBUTED_TO",
                    source_label="Facility",
                    target_label="TransactionOutcome",
                ),
            ],
            chain_depth_limit=5,
        ),
    )


class TestAttributionModels:
    def test_first_touch_all_credit_to_first(self):
        results = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8, 0.7]},
            {"touchpoint_id": "tp3", "distance": 3, "confidences": [0.6, 0.5, 0.4]},
        ]
        weights = AttributionCalculator._assign_weights(results, "first_touch")

        assert weights["tp1"] == 1.0
        assert weights["tp2"] == 0.0
        assert weights["tp3"] == 0.0

    def test_last_touch_all_credit_to_last(self):
        results = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8, 0.7]},
        ]
        weights = AttributionCalculator._assign_weights(results, "last_touch")

        assert weights["tp1"] == 0.0
        assert weights["tp2"] == 1.0

    def test_linear_equal_credit(self):
        results = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8]},
            {"touchpoint_id": "tp3", "distance": 3, "confidences": [0.7]},
        ]
        weights = AttributionCalculator._assign_weights(results, "linear")

        expected = 1.0 / 3.0
        for tp_id in ("tp1", "tp2", "tp3"):
            assert weights[tp_id] == pytest.approx(expected, abs=0.001)

    def test_position_based_single(self):
        results = [{"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]}]
        weights = AttributionCalculator._assign_weights(results, "position_based")
        assert weights["tp1"] == 1.0

    def test_position_based_two(self):
        results = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8]},
        ]
        weights = AttributionCalculator._assign_weights(results, "position_based")
        assert weights["tp1"] == 0.5
        assert weights["tp2"] == 0.5

    def test_position_based_many(self):
        results = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8]},
            {"touchpoint_id": "tp3", "distance": 3, "confidences": [0.7]},
            {"touchpoint_id": "tp4", "distance": 4, "confidences": [0.6]},
        ]
        weights = AttributionCalculator._assign_weights(results, "position_based")

        assert weights["tp1"] == 0.4  # first touch
        assert weights["tp4"] == 0.4  # last touch
        # Middle: 0.2 / 2 = 0.1 each
        assert weights["tp2"] == 0.1
        assert weights["tp3"] == 0.1

    def test_empty_results(self):
        weights = AttributionCalculator._assign_weights([], "linear")
        assert weights == {}


class TestTemporalDecay:
    """Feature-flagged temporal-decay refinement of attribution weights."""

    def test_decay_factor_bounds(self):
        # age 0 -> no decay; age == halflife -> exp(-1); negative age -> no decay
        assert _temporal_decay_factor(0.0, 180.0) == 1.0
        assert _temporal_decay_factor(180.0, 180.0) == pytest.approx(math.exp(-1))
        assert _temporal_decay_factor(-5.0, 180.0) == 1.0

    def test_decay_factor_nonpositive_halflife_is_noop(self):
        assert _temporal_decay_factor(365.0, 0.0) == 1.0
        assert _temporal_decay_factor(365.0, -10.0) == 1.0

    def test_apply_decay_favors_recent_and_renormalizes(self):
        # Equal base credit; tp2 is one halflife old -> tp1 keeps more, sum == 1.0
        weights = {"tp1": 0.5, "tp2": 0.5}
        ages = {"tp1": 0.0, "tp2": 180.0}
        out = AttributionCalculator._apply_temporal_decay(weights, ages, 180.0)
        assert out["tp1"] > out["tp2"]
        assert sum(out.values()) == pytest.approx(1.0, abs=1e-6)
        # tp1 : tp2 == 1 : exp(-1)  after renormalization
        expected_ratio = 1.0 / math.exp(-1)
        assert out["tp1"] / out["tp2"] == pytest.approx(expected_ratio, abs=0.001)

    def test_apply_decay_single_touchpoint_stays_full_credit(self):
        # first_touch/last_touch put all credit on one touchpoint; decay+renorm
        # must leave it at 1.0 regardless of age.
        out = AttributionCalculator._apply_temporal_decay({"tp1": 1.0, "tp2": 0.0}, {"tp1": 365.0, "tp2": 0.0}, 180.0)
        assert out["tp1"] == 1.0
        assert out["tp2"] == 0.0

    def test_apply_decay_missing_age_is_noop(self):
        # No recorded age for any touchpoint -> factor 1.0 -> weights unchanged.
        weights = {"tp1": 0.5, "tp2": 0.5}
        out = AttributionCalculator._apply_temporal_decay(weights, {}, 180.0)
        assert out == {"tp1": 0.5, "tp2": 0.5}

    def test_apply_decay_all_zero_returns_base(self):
        # Degenerate: all base weights zero -> return base unchanged (no div-by-zero).
        weights = {"tp1": 0.0, "tp2": 0.0}
        out = AttributionCalculator._apply_temporal_decay(weights, {"tp1": 10.0}, 180.0)
        assert out == weights


class TestAttributionCalculator:
    @pytest.mark.asyncio
    async def test_invalid_model_raises(self):
        spec = _spec_with_attribution()
        driver = AsyncMock()
        calc = AttributionCalculator(driver, spec)

        with pytest.raises(ValueError, match="Invalid attribution model"):
            await calc.compute_attribution("out_1", model="invalid_model")

    @pytest.mark.asyncio
    async def test_no_touchpoints_returns_empty(self):
        spec = _spec_with_attribution()
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        calc = AttributionCalculator(driver, spec)

        result = await calc.compute_attribution("out_1", model="linear")

        assert result["touchpoints"] == {}
        assert result["chain_depth"] == 0

    @pytest.mark.asyncio
    async def test_compute_attribution_with_results(self):
        spec = _spec_with_attribution()
        driver = AsyncMock()
        driver.execute_query = AsyncMock(
            return_value=[
                {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
                {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8, 0.7]},
            ]
        )
        calc = AttributionCalculator(driver, spec)

        result = await calc.compute_attribution("out_1", model="linear")

        assert result["model"] == "linear"
        assert result["total_touchpoints"] == 2
        assert "tp1" in result["touchpoints"]
        assert "tp2" in result["touchpoints"]


# ── RU-067 / CEG-COR-055: weights over unique touchpoints ──

# A diamond: tp1 reaches the outcome directly and via tp2; tp3 via two parallel paths.
_DIAMOND_ROWS = [
    {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9], "ages_days": [1]},
    {"touchpoint_id": "tp2", "distance": 1, "confidences": [0.8], "ages_days": [2]},
    {"touchpoint_id": "tp1", "distance": 2, "confidences": [0.9, 0.8], "ages_days": [400, 2]},
    {"touchpoint_id": "tp3", "distance": 2, "confidences": [0.7, 0.8], "ages_days": [3, 2]},
    {"touchpoint_id": "tp3", "distance": 3, "confidences": [0.7, 0.6, 0.9], "ages_days": [3, 5, 1]},
]


class TestAttributionUniqueTouchpoints:
    @pytest.mark.parametrize("model", ["linear", "first_touch", "last_touch", "position_based"])
    def test_diamond_weights_sum_to_one(self, model: str) -> None:
        weights = AttributionCalculator._assign_weights(_DIAMOND_ROWS, model)
        assert set(weights) == {"tp1", "tp2", "tp3"}
        # Each weight is rounded to 6 decimals (unchanged behaviour), so three
        # linear shares sum to 0.999999: allow n * 0.5e-6 of rounding.
        assert sum(weights.values()) == pytest.approx(1.0, abs=1.5e-6)
        assert all(0.0 <= w <= 1.0 for w in weights.values())

    def test_first_touch_recurring_touchpoint_keeps_credit(self) -> None:
        rows = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.9, 0.8]},
            {"touchpoint_id": "tp1", "distance": 3, "confidences": [0.9, 0.8, 0.7]},
        ]
        weights = AttributionCalculator._assign_weights(rows, "first_touch")
        assert weights == {"tp1": 1.0, "tp2": 0.0}

    def test_linear_chain_without_duplicates_is_unchanged(self) -> None:
        rows = [
            {"touchpoint_id": "tp1", "distance": 1, "confidences": [0.9]},
            {"touchpoint_id": "tp2", "distance": 2, "confidences": [0.8]},
            {"touchpoint_id": "tp3", "distance": 3, "confidences": [0.7]},
        ]
        assert AttributionCalculator._assign_weights(rows, "position_based") == {"tp1": 0.4, "tp2": 0.2, "tp3": 0.4}

    @pytest.mark.asyncio
    async def test_compute_attribution_counts_unique_ids_and_ages_from_retained_row(self) -> None:
        from engine.config.schema import CausalSubgraphSpec

        spec = _spec_with_attribution()
        causal = spec.causal.model_copy(update={"temporal_decay_enabled": True})
        spec = spec.model_copy(update={"causal": causal})
        assert isinstance(spec.causal, CausalSubgraphSpec)
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=_DIAMOND_ROWS)
        result = await AttributionCalculator(driver, spec).compute_attribution("out_1", model="linear")
        assert result["total_touchpoints"] == 3
        assert sum(result["touchpoints"].values()) == pytest.approx(1.0, abs=1e-5)
        # tp1's retained (distance-1) row is 1 day old, so it is not decayed below tp2/tp3.
        assert result["touchpoints"]["tp1"] >= result["touchpoints"]["tp3"]

    def test_flag_off_restores_per_row_weighting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "causal_attribution_unique_touchpoints", False)
        weights = AttributionCalculator._assign_weights(_DIAMOND_ROWS, "linear")
        assert sum(weights.values()) == pytest.approx(0.6, abs=1e-6)  # legacy: 3 of 5 row shares


# ── RU-068 / CEG-COR-057: declared identities, anchored WHERE, stable MERGE ──


def _edge(**kw) -> CausalEdgeSpec:
    base = {"edge_type": "RESULTED_IN", "source_label": "Facility", "target_label": "TransactionOutcome"}
    return CausalEdgeSpec(**{**base, **kw})


class TestCausalCompilerIdentity:
    @pytest.mark.parametrize(("temporal", "threshold"), [(True, 0.0), (True, 0.5), (False, 0.5), (False, 0.0)])
    def test_predicates_share_one_where(self, temporal: bool, threshold: float) -> None:
        spec = _spec_with_causal(causal_edges=[_edge(temporal_validation=temporal, confidence_threshold=threshold)])
        cypher = CausalCompiler(spec).compile_causal_edge_create(spec.causal.causal_edges[0])
        expected_where = 1 if (temporal or threshold > 0) else 0
        assert cypher.count("WHERE") == expected_where
        assert "\nAND " not in cypher
        assert ("source.created_at < target.created_at" in cypher) is temporal
        assert ("$confidence >= $confidence_threshold" in cypher) is (threshold > 0)
        if expected_where:
            assert cypher.index("WHERE") < cypher.index("MERGE")

    def test_target_outcome_matched_by_outcome_id_source_by_declared_id(self) -> None:
        spec = _spec_with_causal(causal_edges=[_edge()])
        cypher = CausalCompiler(spec).compile_causal_edge_create(spec.causal.causal_edges[0])
        assert "MATCH (target:TransactionOutcome {outcome_id: $target_id})" in cypher
        assert "MATCH (source:Facility {facility_id: $source_id})" in cypher
        assert "entity_id" not in cypher

    def test_explicit_id_properties_win_and_are_sanitized(self) -> None:
        spec = _spec_with_causal(causal_edges=[_edge()])
        compiler = CausalCompiler(spec)
        cypher = compiler.compile_causal_edge_create(spec.causal.causal_edges[0], source_id_property="depot_code")
        assert "{depot_code: $source_id}" in cypher
        with pytest.raises(ValueError):
            compiler.compile_causal_edge_create(spec.causal.causal_edges[0], source_id_property="x}) DETACH DELETE (n")

    def test_merge_has_no_properties_and_sets_on_create_and_match(self) -> None:
        spec = _spec_with_causal(causal_edges=[_edge()])
        cypher = CausalCompiler(spec).compile_causal_edge_create(spec.causal.causal_edges[0])
        assert "MERGE (source)-[r:RESULTED_IN]->(target)" in cypher
        assert "ON CREATE SET" in cypher
        assert "ON MATCH SET" in cypher
        merge_line = next(line for line in cypher.splitlines() if line.startswith("MERGE"))
        assert "datetime()" not in merge_line

    def test_label_without_declaration_falls_back_to_entity_id(self) -> None:
        spec = _spec_with_causal(
            causal_edges=[_edge(edge_type="CAUSED_BY", source_label="Unknown", target_label="Facility")]
        )
        cypher = CausalCompiler(spec).compile_causal_edge_create(spec.causal.causal_edges[0])
        assert "MATCH (source:Unknown {entity_id: $source_id})" in cypher
        assert "MATCH (target:Facility {facility_id: $target_id})" in cypher

    def test_flag_off_restores_legacy_cypher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "causal_edge_declared_identity", False)
        spec = _spec_with_causal(causal_edges=[_edge(temporal_validation=False, confidence_threshold=0.5)])
        cypher = CausalCompiler(spec).compile_causal_edge_create(spec.causal.causal_edges[0])
        assert cypher == (
            "MATCH (source:Facility {entity_id: $source_id})\n"
            "MATCH (target:TransactionOutcome {entity_id: $target_id})\n"
            "AND $confidence >= 0.5\n"
            "MERGE (source)-[r:RESULTED_IN {\n"
            "    confidence: $confidence,\n"
            "    mechanism: $mechanism,\n"
            "    created_at: datetime()\n"
            "}]->(target)\n"
            "RETURN r"
        )


class TestAttributionTouchpointIdentity:
    """PO-04 (folded into CEG-COR-002 / RU-068): the trace reads the declared id, not entity_id."""

    @pytest.mark.asyncio
    async def test_trace_returns_declared_id_per_source_label(self) -> None:
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        await AttributionCalculator(driver, _spec_with_attribution()).compute_attribution("out_1")
        cypher = driver.execute_query.call_args.args[0]
        assert (
            "RETURN CASE WHEN touchpoint:Facility THEN coalesce(touchpoint.facility_id, touchpoint.entity_id) "
            "ELSE touchpoint.entity_id END AS touchpoint_id" in cypher
        )

    @pytest.mark.asyncio
    async def test_flag_off_reads_entity_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "causal_edge_declared_identity", False)
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        await AttributionCalculator(driver, _spec_with_attribution()).compute_attribution("out_1")
        assert "RETURN touchpoint.entity_id AS touchpoint_id" in driver.execute_query.call_args.args[0]
