"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, match, arbitration, scoring]
owner: engine-team
status: active
--- /L9_META ---

CEG-01 / CEG-02 (fourth external audit, 2026-11-27): constraint knownness comes
from the scoring compiler's typed source resolution
(``ScoringAssembler._source_missing_predicate`` / ``_source_property``), not
from coercing every raw property to a float.

* a temporal (``temporalproximity``) or categorical (``communitymatch``)
  source is known when it is non-null: no NaN or float test applies to it;
* a numeric source is unknown when null, NaN or not a number;
* a dimension that omits ``candidateprop`` is judged on the property the
  compiler resolves for it (``value`` for ``candidateproperty``), so a missing
  default source scored through its fallback is unknown.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest

from engine.config.schema import ArbitrationConstraintSpec, DecisionArbitrationSpec, ScoringDimensionSpec
from engine.config.settings import settings
from engine.handlers import _arbitration_constraint_cypher, _arbitration_constraint_dicts
from engine.scoring.pareto_integrator import apply_constraint_penalties
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

ORBITAL = "orbital-logistics"


def _dim(**kw: Any) -> ScoringDimensionSpec:
    base = {"source": "candidateproperty", "weightkey": f"w_{kw['name']}", "defaultweight": 0.1}
    return ScoringDimensionSpec(**(base | kw))


DIMS = {
    "recency": _dim(name="recency", computation="temporalproximity", candidateprop="last_seen"),
    "cohort": _dim(name="cohort", computation="communitymatch", candidateprop="community"),
    "fallback": _dim(name="fallback", computation="candidateproperty", defaultwhennull=0.8),
    "reliability": _dim(name="reliability", computation="candidateproperty", candidateprop="reliability"),
}


def _spec(dimension: str, threshold: float) -> Any:
    spec = synthetic_loader().load_domain(ORBITAL)
    scoring = spec.scoring.model_copy(update={"dimensions": [DIMS[dimension]]})
    arbitration = DecisionArbitrationSpec(
        enabled=True, constraints=[ArbitrationConstraintSpec(dimension=dimension, threshold=threshold, hard=True)]
    )
    return spec.model_copy(update={"scoring": scoring, "decision_arbitration": arbitration})


def _hard(dimension: str, threshold: float = 0.5) -> str:
    hard, _soft, _params = _arbitration_constraint_cypher(_spec(dimension, threshold), [dimension])
    return hard


def _row(dimension: str, score: float, **props: Any) -> dict[str, Any]:
    return {"candidate": dict(props), "score": score, "dimension_scores": {dimension: score}}


def _kept(dimension: str, threshold: float, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return apply_constraint_penalties(rows, _arbitration_constraint_dicts(_spec(dimension, threshold), [dimension]))


def test_unknown_fails_is_on() -> None:
    assert settings.arbitration_unknown_fails_constraint_enabled is True


class TestCypher:
    def test_temporal_source_is_tested_for_null_not_coerced(self) -> None:
        hard = _hard("recency")
        assert "candidate.last_seen IS NULL" in hard
        assert "toFloatOrNull(candidate.last_seen)" not in hard

    def test_categorical_source_is_tested_for_null_not_coerced(self) -> None:
        hard = _hard("cohort")
        assert "candidate.community IS NULL" in hard
        assert "toFloatOrNull(candidate.community)" not in hard

    def test_omitted_candidateprop_tests_the_resolved_default(self) -> None:
        hard = _hard("fallback", 0.7)
        assert "candidate.value IS NULL" in hard
        assert "isNaN(toFloatOrNull(candidate.value))" in hard

    def test_numeric_source_keeps_the_nan_test(self) -> None:
        hard = _hard("reliability", 0.0)
        assert "candidate.reliability IS NULL" in hard
        assert "isNaN(toFloatOrNull(candidate.reliability))" in hard


class TestPythonPass:
    def test_datetime_source_is_known(self) -> None:
        fresh = _row("recency", 1.0, last_seen=datetime.now(tz=UTC))
        missing = _row("recency", 1.0)
        assert _kept("recency", 0.5, [fresh, missing]) == [fresh]

    def test_string_community_is_known(self) -> None:
        member = _row("cohort", 1.0, community="c-7")
        missing = _row("cohort", 1.0)
        assert _kept("cohort", 0.9, [member, missing]) == [member]

    def test_missing_default_source_fails_despite_fallback_score(self) -> None:
        defaulted = _row("fallback", 0.8)  # no `value`: the compiler scored defaultwhennull
        stored = _row("fallback", 0.8, value=0.8)
        assert _kept("fallback", 0.7, [defaulted, stored]) == [stored]

    def test_numeric_zero_passes_and_nan_fails(self) -> None:
        zero = _row("reliability", 0.0, reliability=0.0)
        nan = _row("reliability", 0.0, reliability=math.nan)
        assert _kept("reliability", 0.0, [zero, nan]) == [zero]
