"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, assembler]
owner: engine-team
status: active
--- /L9_META ---

Per-dimension knownness projection (CEG-13, ``dimension_knownness_projection_enabled``).

The first scoring WITH projects ``<dim>__known`` beside each active and
observed-only dimension, from the compiler's own source resolution, while the
operands (traversal aliases included) are still in scope. ``handle_match``
carries the map through the per-candidate collapse as ``dimension_known``.
Score values are untouched. The values on a real graph are asserted in
``tests/integration/test_dimension_knownness_real_graph.py``.
"""

from __future__ import annotations

import copy
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
_FLAG = "dimension_knownness_projection_enabled"


def _numeric(operand: str) -> str:
    return f"({operand} IS NOT NULL AND coalesce(NOT isNaN(toFloatOrNull({operand})), false))"


def _raw() -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    return copy.deepcopy(raw)


def _reliability(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "depot_reliability",
        "source": "candidateproperty",
        "candidateprop": "reliability",
        "computation": "candidateproperty",
        "weightkey": "w_reliability",
        "defaultweight": 0.6,
        **overrides,
    }


def _extra(**fields: Any) -> dict[str, Any]:
    return {"name": "extra", "source": "candidateproperty", "weightkey": "w_extra", "defaultweight": 0.3, **fields}


def _assembler(dims: list[dict[str, Any]], arbitration: dict[str, Any] | None = None) -> ScoringAssembler:
    raw = _raw()
    raw["scoring"]["dimensions"] = dims
    if arbitration is not None:
        raw["decision_arbitration"] = arbitration
    return ScoringAssembler(DomainSpec.model_validate(raw))


def _first_with(assembler: ScoringAssembler, weights: dict[str, float] | None = None) -> tuple[str, str]:
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, weights or {})
    first, second = clause.splitlines()
    return first, second


def test_flag_defaults_on() -> None:
    assert type(settings).model_fields[_FLAG].default is True


def test_numeric_source_is_known_when_not_null_and_not_nan() -> None:
    first, second = _first_with(_assembler([_reliability()]))
    assert first.endswith(f", {_numeric('candidate.reliability')} AS depot_reliability__known")
    assert second.startswith("WITH candidate, depot_reliability, depot_reliability__known, ")


def test_defaulted_candidateprop_is_resolved_like_the_compiler() -> None:
    """A lognormalized dimension with no candidateprop reads ``value``; so does its knownness."""
    dim = _extra(computation="lognormalized", maxvalue=1000.0)
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert f"{_numeric('candidate.value')} AS extra__known" in first


def test_temporal_source_is_known_when_not_null() -> None:
    dim = _extra(computation="temporalproximity", candidateprop="docked_at")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert "(candidate.docked_at IS NOT NULL) AS extra__known" in first


def test_categorical_source_is_known_when_not_null() -> None:
    dim = _extra(computation="communitymatch", candidateprop="community_id")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert "(candidate.community_id IS NOT NULL) AS extra__known" in first


def test_alias_sourced_dimension_reads_the_traversal_alias() -> None:
    dim = _extra(source="traversalalias", computation="traversalalias", alias="grade", candidateprop="purity")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert f"{_numeric('grade.purity')} AS extra__known" in first


def test_alias_sourced_dimension_defaults_to_the_score_property() -> None:
    dim = _extra(source="traversalalias", computation="traversalalias", alias="grade")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert f"{_numeric('grade.score')} AS extra__known" in first


def test_geodecay_also_requires_the_longitude() -> None:
    dim = _extra(computation="geodecay", candidateprop="lat", candidateprop_lon="lon")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert f"{_numeric('candidate.lat')} AND {_numeric('candidate.lon')} AS extra__known" in first


def test_weightedrate_also_requires_the_confidence() -> None:
    dim = _extra(computation="weightedrate", candidateprop="reliability")
    first, _ = _first_with(_assembler([_reliability(), dim]))
    assert f"{_numeric('candidate.reliability')} AND {_numeric('candidate.confidence')} AS extra__known" in first


def test_observed_only_zero_weight_dimension_projects_its_knownness() -> None:
    """CEG-R02: a 0-weight dimension an arbitration constraint names is observed; L3 reads its knownness."""
    arbitration = {"enabled": True, "constraints": [{"dimension": "depot_reliability", "threshold": 0.6}]}
    assembler = _assembler([_reliability(), _extra(computation="lognormalized")], arbitration)
    first, second = _first_with(assembler, {"w_reliability": 0.0})
    assert "AS depot_reliability__known" in first
    assert "depot_reliability__known" in second
    assert "depot_reliability" in (assembler.dimension_known_projection() or "")


def test_excluded_dimension_projects_no_knownness() -> None:
    assembler = _assembler([_reliability(), _extra(computation="lognormalized")])
    first, _ = _first_with(assembler, {"w_extra": 0.0})
    assert "extra__known" not in first
    assert assembler.dimension_known_projection() == "{depot_reliability: depot_reliability__known}"


def test_known_projection_names_every_projected_dimension() -> None:
    assembler = _assembler([_reliability(), _extra(computation="lognormalized")])
    _first_with(assembler)
    assert assembler.dimension_known_projection() == (
        "{depot_reliability: depot_reliability__known, extra: extra__known}"
    )


def test_known_aliases_are_exposed_for_a_re_projecting_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clause after the scoring WITH (an arbitration WITH) passes these columns through."""
    assembler = _assembler([_reliability(), _extra(computation="lognormalized")])
    _first_with(assembler)
    assert assembler.last_dimension_known_aliases == {
        "depot_reliability": "depot_reliability__known",
        "extra": "extra__known",
    }
    monkeypatch.setattr(settings, _FLAG, False)
    _first_with(assembler)
    assert assembler.last_dimension_known_aliases == {}


