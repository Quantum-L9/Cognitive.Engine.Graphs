"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, communitymatch]
owner: engine-team
status: active
--- /L9_META ---

SI-19 / SI-20: communitymatch scores community membership, not id distance.

``communitymatch_membership_scoring_enabled`` (default off). Off, the compiled
expression is the legacy one, byte for byte. On, ``soft_match`` no longer
grades by ``|candidate id - query id|`` and an undeclared ``bias`` is 1.0.
"""

from __future__ import annotations

import pytest

from engine.config.schema import ScoringDimensionSpec
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from tests.unit.test_hgkr_pass2 import _make_minimal_domain_spec

pytestmark = pytest.mark.unit

PROP = "cluster_ref"


def _dim(*, soft_match: bool, bias: float | None = None) -> ScoringDimensionSpec:
    return ScoringDimensionSpec(
        name="membership",
        source="candidateproperty",
        computation="communitymatch",
        candidateprop=PROP,
        queryprop=PROP,
        weightkey="w_membership",
        defaultweight=0.2,
        soft_match=soft_match,
        bias=bias,
    )


def _compile(dim: ScoringDimensionSpec) -> str:
    return ScoringAssembler(_make_minimal_domain_spec(dimensions=[dim]))._compile_communitymatch(dim)


def _legacy_binary(bias: str) -> str:
    return (
        f"CASE "
        f"  WHEN candidate.{PROP} IS NULL OR ${PROP} IS NULL THEN 0.5 "
        f"  WHEN candidate.{PROP} = ${PROP} THEN {bias} "
        f"  ELSE 0.2 "
        f"END"
    )


def _legacy_soft(bias: str) -> str:
    return (
        f"CASE "
        f"  WHEN candidate.{PROP} IS NULL OR ${PROP} IS NULL THEN 0.5 "
        f"  WHEN candidate.{PROP} = ${PROP} THEN {bias} "
        f"  ELSE toFloat({bias}) / (1.0 + abs(candidate.{PROP} - ${PROP})) "
        f"END"
    )


class TestFlagOffIsLegacy:
    def test_default_is_off(self) -> None:
        assert type(settings).model_fields["communitymatch_membership_scoring_enabled"].default is False

    @pytest.mark.parametrize(
        ("soft", "bias", "expected"),
        [
            (False, None, _legacy_binary("1.5")),
            (True, None, _legacy_soft("1.5")),
            (False, 0.8, _legacy_binary("0.8")),
            (True, 0.8, _legacy_soft("0.8")),
            # `bias or 1.5`: a declared 0.0 was replaced by 1.5.
            (False, 0.0, _legacy_binary("1.5")),
        ],
    )
    def test_byte_identical(self, monkeypatch, soft, bias, expected) -> None:
        monkeypatch.setattr(settings, "communitymatch_membership_scoring_enabled", False)
        assert _compile(_dim(soft_match=soft, bias=bias)) == expected


class TestFlagOnMembership:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "communitymatch_membership_scoring_enabled", True)

    def test_soft_match_has_no_id_arithmetic(self) -> None:
        expr = _compile(_dim(soft_match=True, bias=0.9))
        assert "abs(" not in expr
        assert " - " not in expr
        assert expr == _legacy_binary("0.9")

    def test_soft_and_binary_compile_identically(self) -> None:
        assert _compile(_dim(soft_match=True)) == _compile(_dim(soft_match=False))

    def test_undeclared_bias_is_one(self) -> None:
        assert _compile(_dim(soft_match=False)) == _legacy_binary("1.0")

    def test_declared_bias_kept(self) -> None:
        assert _compile(_dim(soft_match=False, bias=0.7)) == _legacy_binary("0.7")

    def test_declared_zero_bias_kept(self) -> None:
        assert _compile(_dim(soft_match=False, bias=0.0)) == _legacy_binary("0.0")
