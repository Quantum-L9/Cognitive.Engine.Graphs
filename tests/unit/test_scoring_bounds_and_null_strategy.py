"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, assembler, security]
owner: engine-team
status: active
--- /L9_META ---

Scoring bounds, learned-weight hygiene and effective null strategies (CEG scoring audit L2).

* D5: under ``score_clamp_enabled`` the composite ``score`` is clamped to
  [0, 1] like each dimension; a learned-weight penalty keeps its subtractive
  meaning inside the range. Learned weights that are not finite numbers are
  ignored on read with a logged reason.
* D3a: ``null_strategy`` ``inherit_prior`` / ``population_mean`` take effect
  for a missing source property; a present property is unaffected.

The compiled Cypher is asserted here (no Neo4j); the values are asserted on a
real graph in ``tests/integration/test_scoring_bounds_real_graph.py``.
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Any

import pytest
import yaml

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

_DOMAIN = "orbital-logistics"
_DIRECTION = "window_to_depot"


def _raw() -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    return copy.deepcopy(raw)


def _declare_producers(raw: dict[str, Any], dims: list[dict[str, Any]]) -> None:
    """Declare what each dimension's null option reads, as a real pack must.

    population_mean reads the `_popmean_<dim>` query parameter and inherit_prior
    the `_prior_<dim>` candidate property; strict scoring validation refuses an
    option whose input the pack does not declare (ND-PARAM, #567).
    """
    depot = next(node for node in raw["ontology"]["nodes"] if node["label"] == "OrbitalDepot")
    for dim in dims:
        if dim.get("null_strategy") == "population_mean":
            raw["queryschema"]["fields"].append({"name": f"_popmean_{dim['name']}", "type": "float"})
        if dim.get("null_strategy") == "inherit_prior":
            depot["properties"].append({"name": f"_prior_{dim['name']}", "type": "float"})


def _spec(*, feedback: bool = False, dims: list[dict[str, Any]] | None = None) -> DomainSpec:
    raw = _raw()
    if dims is not None:
        raw["scoring"]["dimensions"] = dims
        _declare_producers(raw, dims)
    if feedback:
        raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "enabled": True, "signal_weights": {"enabled": True}}
    return DomainSpec.model_validate(raw)


class _RowsDriver:
    """A fixed DimensionWeight read (unit tier: no Neo4j)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        return self.rows


# ── D5: composite clamp ──────────────────────────────────────────────────────


def _score_line(clause: str) -> str:
    return clause.splitlines()[-1]


def test_composite_score_is_clamped_under_score_clamp() -> None:
    assembler = ScoringAssembler(_spec())
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {})
    weighted = "(0.6 * depot_reliability) + (0.3 * port_capacity)"
    assert _score_line(clause) == (
        "WITH candidate, depot_reliability, port_capacity, depot_reliability__known, port_capacity__known, "
        f"{ScoringAssembler._clamp_expression(weighted)} AS score"
    )


def test_composite_is_unclamped_with_score_clamp_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_clamp_enabled", False)
    clause, _ = ScoringAssembler(_spec()).assemble_scoring_clause(_DIRECTION, {})
    assert _score_line(clause) == (
        "WITH candidate, depot_reliability, port_capacity, depot_reliability__known, port_capacity__known, "
        "(0.6 * depot_reliability) + (0.3 * port_capacity) AS score"
    )


def test_clamp_bounds_null_nan_and_both_ends() -> None:
    expr = ScoringAssembler._clamp_expression("s")
    assert expr == (
        "CASE WHEN (s) IS NULL THEN 0.0 WHEN isNaN((s)) THEN 0.0 WHEN (s) < 0.0 THEN 0.0 WHEN (s) > 1.0 THEN 1.0 ELSE (s) END"
    )


async def _assembled_with_learned(rows: list[dict[str, Any]]) -> tuple[ScoringAssembler, str]:
    assembler = ScoringAssembler(_spec(feedback=True), graph_driver=_RowsDriver(rows))  # type: ignore[arg-type]
    await assembler.load_learned_weights()
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {})
    return assembler, clause


@pytest.mark.asyncio
async def test_normal_positive_learned_weight_multiplies_and_renormalises() -> None:
    assembler, clause = await _assembled_with_learned([{"name": "depot_reliability", "weight": 2.0}])
    weights = assembler.last_effective_weights
    assert all(math.isfinite(w) and w >= 0 for w in weights.values())
    # RU-023: renormalised back to the pre-learning positive sum (0.9).
    assert sum(weights.values()) == pytest.approx(0.9)
    assert weights["depot_reliability"] > weights["port_capacity"]
    assert "CASE WHEN ((" in _score_line(clause)


@pytest.mark.asyncio
@pytest.mark.parametrize("learned", [0.0, -1.5])
async def test_zero_and_negative_learned_weight_penalise_inside_the_clamped_range(learned: float) -> None:
    assembler, clause = await _assembled_with_learned([{"name": "port_capacity", "weight": learned}])
    weights = assembler.last_effective_weights
    assert weights["port_capacity"] == pytest.approx(-0.3)  # -abs(penalty_factor): subtracts
    weighted = "(0.6 * depot_reliability) + (-0.3 * port_capacity)"
    assert _score_line(clause).endswith(f"{ScoringAssembler._clamp_expression(weighted)} AS score")


@pytest.mark.asyncio
@pytest.mark.parametrize("learned", [math.nan, math.inf, -math.inf, "0.9", None, True])
async def test_non_finite_or_non_numeric_learned_weight_is_ignored_with_a_reason(
    learned: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="engine.scoring.assembler")
    assembler, clause = await _assembled_with_learned(
        [{"name": "depot_reliability", "weight": learned}, {"name": "port_capacity", "weight": 1.0}]
    )
    loaded = assembler._learned_weights or {}
    assert "depot_reliability" not in loaded
    assert loaded.get("port_capacity") == 1.0
    assert assembler.last_effective_weights["depot_reliability"] == pytest.approx(0.6)
    assert "nan" not in clause.lower().replace("isnan", "")
    assert "inf" not in clause.lower()
    if learned is not None:  # null is absence, not a defect (RU-023 reader)
        assert any("Ignoring learned weight for dimension depot_reliability" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_finite_read_flag_off_restores_the_legacy_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback: with the filter off a non-finite learned weight is loaded as before."""
    monkeypatch.setattr(settings, "learned_weight_finite_read_enabled", False)
    assembler = ScoringAssembler(
        _spec(feedback=True), graph_driver=_RowsDriver([{"name": "port_capacity", "weight": "x"}])
    )  # type: ignore[arg-type]
    loaded = await assembler.load_learned_weights()
    assert loaded == {"port_capacity": "x"}