def test_knownness_does_not_enter_the_score() -> None:
    assembler = _assembler([_reliability(), _extra(computation="lognormalized")])
    _, second = _first_with(assembler)
    score_expr = second.split("extra__known, ", 1)[1]
    assert "__known" not in score_expr


def test_flag_off_is_byte_identical_to_the_legacy_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    dims = [_reliability(), _extra(source="traversalalias", computation="traversalalias", alias="grade")]
    on = _assembler(dims)
    on_clause, _ = on.assemble_scoring_clause(_DIRECTION, {})
    monkeypatch.setattr(settings, _FLAG, False)
    off = _assembler(dims)
    off_clause, _ = off.assemble_scoring_clause(_DIRECTION, {})
    assert "__known" not in off_clause
    assert off.dimension_known_projection() is None
    first_on, second_on = on_clause.splitlines()
    legacy_first = first_on.split(f", {_numeric('candidate.reliability')} AS depot_reliability__known")[0]
    legacy_second = second_on.replace(", depot_reliability__known, extra__known", "")
    assert off_clause == f"{legacy_first}\n{legacy_second}"


# ── handle_match carries the map through the per-candidate collapse ─────────


class _RecordingDriver:
    """Fake GraphDriver (unit tier): records the match Cypher, returns no rows."""

    def __init__(self) -> None:
        self.cyphers: list[str] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.cyphers.append(cypher)
        return []


async def _match_cypher() -> str:
    from unittest.mock import MagicMock

    from engine.handlers import handle_match, init_dependencies
    from tests.fixtures.domain_contracts import synthetic_loader

    driver = _RecordingDriver()
    loader = MagicMock()
    loader.load_domain.return_value = synthetic_loader().load_domain(_DOMAIN)
    loader.list_domains.return_value = [_DOMAIN]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    await handle_match(_DOMAIN, {"query": {}, "match_direction": _DIRECTION})
    return driver.cyphers[0]


async def test_handle_match_carries_dimension_known_through_the_collapse() -> None:
    cypher = await _match_cypher()
    known = "{depot_reliability: depot_reliability__known, port_capacity: port_capacity__known} AS dimension_known"
    assert known in cypher
    assert "absent_sources: absent_sources, dimension_known: dimension_known}" in cypher
    assert "best.absent_sources AS absent_sources, best.dimension_known AS dimension_known\n" in cypher


async def test_handle_match_flag_off_has_no_dimension_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, _FLAG, False)
    cypher = await _match_cypher()
    assert "__known" not in cypher
    assert "dimension_known" not in cypher
    assert "best.absent_sources AS absent_sources\nORDER BY score DESC" in cypher
