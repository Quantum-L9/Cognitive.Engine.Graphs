"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, assembler, schema]
owner: engine-team
status: active
--- /L9_META ---

Scoring compiler defaults (CEG scoring audit L7).

* SCR-01 (rule Q1): a 0 pre-learning weight excludes the dimension; no
  learned-weight multiplier or penalty reaches it.
* SCR-07: ``absent_sources`` resolves the property each compiler reads.
* SCR-08 (rule Q3): weightedrate with a missing confidence takes the declared
  null strategy instead of a confidence of 1.0.
* SCR-09: divisors and scales are validated at load (strict validation) and
  read as declared (``is not None``); a lognormalized value <= -1 is unknown.

Compiled Cypher is asserted here (no Neo4j). The values are asserted on a real
graph in ``tests/integration/test_scoring_compiler_defaults_real_graph.py``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engine.config.schema import DomainSpec, ScoringDimensionSpec
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

_DOMAIN = "orbital-logistics"
_DIRECTION = "window_to_depot"
# The D3 strict-validation switch (#567): SCR-09 refusals follow it.
_STRICT = "scoring_spec_strict_validation_enabled"


def _set_strict(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(settings, _STRICT, value)


def _raw() -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    return copy.deepcopy(raw)


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


def _ports(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "port_capacity",
        "source": "candidateproperty",
        "candidateprop": "docking_ports",
        "computation": "lognormalized",
        "maxvalue": 1000.0,
        "weightkey": "w_ports",
        "defaultweight": 0.3,
        **overrides,
    }


def _spec(dims: list[dict[str, Any]] | None = None, *, feedback: bool = False) -> DomainSpec:
    raw = _raw()
    if dims is not None:
        raw["scoring"]["dimensions"] = dims
        depot = next(node for node in raw["ontology"]["nodes"] if node["label"] == "OrbitalDepot")
        for dim in dims:  # declare what each null option reads, as a real pack must
            if dim.get("null_strategy") == "inherit_prior":
                depot["properties"].append({"name": f"_prior_{dim['name']}", "type": "float"})
    if feedback:
        raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "enabled": True, "signal_weights": {"enabled": True}}
    return DomainSpec.model_validate(raw)


def _compiled(dim: dict[str, Any]) -> str:
    spec = _spec([dim])
    return ScoringAssembler(spec)._compile_dimension(spec.scoring.dimensions[0])


