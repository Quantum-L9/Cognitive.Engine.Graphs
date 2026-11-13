"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, wave1, invariants, sel4]
owner: engine-team
status: active
--- /L9_META ---

Tests for Wave 1: Invariant & Validation Hardening (seL4-inspired).
Covers W1-01 through W1-05 feature flags, validators, and error paths.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from engine.config.schema import (
    DomainSpec,
    GateSpec,
    GateType,
    NullBehavior,
)
from engine.config.settings import Settings
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

# Canonical patch target — all `from engine.config.settings import settings`
# binds the module-level singleton, so we patch the object at its source.
_SETTINGS_TARGET = "engine.config.settings.settings"


# ── Helpers ──────────────────────────────────────────────


def _minimal_spec_raw(**overrides: Any) -> dict[str, Any]:
    """Build a minimal domain spec dict, with optional overrides."""
    base: dict[str, Any] = {
        "domain": {"id": "test", "name": "Test Domain", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "supply_opportunity_to_buyer_facility",
                    "properties": [
                        {"name": "facility_id", "type": "int", "required": True},
                        {"name": "name", "type": "string"},
                        {"name": "lat", "type": "float"},
                        {"name": "lon", "type": "float"},
                        {"name": "credit_score", "type": "float"},
                        {"name": "min_density", "type": "float"},
                        {"name": "max_density", "type": "float"},
                        {"name": "is_active", "type": "bool"},
                    ],
                },
                {
                    "label": "MaterialIntake",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "supply_opportunity_to_buyer_facility",
                    "properties": [
                        {"name": "intake_id", "type": "int", "required": True},
                    ],
                },
            ],
            "edges": [
                {
                    "type": "EXCLUDED_FROM",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "exclusion",
                    "managedby": "sync",
                },
            ],
        },
        "matchentities": {
            "candidate": [{"label": "Facility", "matchdirection": "supply_opportunity_to_buyer_facility"}],
            "queryentity": [{"label": "MaterialIntake", "matchdirection": "supply_opportunity_to_buyer_facility"}],
        },
        "queryschema": {
            "matchdirections": ["supply_opportunity_to_buyer_facility"],
            "fields": [
                {"name": "density", "type": "float"},
                {"name": "target_score", "type": "float"},
            ],
        },
        "traversal": {"steps": []},
        "gates": [],
        "scoring": {"dimensions": []},
    }
    base.update(overrides)
    return base