def test_finite_read_flag_defaults_on() -> None:
    assert type(settings).model_fields["learned_weight_finite_read_enabled"].default is True


# ── D3a: effective null strategies ───────────────────────────────────────────


def _dim(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "depot_reliability",
        "source": "candidateproperty",
        "candidateprop": "reliability",
        "computation": "candidateproperty",
        "weightkey": "w_reliability",
        "defaultweight": 0.6,
        "defaultwhennull": 0.1,
        **overrides,
    }


def _compiled(dim: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    spec = _spec(dims=[dim])
    assembler = ScoringAssembler(spec)
    return assembler._compile_dimension(spec.scoring.dimensions[0]), assembler.last_query_params


def test_zero_strategy_is_unchanged() -> None:
    expr, _ = _compiled(_dim())
    assert expr == "coalesce(candidate.reliability, 0.1)"


def test_inherit_prior_takes_the_prior_when_the_source_is_missing() -> None:
    expr, _ = _compiled(_dim(null_strategy="inherit_prior"))
    assert expr == (
        "CASE WHEN candidate.reliability IS NULL THEN coalesce(candidate._prior_depot_reliability, 0.1) "
        "ELSE coalesce((coalesce(candidate.reliability, 0.1)), coalesce(candidate._prior_depot_reliability, 0.1)) END"
    )


def test_population_mean_takes_the_parameter_when_the_source_is_missing() -> None:
    expr, _ = _compiled(_dim(null_strategy="population_mean"))
    assert expr == (
        "CASE WHEN candidate.reliability IS NULL THEN coalesce($_popmean_depot_reliability, 0.1) "
        "ELSE coalesce((coalesce(candidate.reliability, 0.1)), coalesce($_popmean_depot_reliability, 0.1)) END"
    )


@pytest.mark.parametrize("strategy", ["inherit_prior", "population_mean"])
def test_a_present_source_keeps_the_computation_value(strategy: str) -> None:
    """The ELSE branch is the zero-strategy expression, byte for byte, coalesced only against a null result."""
    zero, _ = _compiled(_dim())
    expr, _ = _compiled(_dim(null_strategy=strategy))
    assert f"ELSE coalesce(({zero}), coalesce(" in expr


def test_a_null_computation_with_its_source_present_still_takes_the_fallback() -> None:
    """geodecay with a null query operand is null although lat/lon are stored: the fallback still applies."""
    expr, _ = _compiled(
        _dim(
            name="geo",
            source="computed",
            computation="geodecay",
            candidateprop="lat",
            candidateprop_lon="lon",
            queryprop="lat",
            queryprop_lon="lon",
            null_strategy="inherit_prior",
        )
    )
    else_branch = expr.split(" ELSE ", 1)[1]
    assert else_branch.startswith("coalesce((1.0 / (1.0 + point.distance(")
    assert else_branch.endswith(", coalesce(candidate._prior_geo, 0.1)) END")


@pytest.mark.parametrize("strategy", ["inherit_prior", "population_mean"])
def test_flag_off_restores_the_legacy_dead_wrapper(monkeypatch: pytest.MonkeyPatch, strategy: str) -> None:
    monkeypatch.setattr(settings, "null_strategy_effective_enabled", False)
    expr, _ = _compiled(_dim(null_strategy=strategy))
    fallback = "candidate._prior_depot_reliability" if strategy == "inherit_prior" else "$_popmean_depot_reliability"
    assert expr == f"coalesce((coalesce(candidate.reliability, 0.1)), {fallback}, 0.1)"


def test_the_legacy_wrapper_was_dead() -> None:
    """Why D3a: the inner coalesce is never null, so the outer fallback never applied."""
    zero, _ = _compiled(_dim())
    assert zero.startswith("coalesce(candidate.reliability, ")


def test_lognormalized_guard_reads_its_declared_property() -> None:
    expr, _ = _compiled(
        _dim(
            name="port_capacity",
            candidateprop="docking_ports",
            computation="lognormalized",
            null_strategy="inherit_prior",
        )
    )
    # SCR-09 (L7): a value <= -1 is unknown to log(1 + x), tested beside the null.
    assert expr.startswith(
        "CASE WHEN candidate.docking_ports IS NULL OR candidate.docking_ports <= -1 "
        "THEN coalesce(candidate._prior_port_capacity, 0.1)"
    )


def test_geodecay_guard_checks_both_coordinates() -> None:
    expr, _ = _compiled(
        _dim(
            name="geo",
            source="computed",
            computation="geodecay",
            candidateprop="lat",
            candidateprop_lon="lon",
            queryprop="lat",
            queryprop_lon="lon",
            null_strategy="population_mean",
        )
    )
    assert expr.startswith("CASE WHEN candidate.lat IS NULL OR candidate.lon IS NULL THEN coalesce($_popmean_geo, 0.1)")


def test_geodecay_guard_follows_the_declared_longitude_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off, _compile_geodecay reads candidate.lon whatever is declared; the guard tests the same property."""
    monkeypatch.setattr(settings, "geodecay_declared_longitude", False)
    expr, _ = _compiled(
        _dim(
            name="geo",
            source="computed",
            computation="geodecay",
            candidateprop="lat",
            candidateprop_lon="longitude_deg",
            queryprop="lat",
            null_strategy="population_mean",
        )
    )
    assert expr.startswith("CASE WHEN candidate.lat IS NULL OR candidate.lon IS NULL THEN ")
    assert "candidate.longitude_deg IS NULL" not in expr


def test_geodecay_guard_treats_a_blank_declared_longitude_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace candidateprop_lon resolves like the compiler: absent, so (rollback flag off) candidate.lon."""
    monkeypatch.setattr(settings, "geodecay_declared_longitude", True)
    monkeypatch.setattr(settings, "declared_structural_properties_required_enabled", False)
    expr, _ = _compiled(
        _dim(
            name="geo",
            source="computed",
            computation="geodecay",
            candidateprop="lat",
            candidateprop_lon="   ",
            queryprop="lat",
            null_strategy="population_mean",
        )
    )
    assert expr.startswith("CASE WHEN candidate.lat IS NULL OR candidate.lon IS NULL THEN ")
    assert "longitude: candidate.lon}" in expr


@pytest.mark.parametrize(
    ("computation", "extra", "legacy_prop"),
    [
        ("communitymatch", {"source": "computed"}, "community_id"),
        ("geodecay", {"source": "computed", "candidateprop_lon": "lon"}, "lat"),
    ],
)
def test_omitted_structural_property_guards_the_legacy_default_with_the_rollback_flag(
    monkeypatch: pytest.MonkeyPatch, computation: str, extra: dict[str, Any], legacy_prop: str
) -> None:
    """declared_structural_properties_required_enabled off: the compiler reads the former default; so does the guard."""
    monkeypatch.setattr(settings, "declared_structural_properties_required_enabled", False)
    dim = _dim(name="structural", computation=computation, null_strategy="inherit_prior", **extra)
    dim.pop("candidateprop")
    expr, _ = _compiled(dim)
    assert expr.startswith(f"CASE WHEN candidate.{legacy_prop} IS NULL")
    assert f"candidate.{legacy_prop}" in expr.split(" ELSE ", 1)[1]


def test_communitymatch_guard_reads_its_declared_property() -> None:
    expr, _ = _compiled(
        _dim(
            name="community",
            source="computed",
            computation="communitymatch",
            candidateprop="cluster",
            queryprop="cluster",
            null_strategy="population_mean",
        )
    )
    assert expr.startswith("CASE WHEN candidate.cluster IS NULL THEN coalesce($_popmean_community, 0.1)")


def test_traversalalias_guard_reads_the_alias() -> None:
    expr, _ = _compiled(
        _dim(
            name="via",
            source="traversalalias",
            computation="traversalalias",
            alias="orbit",
            candidateprop="tier",
            null_strategy="inherit_prior",
        )
    )
    assert expr.startswith("CASE WHEN orbit.tier IS NULL THEN coalesce(candidate._prior_via, 0.1)")


def test_customcypher_keeps_the_legacy_wrapper() -> None:
    expr, _ = _compiled(
        _dim(
            name="custom",
            source="computed",
            computation="customcypher",
            expression="candidate.reliability * 0.5",
            null_strategy="inherit_prior",
        )
    )
    assert expr == "coalesce((candidate.reliability * 0.5), candidate._prior_custom, 0.1)"


def test_null_strategy_flag_defaults_on() -> None:
    assert type(settings).model_fields["null_strategy_effective_enabled"].default is True