class _RowsDriver:
    """A fixed DimensionWeight read (unit tier: no Neo4j)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        return self.rows


@pytest.mark.parametrize(
    "flag",
    [
        "zero_weight_dimension_excluded_enabled",
        "absent_sources_compiler_resolution_enabled",
        "weightedrate_unknown_confidence_enabled",
        "scoring_explicit_numeric_defaults_enabled",
    ],
)
def test_flags_default_on(flag: str) -> None:
    assert type(settings).model_fields[flag].default is True


# ── SCR-01: a 0 pre-learning weight excludes the dimension ──────────────────


def test_zero_request_weight_excludes_the_dimension() -> None:
    assembler = ScoringAssembler(_spec([_dim(), _ports()]))
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.0})
    assert "port_capacity" not in clause
    assert assembler.last_active_dimension_names == ["depot_reliability"]
    assert assembler.last_effective_weights == {"depot_reliability": 0.6}


def test_zero_spec_default_weight_excludes_the_dimension() -> None:
    assembler = ScoringAssembler(_spec([_dim(), _ports(defaultweight=0.0)]))
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {})
    assert "port_capacity" not in clause
    assert assembler.last_active_dimension_names == ["depot_reliability"]


def test_a_request_override_reinstates_a_zero_default() -> None:
    assembler = ScoringAssembler(_spec([_dim(), _ports(defaultweight=0.0)]))
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.2})
    assert "(0.2 * port_capacity)" in clause


@pytest.mark.asyncio
async def test_a_learned_penalty_does_not_reach_an_excluded_dimension() -> None:
    """The learned 0.0 is below penalty_threshold 0.5: it would emit -0.3 for a 0-weighted dimension."""
    rows = [{"name": "port_capacity", "weight": 0.0}, {"name": "depot_reliability", "weight": 1.0}]
    assembler = ScoringAssembler(_spec([_dim(), _ports()], feedback=True), graph_driver=_RowsDriver(rows))  # type: ignore[arg-type]
    await assembler.load_learned_weights()
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.0})
    assert "port_capacity" not in assembler.last_effective_weights
    assert "-0.3" not in clause
    assert all(weight >= 0 for weight in assembler.last_effective_weights.values())


@pytest.mark.asyncio
async def test_flag_off_restores_the_penalised_zero_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "zero_weight_dimension_excluded_enabled", False)
    rows = [{"name": "port_capacity", "weight": 0.0}, {"name": "depot_reliability", "weight": 1.0}]
    assembler = ScoringAssembler(_spec([_dim(), _ports()], feedback=True), graph_driver=_RowsDriver(rows))  # type: ignore[arg-type]
    await assembler.load_learned_weights()
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.0})
    assert assembler.last_effective_weights["port_capacity"] == pytest.approx(-0.3)
    assert "(-0.3 * port_capacity)" in clause


def test_merged_weights_for_the_ceiling_still_name_every_dimension() -> None:
    """The match weight-ceiling check validates the request as supplied; exclusion is a scoring decision."""
    assembler = ScoringAssembler(_spec([_dim(), _ports()]))
    assert assembler.merged_request_weights(_DIRECTION, {"w_ports": 0.0}) == {
        "depot_reliability": 0.6,
        "port_capacity": 0.0,
    }


@pytest.mark.parametrize("weight", [False, True])
def test_a_bool_weight_is_not_a_zero_weight(weight: bool) -> None:
    assert ScoringAssembler._is_zero_weight(weight) is False


# ── SCR-07: absent_sources resolves what the compiler reads ─────────────────


def _absent(dims: list[dict[str, Any]]) -> str:
    assembler = ScoringAssembler(_spec(dims))
    assembler.assemble_scoring_clause(_DIRECTION, {})
    return assembler.absent_sources_expr()


def test_absent_sources_reports_a_defaulted_candidateprop() -> None:
    dims = [_dim(), _ports(candidateprop=None)]
    expr = _absent(dims)
    assert "CASE WHEN candidate.value IS NULL THEN $absent_src_port_capacity ELSE NULL END" in expr


def test_absent_sources_flag_off_reports_declared_candidateprops_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "absent_sources_compiler_resolution_enabled", False)
    expr = _absent([_dim(), _ports(candidateprop=None)])
    assert "port_capacity" not in expr
    assert "candidate.reliability IS NULL" in expr


def test_absent_sources_reports_weightedrate_confidence() -> None:
    rate = _dim(name="rate_quality", computation="weightedrate", candidateprop="reliability", queryprop="docking_ports")
    expr = _absent([rate])
    assert (
        "CASE WHEN candidate.reliability IS NULL OR candidate.docking_ports IS NULL "
        "THEN $absent_src_rate_quality ELSE NULL END"
    ) in expr


def test_absent_sources_reports_the_defaulted_weightedrate_properties() -> None:
    rate = _dim(name="rate_quality", computation="weightedrate", candidateprop=None)
    expr = _absent([rate])
    assert "candidate.rate IS NULL OR candidate.confidence IS NULL" in expr


def test_absent_sources_reports_both_geodecay_coordinates() -> None:
    geo = _dim(
        name="geo",
        source="computed",
        computation="geodecay",
        candidateprop="lat",
        candidateprop_lon="lon",
        queryprop="lat",
        queryprop_lon="lon",
    )
    assert "candidate.lat IS NULL OR candidate.lon IS NULL" in _absent([geo])


def test_absent_sources_skips_an_excluded_dimension() -> None:
    assembler = ScoringAssembler(_spec([_dim(), _ports()]))
    assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.0})
    assert "port_capacity" not in assembler.absent_sources_expr()


# ── SCR-08: weightedrate with an unknown confidence ──────────────────────────


def _rate(**overrides: Any) -> dict[str, Any]:
    return _dim(name="rate_quality", computation="weightedrate", candidateprop="reliability", **overrides)


def test_weightedrate_missing_confidence_scores_defaultwhennull_under_zero() -> None:
    assert _compiled(_rate()) == (
        "CASE WHEN candidate.confidence IS NULL THEN 0.1 "
        "ELSE coalesce(candidate.reliability, 0.1) * candidate.confidence END"
    )


def test_weightedrate_missing_confidence_takes_the_prior() -> None:
    expr = _compiled(_rate(null_strategy="inherit_prior"))
    assert expr.startswith(
        "CASE WHEN candidate.reliability IS NULL OR candidate.confidence IS NULL "
        "THEN coalesce(candidate._prior_rate_quality, 0.1) ELSE "
    )


def test_weightedrate_flag_off_restores_confidence_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "weightedrate_unknown_confidence_enabled", False)
    assert _compiled(_rate()) == "coalesce(candidate.reliability, 0.1) * coalesce(candidate.confidence, 1.0)"
    guard = _compiled(_rate(null_strategy="inherit_prior"))
    assert guard.startswith("CASE WHEN candidate.reliability IS NULL THEN ")


def test_communitymatch_documented_constants_are_unchanged() -> None:
    community = _dim(name="orbit", source="computed", computation="communitymatch", candidateprop="docking_ports")
    expr = _compiled(community)
    assert "IS NULL OR $docking_ports IS NULL THEN 0.5" in expr
    assert "ELSE 0.2" in expr


# ── SCR-09: divisors and scales ──────────────────────────────────────────────

REFUSED_SCALES = [
    pytest.param(_ports(maxvalue=0.0), "maxvalue must be > 0 for lognormalized", id="lognormalized-max-0"),
    pytest.param(_ports(maxvalue=-5.0), "maxvalue must be > 0 for lognormalized", id="lognormalized-max-neg"),
    pytest.param(
        _dim(computation="pricealignment", queryprop="min_ports", maxvalue=0.0),
        "maxvalue must be > 0 for pricealignment",
        id="pricealignment-tau-0",
    ),
    pytest.param(
        _dim(computation="temporalproximity", candidateprop="docked_at", maxvalue=-1.0),
        "maxvalue must be > 0 for temporalproximity",
        id="temporalproximity-decay-neg",
    ),
    pytest.param(
        _dim(name="geo", source="computed", computation="geodecay", candidateprop="lat", decayconstant=0.0),
        "decayconstant must be > 0 for geodecay",
        id="geodecay-k-0",
    ),
    pytest.param(
        _dim(computation="inverselinear", minvalue=10.0, maxvalue=10.0),
        "inverselinear needs minvalue < maxvalue",
        id="inverselinear-equal",
    ),
    pytest.param(
        _dim(computation="inverselinear", minvalue=150.0),
        "inverselinear needs minvalue < maxvalue, got minvalue=150.0, maxvalue=100.0",
        id="inverselinear-min-above-default-max",
    ),
]


@pytest.mark.parametrize(("dim", "message"), REFUSED_SCALES)
def test_strict_validation_refuses_an_unusable_scale(
    monkeypatch: pytest.MonkeyPatch, dim: dict[str, Any], message: str
) -> None:
    _set_strict(monkeypatch, True)
    with pytest.raises(ValidationError, match=message):
        ScoringDimensionSpec.model_validate(dim)


@pytest.mark.parametrize(("dim", "message"), REFUSED_SCALES)
def test_strict_validation_off_admits_the_scale(
    monkeypatch: pytest.MonkeyPatch, dim: dict[str, Any], message: str
) -> None:
    _set_strict(monkeypatch, False)
    assert ScoringDimensionSpec.model_validate(dim).name == dim["name"]


@pytest.mark.parametrize(
    "dim",
    [
        pytest.param(_ports(maxvalue=0.5), id="lognormalized-small-positive"),
        pytest.param(_ports(maxvalue=None), id="lognormalized-default"),
        pytest.param(_dim(computation="inverselinear", minvalue=-10.0, maxvalue=0.0), id="inverselinear-max-0"),
        pytest.param(_dim(computation="candidateproperty", maxvalue=0.0), id="maxvalue-unread"),
    ],
)
def test_strict_validation_admits_a_usable_scale(monkeypatch: pytest.MonkeyPatch, dim: dict[str, Any]) -> None:
    _set_strict(monkeypatch, True)
    ScoringDimensionSpec.model_validate(dim)


def test_inverselinear_reads_a_declared_zero_maxvalue() -> None:
    """[-10, 0]: the legacy `maxvalue or 100.0` replaced the declared 0 by 100."""
    expr = _compiled(_dim(computation="inverselinear", minvalue=-10.0, maxvalue=0.0))
    assert expr == "1.0 - (coalesce(candidate.reliability, 0.0) - -10.0) / (0.0 - -10.0)"


def test_explicit_defaults_flag_off_restores_the_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "scoring_explicit_numeric_defaults_enabled", False)
    expr = _compiled(_dim(computation="inverselinear", minvalue=-10.0, maxvalue=0.0))
    assert expr == "1.0 - (coalesce(candidate.reliability, 100.0) - -10.0) / (100.0 - -10.0)"


def test_undeclared_scales_keep_their_defaults() -> None:
    assert _compiled(_ports(maxvalue=None)) == "log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)"


def test_lognormalized_value_at_or_below_minus_one_takes_the_null_strategy() -> None:
    expr = _compiled(_ports(null_strategy="inherit_prior", defaultwhennull=0.1))
    assert expr.startswith(
        "CASE WHEN candidate.docking_ports IS NULL OR candidate.docking_ports <= -1 "
        "THEN coalesce(candidate._prior_port_capacity, 0.1) ELSE "
    )


def test_lognormalized_unknown_guard_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "scoring_explicit_numeric_defaults_enabled", False)
    expr = _compiled(_ports(null_strategy="inherit_prior", defaultwhennull=0.1))
    assert "<= -1" not in expr


def test_lognormalized_under_zero_is_unchanged_and_the_clamp_floors_the_undefined_log() -> None:
    """Under `zero` the expression is the legacy one; log(1 + x) for x <= -1 is -Infinity / NaN, which clamps to 0.0."""
    expr = _compiled(_ports())
    assert expr == "log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)"
    clamped = ScoringAssembler._clamp_expression(expr)
    assert f"WHEN isNaN(({expr})) THEN 0.0" in clamped
    assert f"WHEN ({expr}) < 0.0 THEN 0.0" in clamped


@pytest.mark.parametrize("root", [DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT], ids=["design", "synthetic"])
def test_every_committed_pack_still_loads_under_strict_scales(monkeypatch: pytest.MonkeyPatch, root: Any) -> None:
    _set_strict(monkeypatch, True)
    packs = sorted(root.glob("*/spec.yaml"))
    assert packs
    for path in packs:
        DomainSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ── SCR-07 robustness: dimensions the resolver does not know contribute nothing ──


def _double(computation: Any, **attrs: Any) -> Any:
    """A duck-typed dimension double (as tests/test_handlers.py builds specs from mocks)."""
    from unittest.mock import MagicMock

    dim = MagicMock()
    dim.name = "port_capacity"
    dim.computation = computation
    for key, value in attrs.items():
        setattr(dim, key, value)
    return dim


@pytest.mark.parametrize(
    "dim",
    [
        pytest.param(_double(object()), id="unknown-computation"),
        pytest.param(_double("lognormalized", candidateprop="docking_ports"), id="computation-not-enum"),
        pytest.param(_double(None), id="no-computation"),
    ],
)
def test_an_unknown_computation_contributes_no_absent_source(dim: Any) -> None:
    from engine.config.schema import ComputationType

    assert not isinstance(dim.computation, ComputationType)
    assembler = ScoringAssembler(_spec([_dim(), _ports()]))
    assert assembler._absent_source_case(dim) is None
    assert assembler._source_missing_predicate(dim) is None


@pytest.mark.parametrize(
    ("computation", "attrs"),
    [
        pytest.param("lognormalized", {"candidateprop": 42}, id="lognormalized-int-prop"),
        pytest.param("lognormalized", {"candidateprop": ["docking_ports"]}, id="lognormalized-list-prop"),
        pytest.param(
            "weightedrate", {"candidateprop": "reliability", "queryprop": 7}, id="weightedrate-int-confidence"
        ),
        pytest.param("kge", {"candidateprop": None, "alias": 3}, id="kge-int-alias"),
    ],
)
def test_a_non_string_property_contributes_no_absent_source(computation: str, attrs: dict[str, Any]) -> None:
    """Never passed to sanitize_label: the term is dropped, the match still assembles."""
    from engine.config.schema import ComputationType

    dim = _double(ComputationType(computation), **attrs)
    assembler = ScoringAssembler(_spec([_dim(), _ports()]))
    assert assembler._absent_source_case(dim) is None
    assert assembler._source_missing_predicate(dim, include_confidence=True) is None
    assert assembler._source_unknown_predicate(dim) is None


def test_strict_flag_off_really_disables_the_scale_refusals_on_a_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """CEG-A06: the declared #567 flag is the rollback; with it off the pack loads with the unusable scale."""
    dims = [_dim(), _ports(maxvalue=0.0)]
    _set_strict(monkeypatch, True)
    with pytest.raises(ValidationError, match="maxvalue must be > 0 for lognormalized"):
        _spec(dims)
    _set_strict(monkeypatch, False)
    assert _spec(dims).scoring.dimensions[1].maxvalue == 0.0
    assert type(settings).model_fields[_STRICT].default is True


