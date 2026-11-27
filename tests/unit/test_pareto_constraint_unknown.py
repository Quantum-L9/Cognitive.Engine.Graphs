"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, pareto, arbitration, constraints]
owner: engine-team
status: active
--- /L9_META ---

SCR-03 / SCR-04 (ceg-scoring-audit, operator rule Q2) on the Python constraint pass.

``apply_constraint_penalties`` still runs when ``match_arbitration_in_cypher_enabled``
is off and in the dormant SI-06 pre-cut. It used to skip a missing, None,
non-numeric or NaN value, so an unknown passed a hard constraint and escaped a
soft penalty; and ``score * penalty`` raised a negative score and crashed on a
None one. Under ``arbitration_unknown_fails_constraint_enabled`` an unknown value
fails the constraint, the penalised score is ``min(score, score * penalty)``, a
None score passes through, and ``stats["unknown_rejected"]`` counts the hard
rejections caused by an unknown value.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from engine.config.settings import settings
from engine.scoring.pareto_integrator import apply_constraint_penalties

pytestmark = pytest.mark.unit

HARD = {"dimension": "quality", "threshold": 0.5, "hard": True, "penalty": 0.5}
SOFT = {"dimension": "quality", "threshold": 0.5, "hard": False, "penalty": 0.5}

UNKNOWN: list[tuple[str, dict[str, Any]]] = [
    ("missing", {}),
    ("none", {"quality": None}),
    ("string", {"quality": "high"}),
    ("nan", {"quality": math.nan}),
    ("bool", {"quality": True}),
]


def _row(dims: dict[str, Any], score: float | None = 0.8) -> dict[str, Any]:
    return {"candidate": {"entity_id": "c"}, "score": score, "dimension_scores": dict(dims)}


def test_flag_ships_on() -> None:
    assert settings.arbitration_unknown_fails_constraint_enabled is True


@pytest.mark.parametrize(("label", "dims"), UNKNOWN, ids=[u[0] for u in UNKNOWN])
def test_unknown_value_fails_a_hard_constraint_and_is_counted(label: str, dims: dict[str, Any]) -> None:
    stats: dict[str, int] = {}
    kept = apply_constraint_penalties([_row(dims), _row({"quality": 0.9})], [HARD], stats=stats)
    assert [r["dimension_scores"].get("quality") for r in kept] == [0.9]
    assert stats == {"unknown_rejected": 1}


@pytest.mark.parametrize(("label", "dims"), UNKNOWN, ids=[u[0] for u in UNKNOWN])
def test_unknown_value_takes_the_soft_penalty(label: str, dims: dict[str, Any]) -> None:
    kept = apply_constraint_penalties([_row(dims)], [SOFT])
    assert kept[0]["score"] == pytest.approx(0.4)


def test_known_values_keep_their_meaning() -> None:
    stats: dict[str, int] = {}
    rows = [_row({"quality": 0.2}), _row({"quality": 0.5}), _row({"quality": 0})]
    kept = apply_constraint_penalties(rows, [HARD], stats=stats)
    assert [r["dimension_scores"]["quality"] for r in kept] == [0.5]
    assert stats == {"unknown_rejected": 0}  # 0.2 and 0 are known values below threshold


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.8, 0.4), (0.0, 0.0), (-0.4, -0.4), (None, None)],
    ids=["positive", "zero", "negative", "none"],
)
def test_soft_penalty_never_raises_a_score(score: float | None, expected: float | None) -> None:
    kept = apply_constraint_penalties([_row({"quality": 0.1}, score=score)], [SOFT])
    assert kept[0]["score"] == (expected if expected is None else pytest.approx(expected))


class TestRollback:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "arbitration_unknown_fails_constraint_enabled", False)

    def test_unknown_value_is_skipped(self) -> None:
        stats: dict[str, int] = {}
        kept = apply_constraint_penalties([_row({"quality": None}), _row({})], [HARD], stats=stats)
        assert len(kept) == 2
        assert stats == {}

    def test_penalty_multiplies(self) -> None:
        kept = apply_constraint_penalties([_row({"quality": 0.1}, score=-0.4)], [SOFT])
        assert kept[0]["score"] == pytest.approx(-0.2)


@pytest.mark.parametrize(
    ("label", "raw"),
    [("missing", None), ("nan", math.nan), ("string", "n/a"), ("bool", True), ("absent_key", ...)],
    ids=["missing", "nan", "string", "bool", "absent_key"],
)
def test_raw_source_unknown_fails_even_when_the_score_meets_the_threshold(label: str, raw: Any) -> None:
    """CEG-A07: the dimension score is 0.0 (defaulted / clamped) and meets a 0.0
    threshold; the raw candidate property decides knownness."""
    zero = {"dimension": "quality", "threshold": 0.0, "hard": True, "penalty": 0.5, "source_properties": ["q"]}
    stats: dict[str, int] = {}
    unknown = _row({"quality": 0.0})
    if raw is not ...:
        unknown["candidate"]["q"] = raw
    known_zero = _row({"quality": 0.0})
    known_zero["candidate"]["q"] = 0.0
    numeric_string = _row({"quality": 0.0})
    numeric_string["candidate"]["q"] = "0.0"  # toFloatOrNull parses it, so it is known
    kept = apply_constraint_penalties([unknown, known_zero, numeric_string], [zero], stats=stats)
    assert kept == [known_zero, numeric_string]
    assert stats == {"unknown_rejected": 1}