def _make_settings(**overrides: Any) -> Settings:
    """Create a Settings instance with overrides (avoids .env side effects)."""
    defaults = {
        "neo4j_password": "test-pw",
        "api_secret_key": "test-key",
        "l9_env": "dev",
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-01: Domain-Spec Cross-Reference Invariant Validators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW101DomainCrossRef:
    """W1-01: Domain-spec cross-reference validators."""

    def test_valid_spec_passes(self) -> None:
        """A well-formed spec passes all cross-reference checks."""
        raw = _minimal_spec_raw()
        spec = DomainSpec.model_validate(raw)
        assert spec.domain.id == "test"

    def test_edge_source_unknown_node_raises(self) -> None:
        """W1-01(a): Edge source referencing an undeclared node is rejected."""
        raw = _minimal_spec_raw()
        raw["ontology"]["edges"].append(
            {
                "type": "LINKS_TO",
                "from": "GhostNode",
                "to": "Facility",
                "direction": "DIRECTED",
                "category": "capability",
                "managedby": "sync",
            }
        )
        with pytest.raises(ValueError, match="source 'GhostNode' not found"):
            DomainSpec.model_validate(raw)

    def test_edge_target_unknown_node_raises(self) -> None:
        """W1-01(a): Edge target referencing an undeclared node is rejected."""
        raw = _minimal_spec_raw()
        raw["ontology"]["edges"].append(
            {
                "type": "LINKS_TO",
                "from": "Facility",
                "to": "PhantomNode",
                "direction": "DIRECTED",
                "category": "capability",
                "managedby": "sync",
            }
        )
        with pytest.raises(ValueError, match="target 'PhantomNode' not found"):
            DomainSpec.model_validate(raw)

    def test_edge_validation_skipped_when_flag_off(self) -> None:
        """W1-01: Edge validation is skipped when DOMAIN_STRICT_VALIDATION=False."""
        raw = _minimal_spec_raw()
        raw["ontology"]["edges"].append(
            {
                "type": "LINKS_TO",
                "from": "GhostNode",
                "to": "Facility",
                "direction": "DIRECTED",
                "category": "capability",
                "managedby": "sync",
            }
        )
        s = _make_settings(domain_strict_validation=False)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
            assert spec is not None

    def test_scoring_weight_sum_exceeds_ceiling_raises(self) -> None:
        """W1-01(c): Total scoring default weights > 1.0 is rejected."""
        raw = _minimal_spec_raw()
        raw["scoring"] = {
            "dimensions": [
                {
                    "name": "dim1",
                    "source": "candidateproperty",
                    "candidateprop": "credit_score",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.6,
                },
                {
                    "name": "dim2",
                    "source": "candidateproperty",
                    "candidateprop": "lat",
                    "computation": "candidateproperty",
                    "weightkey": "w2",
                    "defaultweight": 0.6,
                },
            ],
        }
        with pytest.raises(ValueError, match="weights sum to"):
            DomainSpec.model_validate(raw)

    def test_scoring_weight_sum_at_ceiling_passes(self) -> None:
        """W1-01(c): Weights summing to exactly 1.0 are allowed."""
        raw = _minimal_spec_raw()
        raw["scoring"] = {
            "dimensions": [
                {
                    "name": "dim1",
                    "source": "candidateproperty",
                    "candidateprop": "credit_score",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
                {
                    "name": "dim2",
                    "source": "candidateproperty",
                    "candidateprop": "lat",
                    "computation": "candidateproperty",
                    "weightkey": "w2",
                    "defaultweight": 0.5,
                },
            ],
        }
        spec = DomainSpec.model_validate(raw)
        assert len(spec.scoring.dimensions) == 2

    @staticmethod
    def _mixed_scope_dimensions(scoped: float, unscoped: float) -> dict[str, Any]:
        """One dimension scoped to the spec's direction, one unscoped (applies to every direction)."""
        return {
            "dimensions": [
                {
                    "name": "scoped_dim",
                    "source": "candidateproperty",
                    "candidateprop": "credit_score",
                    "computation": "candidateproperty",
                    "weightkey": "w_scoped",
                    "defaultweight": scoped,
                    "matchdirections": ["supply_opportunity_to_buyer_facility"],
                },
                {
                    "name": "unscoped_dim",
                    "source": "candidateproperty",
                    "candidateprop": "lat",
                    "computation": "candidateproperty",
                    "weightkey": "w_unscoped",
                    "defaultweight": unscoped,
                },
            ],
        }

    def test_scoring_weight_mixed_scope_exceeds_ceiling_raises(self) -> None:
        """RU-044 / CEG-COR-024: unscoped weight counts toward every declared direction.

        Effective weight for the direction is 0.6 (scoped) + 0.6 (unscoped) = 1.2.
        """
        raw = _minimal_spec_raw()
        raw["scoring"] = self._mixed_scope_dimensions(scoped=0.6, unscoped=0.6)
        with pytest.raises(ValueError, match="'supply_opportunity_to_buyer_facility'"):
            DomainSpec.model_validate(raw)

    def test_scoring_weight_mixed_scope_at_ceiling_passes(self) -> None:
        """RU-044: scoped 0.4 + unscoped 0.6 is exactly the ceiling and is accepted."""
        raw = _minimal_spec_raw()
        raw["scoring"] = self._mixed_scope_dimensions(scoped=0.4, unscoped=0.6)
        spec = DomainSpec.model_validate(raw)
        assert len(spec.scoring.dimensions) == 2

    def test_scoring_weight_mixed_scope_flag_off_keeps_bucket_check(self) -> None:
        """RU-044 flag off: each scope bucket is checked alone (legacy), so 0.6 + 0.6 loads."""
        raw = _minimal_spec_raw()
        raw["scoring"] = self._mixed_scope_dimensions(scoped=0.6, unscoped=0.6)
        s = _make_settings(weight_ceiling_includes_unscoped=False)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
        assert len(spec.scoring.dimensions) == 2

    def test_scoring_weight_mixed_scope_skipped_without_strict_validation(self) -> None:
        """RU-044 stays gated by domain_strict_validation, as the bucket check is today."""
        raw = _minimal_spec_raw()
        raw["scoring"] = self._mixed_scope_dimensions(scoped=0.6, unscoped=0.6)
        s = _make_settings(domain_strict_validation=False)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
        assert len(spec.scoring.dimensions) == 2

    @staticmethod
    def _spec_with_gate(gate: dict[str, Any], extra_fields: list[str] | None = None) -> dict[str, Any]:
        raw = _minimal_spec_raw()
        for name in extra_fields or []:
            raw["queryschema"]["fields"].append({"name": name, "type": "datetime"})
        raw["gates"] = [gate]
        return raw

    _THRESHOLD_ABSENT = {
        "name": "tw",
        "type": "threshold",
        "candidateprop": "credit_score",
        "operator": ">=",
        "queryparam": "window",
    }
    _TEMPORAL_WINDOW = {"name": "tw", "type": "temporalrange", "candidateprop": "name", "queryparam": "window"}

    def test_undeclared_gate_param_raises(self) -> None:
        """RU-050 / CEG-COR-077: a gate naming an absent parameter fails validation (was warning only)."""
        raw = self._spec_with_gate(dict(self._THRESHOLD_ABSENT))
        with pytest.raises(ValueError, match="Gate 'tw' references undeclared parameter 'window'"):
            DomainSpec.model_validate(raw)

    def test_temporalrange_derived_params_declared_passes(self, caplog: pytest.LogCaptureFixture) -> None:
        """RU-050: temporalrange {queryparam: window} with window_start and window_end declared validates."""
        raw = self._spec_with_gate(dict(self._TEMPORAL_WINDOW), ["window_start", "window_end"])
        with caplog.at_level("WARNING", logger="engine.config.schema"):
            spec = DomainSpec.model_validate(raw)
        assert spec.gates[0].queryparam == "window"
        assert "undeclared parameter" not in caplog.text

    @pytest.mark.parametrize("declared", [[], ["window_start"], ["window_end"]])
    def test_temporalrange_derived_params_missing_raises(self, declared: list[str]) -> None:
        """RU-050: temporalrange needs both <q>_start and <q>_end declared; neither or one alone raises."""
        raw = self._spec_with_gate(dict(self._TEMPORAL_WINDOW), declared)
        with pytest.raises(ValueError, match="references undeclared parameter 'window'"):
            DomainSpec.model_validate(raw)

    def test_suffixed_param_with_declared_base_passes(self) -> None:
        """RU-050 keeps the `<base>_min/_max/_start/_end` exemption for a declared base."""
        gate = dict(self._THRESHOLD_ABSENT, queryparam="density_min")
        spec = DomainSpec.model_validate(self._spec_with_gate(gate))
        assert spec.gates[0].queryparam == "density_min"

    def test_literal_queryparam_is_not_a_reference(self) -> None:
        """RU-050 keeps literal queryparam operands exempt."""
        gate = dict(self._THRESHOLD_ABSENT, queryparam=85.0)
        spec = DomainSpec.model_validate(self._spec_with_gate(gate))
        assert spec.gates[0].queryparam == pytest.approx(85.0)

    def test_undeclared_gate_param_flag_off_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """RU-050 flag off: the legacy warning, and the spec loads."""
        raw = self._spec_with_gate(dict(self._THRESHOLD_ABSENT))
        s = _make_settings(domain_undeclared_param_rejected=False)
        with patch(_SETTINGS_TARGET, s), caplog.at_level("WARNING", logger="engine.config.schema"):
            spec = DomainSpec.model_validate(raw)
        assert spec.gates[0].queryparam == "window"
        assert "Gate 'tw' references undeclared parameter 'window'" in caplog.text

    def test_undeclared_gate_param_skipped_without_strict_validation(self) -> None:
        """RU-050 applies only under domain_strict_validation, as the warning did."""
        raw = self._spec_with_gate(dict(self._THRESHOLD_ABSENT))
        with patch(_SETTINGS_TARGET, _make_settings(domain_strict_validation=False)):
            spec = DomainSpec.model_validate(raw)
        assert spec.gates[0].queryparam == "window"

    def test_gate_exclusion_unknown_edge_raises(self) -> None:
        """Exclusion gate referencing unknown edge type is rejected."""
        raw = _minimal_spec_raw()
        raw["gates"] = [
            {
                "name": "excl_gate",
                "type": "exclusion",
                "edgetype": "NONEXISTENT_EDGE",
                "nullbehavior": "pass",
            }
        ]
        with pytest.raises(ValueError, match="unknown edge type"):
            DomainSpec.model_validate(raw)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-02: Score-Range Invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW102ScoreRange:
    """W1-02: Score clamping and weight validation."""

    def test_clamp_expression(self) -> None:
        """The _clamp_expression wraps an expression in a CASE clamp."""
        from engine.scoring.assembler import ScoringAssembler

        result = ScoringAssembler._clamp_expression("candidate.score")
        assert "CASE WHEN" in result
        assert "< 0.0 THEN 0.0" in result
        assert "> 1.0 THEN 1.0" in result
        assert "candidate.score" in result

    def test_clamp_expression_maps_null_to_zero(self) -> None:
        """A null dimension (unscored property) clamps to 0.0, not to null.

        Before this branch a candidate with a null scored property produced a
        null weighted ``score`` and ``handle_match`` crashed ranking it
        (``'>' not supported between instances of 'NoneType'``). The CASE shape
        is kept so the structural assertions in tests/property still hold.
        """
        from engine.scoring.assembler import ScoringAssembler

        result = ScoringAssembler._clamp_expression("candidate.score")
        assert result.startswith("CASE WHEN (candidate.score) IS NULL THEN 0.0 ")
        assert result.endswith("ELSE (candidate.score) END")

    def test_clamp_expression_maps_nan_to_zero(self) -> None:
        """RU-031 / CEG-COR-166 (RG-039): NaN compares false to both bounds and fell to ELSE.

        ``log(1 + x)`` for x < -1 or ``0.0 / 0.0`` is NaN in Cypher; NaN is
        treated like null (``NullStrategy.ZERO``) and clamps to 0.0, tested
        right after IS NULL so ``isNaN`` never sees a null.
        """
        from engine.scoring.assembler import ScoringAssembler

        result = ScoringAssembler._clamp_expression("x")
        assert "WHEN isNaN((x)) THEN 0.0" in result
        assert result.index("IS NULL THEN 0.0") < result.index("isNaN((x))") < result.index("< 0.0 THEN 0.0")
        assert "> 1.0 THEN 1.0" in result
        assert result.endswith("ELSE (x) END")

    def test_scoring_assembler_clamps_when_enabled(self) -> None:
        """Dimension expressions are clamped when SCORE_CLAMP_ENABLED=True."""
        raw = _minimal_spec_raw()
        raw["scoring"] = {
            "dimensions": [
                {
                    "name": "test_dim",
                    "source": "candidateproperty",
                    "candidateprop": "credit_score",
                    "computation": "candidateproperty",
                    "weightkey": "w_test",
                    "defaultweight": 0.5,
                },
            ],
        }
        spec = DomainSpec.model_validate(raw)

        from engine.scoring.assembler import ScoringAssembler

        assembler = ScoringAssembler(spec)
        s = _make_settings(score_clamp_enabled=True)
        with patch(_SETTINGS_TARGET, s):
            clause, _ = assembler.assemble_scoring_clause("supply_opportunity_to_buyer_facility", {})
        assert "CASE WHEN" in clause

    def test_scoring_assembler_no_clamp_when_disabled(self) -> None:
        """Dimension expressions are NOT clamped when SCORE_CLAMP_ENABLED=False."""
        raw = _minimal_spec_raw()
        raw["scoring"] = {
            "dimensions": [
                {
                    "name": "test_dim",
                    "source": "candidateproperty",
                    "candidateprop": "credit_score",
                    "computation": "candidateproperty",
                    "weightkey": "w_test",
                    "defaultweight": 0.5,
                },
            ],
        }
        spec = DomainSpec.model_validate(raw)

        from engine.scoring.assembler import ScoringAssembler

        assembler = ScoringAssembler(spec)
        s = _make_settings(score_clamp_enabled=False)
        with patch(_SETTINGS_TARGET, s):
            clause, _ = assembler.assemble_scoring_clause("supply_opportunity_to_buyer_facility", {})
        # The raw expression (before AS test_dim) should NOT be wrapped in CASE clamp
        before_alias = clause.split("AS test_dim")[0]
        assert "CASE WHEN" not in before_alias

    def test_match_weight_validation_rejects_negative(self) -> None:
        """User-supplied negative weight is rejected."""
        from engine.handlers import ValidationError, _validate_match_weights

        with pytest.raises(ValidationError, match="outside allowed range"):
            _validate_match_weights({"w1": -0.1}, "test-tenant")

    def test_match_weight_validation_rejects_over_one(self) -> None:
        """User-supplied weight > 1.0 is rejected."""
        from engine.handlers import ValidationError, _validate_match_weights

        with pytest.raises(ValidationError, match="outside allowed range"):
            _validate_match_weights({"w1": 1.5}, "test-tenant")

    def test_match_weight_validation_rejects_sum_over_one(self) -> None:
        """Weights summing to > 1.0 are rejected."""
        from engine.handlers import ValidationError, _validate_match_weights

        with pytest.raises(ValidationError, match="Weights sum to"):
            _validate_match_weights({"w1": 0.6, "w2": 0.6}, "test-tenant")

    def test_match_weight_validation_valid_passes(self) -> None:
        """Valid weights pass without error."""
        from engine.handlers import _validate_match_weights

        _validate_match_weights({"w1": 0.3, "w2": 0.3}, "test-tenant")

    def test_no_engine_wide_default_weight_set(self) -> None:
        """The fixed four-dimension weights are retired; the ceiling is per spec (TestW101DomainCrossRef)."""
        from engine import boot

        for retired in ("w_structural", "w_geo", "w_reinforcement", "w_freshness"):
            assert retired not in Settings.model_fields
        assert not hasattr(boot, "_assert_default_weight_sum")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-03: Gate Compilation Invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW103GateInvariants:
    """W1-03: Null-semantic and type-consistency gate checks."""

    def test_validate_gates_missing_param(self) -> None:
        """Missing parameter in resolved set is flagged."""
        raw = _minimal_spec_raw()
        raw["gates"] = [
            {
                "name": "threshold_gate",
                "type": "threshold",
                "candidateprop": "credit_score",
                "queryparam": "target_score",
                "operator": ">=",
                "nullbehavior": "pass",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.gates.compiler import GateCompiler

        compiler = GateCompiler(spec)
        s = _make_settings(strict_null_gates=True)
        with patch(_SETTINGS_TARGET, s):
            warnings = compiler.validate_gates(resolved_params={"other_param": 42})
        assert any("target_score" in w for w in warnings)

    def test_validate_gates_null_param(self) -> None:
        """Null-resolved parameter is flagged when strict mode on."""
        raw = _minimal_spec_raw()
        raw["gates"] = [
            {
                "name": "threshold_gate",
                "type": "threshold",
                "candidateprop": "credit_score",
                "queryparam": "target_score",
                "operator": ">=",
                "nullbehavior": "pass",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.gates.compiler import GateCompiler

        compiler = GateCompiler(spec)
        s = _make_settings(strict_null_gates=True)
        with patch(_SETTINGS_TARGET, s):
            warnings = compiler.validate_gates(resolved_params={"target_score": None})
        assert any("resolved to null" in w for w in warnings)

    def test_validate_gates_operator_type_mismatch(self) -> None:
        """Numeric operator on string property is flagged."""
        raw = _minimal_spec_raw()
        raw["gates"] = [
            {
                "name": "name_gate",
                "type": "threshold",
                "candidateprop": "name",
                "queryparam": "target_score",
                "operator": ">=",
                "nullbehavior": "pass",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.gates.compiler import GateCompiler

        compiler = GateCompiler(spec)
        s = _make_settings(strict_null_gates=True)
        with patch(_SETTINGS_TARGET, s):
            warnings = compiler.validate_gates(resolved_params={"target_score": 5})
        assert any("incompatible" in w for w in warnings)

    def test_validate_gates_clean_passes(self) -> None:
        """Well-formed gates produce no warnings."""
        raw = _minimal_spec_raw()
        raw["gates"] = [
            {
                "name": "threshold_gate",
                "type": "threshold",
                "candidateprop": "credit_score",
                "queryparam": "target_score",
                "operator": ">=",
                "nullbehavior": "pass",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.gates.compiler import GateCompiler

        compiler = GateCompiler(spec)
        s = _make_settings(strict_null_gates=True)
        with patch(_SETTINGS_TARGET, s):
            warnings = compiler.validate_gates(resolved_params={"target_score": 5.0})
        assert len(warnings) == 0

    def test_gate_operator_validation_rejects_bad_operator(self) -> None:
        """W1-03: GateSpec rejects unknown operators."""
        with pytest.raises(ValueError, match="not in the allowed set"):
            GateSpec(
                name="bad_op",
                type=GateType.THRESHOLD,
                candidateprop="credit_score",
                queryparam="target",
                operator="LIKE",
                nullbehavior=NullBehavior.PASS,
            )

    def test_gate_operator_validation_allows_valid(self) -> None:
        """W1-03: GateSpec accepts known operators."""
        gate = GateSpec(
            name="good_op",
            type=GateType.THRESHOLD,
            candidateprop="credit_score",
            queryparam="target",
            operator=">=",
            nullbehavior=NullBehavior.PASS,
        )
        assert gate.operator == ">="


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-04: Traversal Pattern Validators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW104TraversalValidators:
    """W1-04: Hop count, direction, and label-reference validators."""

    def test_hop_count_within_cap_passes(self) -> None:
        """Traversal with hops within MAX_HOP_HARD_CAP passes."""
        raw = _minimal_spec_raw()
        raw["traversal"] = {
            "steps": [
                {
                    "name": "step1",
                    "pattern": "(candidate:Facility)-[:EXCLUDED_FROM*1..3]->(target:Facility)",
                    "required": True,
                },
            ],
        }
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
        assert spec.traversal is not None

    def test_hop_count_exceeds_cap_raises(self) -> None:
        """Traversal with hops exceeding MAX_HOP_HARD_CAP is rejected."""
        raw = _minimal_spec_raw()
        raw["traversal"] = {
            "steps": [
                {
                    "name": "step1",
                    "pattern": "(candidate:Facility)-[:EXCLUDED_FROM*1..15]->(target:Facility)",
                    "required": True,
                },
            ],
        }
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match="exceeds hard cap"):
                DomainSpec.model_validate(raw)

    @staticmethod
    def _spec_with_step_pattern(pattern: str) -> dict[str, Any]:
        raw = _minimal_spec_raw()
        raw["traversal"] = {"steps": [{"name": "step1", "pattern": pattern, "required": True}]}
        return raw

    @pytest.mark.parametrize("hops", ["*1..", "*2..", "*..", "*"])
    def test_open_hop_range_raises(self, hops: str) -> None:
        """RU-045 / CEG-COR-038: a variable-length step with no explicit upper bound is refused."""
        raw = self._spec_with_step_pattern(f"(candidate:Facility)-[:EXCLUDED_FROM{hops}]->(target:Facility)")
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match=r"no explicit upper bound|exceeds hard cap"):
                DomainSpec.model_validate(raw)

    @pytest.mark.parametrize("hops", ["*1..", "*2.."])
    def test_open_hop_range_names_the_missing_bound(self, hops: str) -> None:
        """RU-045: the refusal of `*N..` names the open range (it was accepted before)."""
        raw = self._spec_with_step_pattern(f"(candidate:Facility)-[:EXCLUDED_FROM{hops}]->(target:Facility)")
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match="no explicit upper bound"):
                DomainSpec.model_validate(raw)

    @pytest.mark.parametrize(
        ("hops", "message"),
        [("*0..", "hop count must be >= 1"), ("*..20", "exceeds hard cap"), ("*0..3", "hop count must be >= 1")],
    )
    def test_existing_hop_refusals_preserved(self, hops: str, message: str) -> None:
        """RU-045 preserves the existing `*0..` / `*..20` refusals and their messages."""
        raw = self._spec_with_step_pattern(f"(candidate:Facility)-[:EXCLUDED_FROM{hops}]->(target:Facility)")
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match=message):
                DomainSpec.model_validate(raw)

    @pytest.mark.parametrize("hops", ["*1..3", "*2", "*..3", ""])
    def test_bounded_hop_ranges_accepted(self, hops: str) -> None:
        """RU-045 preserves `*1..3`, `*2`, `*..3` and fixed one-hop steps."""
        pattern = f"(candidate:Facility)-[:EXCLUDED_FROM{hops}]->(target:Facility)"
        raw = self._spec_with_step_pattern(pattern)
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
        assert spec.traversal is not None

        from engine.traversal.assembler import _validate_traversal_pattern

        with patch(_SETTINGS_TARGET, s):
            assert _validate_traversal_pattern(pattern, "step1") == pattern

    @pytest.mark.parametrize("hops", ["*1..", "*2..", "*..", "*"])
    def test_assembler_rejects_open_hop_range(self, hops: str) -> None:
        """RU-045 defence in depth: the assembler grammar requires an explicit upper bound."""
        from engine.traversal.assembler import _validate_traversal_pattern

        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match="does not match allowed structure"):
                _validate_traversal_pattern(f"(a:A)-[:R{hops}]->(b:B)", "s")

    @pytest.mark.parametrize("hops", ["*1..", "*2.."])
    def test_open_hop_range_flag_off_restores_acceptance(self, hops: str) -> None:
        """RU-045 flag off: `*N..` loads and the assembler grammar accepts it, as before."""
        from engine.traversal.assembler import _validate_traversal_pattern

        pattern = f"(candidate:Facility)-[:EXCLUDED_FROM{hops}]->(target:Facility)"
        raw = self._spec_with_step_pattern(pattern)
        s = _make_settings(max_hop_hard_cap=10, traversal_open_range_rejected=False)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)
            assert spec.traversal is not None
            assert _validate_traversal_pattern(pattern, "step1") == pattern

    def test_hop_count_zero_raises(self) -> None:
        """Traversal with 0 hops is rejected."""
        raw = _minimal_spec_raw()
        raw["traversal"] = {
            "steps": [
                {
                    "name": "step1",
                    "pattern": "(candidate:Facility)-[:EXCLUDED_FROM*0..3]->(target:Facility)",
                    "required": True,
                },
            ],
        }
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ValueError, match="hop count must be >= 1"):
                DomainSpec.model_validate(raw)

    def test_validate_traversal_undeclared_label_warning(self) -> None:
        """W1-04: References to undeclared labels/types produce warnings."""
        raw = _minimal_spec_raw()
        raw["traversal"] = {
            "steps": [
                {
                    "name": "step1",
                    "pattern": "(candidate:Facility)-[:UNKNOWN_EDGE]->(target:Facility)",
                    "required": True,
                },
            ],
        }
        s = _make_settings(max_hop_hard_cap=10)
        with patch(_SETTINGS_TARGET, s):
            spec = DomainSpec.model_validate(raw)

        from engine.traversal.assembler import TraversalAssembler

        assembler = TraversalAssembler(spec)
        warnings = assembler.validate_traversal("supply_opportunity_to_buyer_facility")
        assert any("UNKNOWN_EDGE" in w for w in warnings)

    def test_empty_traversal_passes(self) -> None:
        """Spec with no traversal steps passes validation."""
        raw = _minimal_spec_raw()
        raw["traversal"] = {"steps": []}
        spec = DomainSpec.model_validate(raw)
        assert spec.traversal is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-05: Parameter Resolver Strict Mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW105ParamStrictMode:
    """W1-05: Parameter resolver strict mode tests."""

    def test_strict_mode_raises_on_bad_expression(self) -> None:
        """Strict mode raises ParameterResolutionError on failed derived param."""
        raw = _minimal_spec_raw()
        raw["derivedparameters"] = [
            {
                "name": "monthly_income",
                "expression": "annualincomeusd / 12.0",
                "type": "float",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.traversal.resolver import ParameterResolutionError, ParameterResolver

        resolver = ParameterResolver(spec)
        s = _make_settings(param_strict_mode=True)
        with patch(_SETTINGS_TARGET, s):
            with pytest.raises(ParameterResolutionError, match="monthly_income"):
                # Missing 'annualincomeusd' in query data → expression fails
                resolver.resolve_parameters({"density": 0.95})

    def test_lenient_mode_swallows_error(self) -> None:
        """Lenient mode logs but doesn't raise on failed derived param."""
        raw = _minimal_spec_raw()
        raw["derivedparameters"] = [
            {
                "name": "monthly_income",
                "expression": "annualincomeusd / 12.0",
                "type": "float",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.traversal.resolver import ParameterResolver

        resolver = ParameterResolver(spec)
        s = _make_settings(param_strict_mode=False)
        with patch(_SETTINGS_TARGET, s):
            result = resolver.resolve_parameters({"density": 0.95})
        # Parameter not resolved — should be absent, not errored
        assert "monthly_income" not in result

    def test_strict_mode_succeeds_for_valid_expression(self) -> None:
        """Strict mode passes when expression evaluates successfully."""
        raw = _minimal_spec_raw()
        raw["derivedparameters"] = [
            {
                "name": "monthly_income",
                "expression": "annualincomeusd / 12.0",
                "type": "float",
            },
        ]
        spec = DomainSpec.model_validate(raw)

        from engine.traversal.resolver import ParameterResolver

        resolver = ParameterResolver(spec)
        s = _make_settings(param_strict_mode=True)
        with patch(_SETTINGS_TARGET, s):
            result = resolver.resolve_parameters({"annualincomeusd": 120000.0})
        assert result["monthly_income"] == 10000.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Feature Flag Toggle Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFeatureFlags:
    """Verify all 5 feature flags exist and default to expected values."""

    def test_all_flags_present(self) -> None:
        """All Wave 1 flags are present in Settings with correct defaults."""
        s = _make_settings()
        assert s.domain_strict_validation is True
        assert s.score_clamp_enabled is True
        assert s.strict_null_gates is True
        assert s.max_hop_hard_cap == 10
        assert s.param_strict_mode is True

    def test_flags_can_be_disabled(self) -> None:
        """All flags can be toggled off."""
        s = _make_settings(
            domain_strict_validation=False,
            score_clamp_enabled=False,
            strict_null_gates=False,
            max_hop_hard_cap=5,
            param_strict_mode=False,
        )
        assert s.domain_strict_validation is False
        assert s.score_clamp_enabled is False
        assert s.strict_null_gates is False
        assert s.max_hop_hard_cap == 5
        assert s.param_strict_mode is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RU-036 / CEG-COR-007: one row per candidate before ORDER BY / LIMIT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _MatchCypherCapture:
    """Fake GraphDriver that records the match Cypher and returns no rows."""

    def __init__(self) -> None:
        self.cyphers: list[str] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.cyphers.append(cypher)
        return []


_DOMAINS_DIR = DESIGN_PACKS_ROOT


async def _plasticos_dir1_match_cypher() -> str:
    from unittest.mock import MagicMock

    from engine.config.loader import DomainPackLoader
    from engine.config.schema import TraversalSpec, TraversalStepSpec
    from engine.handlers import handle_match, init_dependencies

    # RG-047 needs a required multi-valued traversal step. The served plasticos
    # pack constrains polymers with EXISTS gates since RU-040 (CEG-COR-013), so
    # the row-multiplying step is added back to a copy of the pack here.
    spec = DomainPackLoader(config_path=str(_DOMAINS_DIR)).load_domain("plasticos")
    step = TraversalStepSpec(
        name="polymer_capability",
        pattern="(candidate:Facility)-[:PROCESSES]->(polymer:PolymerFamily)",
        required=True,
        matchdirections=["supply_opportunity_to_buyer_facility"],
    )
    spec = spec.model_copy(update={"traversal": TraversalSpec(steps=[step])})
    driver = _MatchCypherCapture()
    loader = MagicMock()
    loader.load_domain.return_value = spec
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    await handle_match(
        "plasticos",
        {"query": {"polymer_type": "PET"}, "match_direction": "supply_opportunity_to_buyer_facility"},
    )
    return driver.cyphers[0]


@pytest.mark.finding("CEG-COR-007")
class TestMatchCollapsesTraversalRows:
    """A Facility with k PROCESSES paths is one candidate, not k rows (RG-047)."""

    async def test_collapse_clause_precedes_order_and_limit(self) -> None:
        cypher = await _plasticos_dir1_match_cypher()
        # The required multi-valued step that multiplies rows:
        assert "MATCH (candidate:Facility)-[:PROCESSES]->(polymer:PolymerFamily)" in cypher
        collapse = "WITH candidate, head(collect({score: score, dimension_scores: dimension_scores})) AS best\n"
        assert collapse in cypher
        tail = "RETURN candidate, best.score AS score, best.dimension_scores AS dimension_scores\nORDER BY score DESC\nLIMIT $top_n"
        assert cypher.endswith(tail)
        # Rows are ordered by score before the per-candidate collect keeps the best.
        assert cypher.index("AS dimension_scores\nORDER BY score DESC\nWITH candidate, head(collect(") > 0
        assert cypher.index(collapse) < cypher.index("LIMIT $top_n")

    async def test_collapse_without_dimension_projection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "match_dimension_projection", False)
        cypher = await _plasticos_dir1_match_cypher()
        assert "WITH candidate, head(collect({score: score})) AS best\n" in cypher
        assert cypher.endswith("RETURN candidate, best.score AS score\nORDER BY score DESC\nLIMIT $top_n")

    async def test_flag_off_has_no_aggregation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "match_collapse_traversal_rows", False)
        cypher = await _plasticos_dir1_match_cypher()
        assert "collect(" not in cypher
        assert "best" not in cypher
        assert cypher.endswith(
            "AS score\nRETURN candidate, score, {geo_proximity: geo_proximity, credit_score: credit_score, "
            "contamination_tolerance_score: contamination_tolerance_score, community_match: community_match, "
            "rate_score: rate_score, price_alignment: price_alignment, recency: recency} AS dimension_scores\n"
            "ORDER BY score DESC\nLIMIT $top_n"
        )