# ── CEG-R02: a 0 weight never disables a declared arbitration constraint ─────


def _constrained(*, enabled: bool = True, hard: bool = True) -> DomainSpec:
    raw = _raw()
    raw["scoring"]["dimensions"] = [_dim(), _ports()]
    raw["decision_arbitration"] = {
        "enabled": enabled,
        "constraints": [{"dimension": "depot_reliability", "threshold": 0.6, "hard": hard}],
    }
    raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "enabled": True, "signal_weights": {"enabled": True}}
    return DomainSpec.model_validate(raw)


def test_a_constrained_zero_weight_dimension_is_projected_without_a_score_term() -> None:
    assembler = ScoringAssembler(_constrained())
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_reliability": 0.0})
    assert "AS depot_reliability" in clause
    assert assembler.last_active_dimension_names == ["depot_reliability", "port_capacity"]
    assert assembler.last_effective_weights == {"depot_reliability": 0.0, "port_capacity": 0.3}
    score_line = clause.splitlines()[-1]
    assert "* depot_reliability)" not in score_line
    assert "(0.3 * port_capacity)" in score_line
    assert "depot_reliability" in assembler.absent_sources_expr()


@pytest.mark.asyncio
async def test_a_constrained_zero_weight_dimension_takes_no_learned_penalty() -> None:
    rows = [{"name": "depot_reliability", "weight": 0.0}, {"name": "port_capacity", "weight": 1.0}]
    assembler = ScoringAssembler(_constrained(), graph_driver=_RowsDriver(rows))  # type: ignore[arg-type]
    await assembler.load_learned_weights()
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_reliability": 0.0})
    assert assembler.last_effective_weights["depot_reliability"] == 0.0
    assert "-0.3" not in clause


def test_a_disabled_arbitration_does_not_keep_the_zero_weight_dimension() -> None:
    assembler = ScoringAssembler(_constrained(enabled=False))
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_reliability": 0.0})
    assert "depot_reliability" not in clause


def test_an_unconstrained_zero_weight_dimension_is_still_excluded() -> None:
    assembler = ScoringAssembler(_constrained())
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_ports": 0.0})
    assert "port_capacity" not in clause
    assert assembler.last_active_dimension_names == ["depot_reliability"]
