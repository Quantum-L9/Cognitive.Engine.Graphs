"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, domain-spec]
owner: engine-team
status: active
--- /L9_META ---

temporalproximity reads only the auxiliary signals a dimension declares (CEG-FEEDBACK-1, FEATURE_GATES §37).

The legacy compiler added ``0.25 * candidate.touch_count_30d / 30`` and
``0.15 * candidate.is_accelerating`` to ``0.6 * recency``, naming properties
no pack declares, so a pack's recency score was capped at 0.6. With
``temporalproximity_declared_signals_enabled`` on, the signals come from
``metadata`` (frequency_property / acceleration_property and their weights),
recency takes the remaining weight, and an undeclared signal contributes
nothing. Off keeps the legacy text byte for byte. Built on the synthetic
``orbital-logistics`` pack (``tests/fixtures/domains``).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

_DOMAIN = "orbital-logistics"
_DIRECTION = "window_to_depot"
_DIM = "depot_freshness"

_LEGACY = (
    "CASE WHEN candidate.last_seen IS NULL THEN 0.0 ELSE ("
    "  0.6 * exp(-1.0 * duration.inDays(candidate.last_seen, datetime()).days / 45.0)"
    "  + 0.25 * toFloat(coalesce(candidate.touch_count_30d, 0)) / 30.0"
    "  + 0.15 * toFloat(coalesce(candidate.is_accelerating, 0))"
    ") END"
)


def _dimension(metadata: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    dim: dict[str, Any] = {
        "name": _DIM,
        "source": "candidateproperty",
        "computation": "temporalproximity",
        "candidateprop": "last_seen",
        "maxvalue": 45.0,
        "weightkey": "w_freshness",
        "defaultweight": 0.05,
        "matchdirections": [_DIRECTION],
        **overrides,
    }
    if metadata is not None:
        dim["metadata"] = metadata
    return dim


def _spec(dimension: dict[str, Any]) -> DomainSpec:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["scoring"]["dimensions"].append(dimension)
    return DomainSpec.model_validate(raw)


def _compile(dimension: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    spec = _spec(dimension)
    assembler = ScoringAssembler(spec)
    dim = next(d for d in spec.scoring.dimensions if d.name == _DIM)
    expr = assembler._compile_dimension(dim)
    return expr, assembler.last_query_params


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "temporalproximity_declared_signals_enabled", True)


# ── Flag off: the legacy compiler, unchanged ────────────────────────────────


def test_flag_defaults_off() -> None:
    assert type(settings).model_fields["temporalproximity_declared_signals_enabled"].default is False


def test_flag_off_compiles_the_legacy_text_and_ignores_metadata() -> None:
    expr, params = _compile(_dimension({"frequency_property": "dock_count"}))
    assert expr == _LEGACY
    assert not any(key.startswith("tp_") for key in params)


# ── Flag on: declared signals only ───────────────────────────────────────────


@pytest.mark.usefixtures("declared")
def test_undeclared_signals_contribute_nothing_and_recency_takes_full_weight() -> None:
    expr, params = _compile(_dimension())
    assert "touch_count_30d" not in expr
    assert "is_accelerating" not in expr
    assert expr == (
        "CASE WHEN candidate.last_seen IS NULL THEN 0.0 ELSE ("
        "$tp_recency_w_depot_freshness * exp(-1.0 * CASE WHEN "
        "duration.inDays(candidate.last_seen, datetime()).days < 0 THEN 0 ELSE "
        "duration.inDays(candidate.last_seen, datetime()).days END / 45.0)"
        ") END"
    )
    assert params == {"tp_recency_w_depot_freshness": 1.0}


# ── D2 (operator decision 2026-11-26): the declared path floors a negative age ─

_AGE = "duration.inDays(candidate.last_seen, datetime()).days"


@pytest.mark.usefixtures("declared")
def test_declared_recency_floors_a_future_date_at_age_zero() -> None:
    """A future date's negative age is floored at 0, so its decay is exp(0) = 1.0 — the fresh score, never more."""
    expr, _params = _compile(_dimension())
    assert f"exp(-1.0 * CASE WHEN {_AGE} < 0 THEN 0 ELSE {_AGE} END / 45.0)" in expr
    # No unfloored age reaches exp(): every age occurrence sits inside the floor.
    assert expr.count(_AGE) == 2
    assert f"exp(-1.0 * {_AGE}" not in expr


@pytest.mark.usefixtures("declared")
def test_declared_floor_applies_with_auxiliary_signals_declared() -> None:
    expr, _params = _compile(_dimension({"frequency_property": "dock_count"}))
    assert f"CASE WHEN {_AGE} < 0 THEN 0 ELSE {_AGE} END" in expr


def test_legacy_path_has_no_floor_and_matches_main_byte_for_byte() -> None:
    """D2: the legacy path stays byte-for-byte (FEATURE_GATES §37); ``_LEGACY`` is main's text at 21dc906."""
    expr, _params = _compile(_dimension())
    assert expr == _LEGACY
    assert "< 0 THEN 0" not in expr


@pytest.mark.usefixtures("declared")
def test_declared_signals_bind_their_weights_as_parameters() -> None:
    expr, params = _compile(
        _dimension(
            {
                "frequency_property": "dock_count",
                "frequency_weight": 0.3,
                "frequency_scale": 12.0,
                "acceleration_property": "dock_rate_rising",
                "acceleration_weight": 0.1,
            }
        )
    )
    assert params == {
        "tp_freq_w_depot_freshness": 0.3,
        "tp_freq_scale_depot_freshness": 12.0,
        "tp_accel_w_depot_freshness": 0.1,
        "tp_recency_w_depot_freshness": pytest.approx(0.6),
    }
    assert "candidate.dock_count" in expr
    assert "candidate.dock_rate_rising" in expr
    # No weight or scale is interpolated as a literal: only parameters carry them.
    for literal in ("0.3", "12.0", "0.1", "0.6"):
        assert literal not in expr
    assert "$tp_freq_w_depot_freshness * CASE WHEN" in expr
    assert "/ $tp_freq_scale_depot_freshness" in expr
    assert "$tp_accel_w_depot_freshness * CASE WHEN candidate.dock_rate_rising = true" in expr


@pytest.mark.usefixtures("declared")
def test_declared_signal_without_weight_uses_the_legacy_weight() -> None:
    _, params = _compile(_dimension({"acceleration_property": "dock_rate_rising"}))
    assert params == {"tp_accel_w_depot_freshness": 0.15, "tp_recency_w_depot_freshness": pytest.approx(0.85)}


@pytest.mark.usefixtures("declared")
def test_the_frequency_term_is_clamped_to_unit_range() -> None:
    expr, _ = _compile(_dimension({"frequency_property": "dock_count"}))
    freq = "toFloat(coalesce(candidate.dock_count, 0)) / $tp_freq_scale_depot_freshness"
    assert f"CASE WHEN {freq} > 1.0 THEN 1.0 WHEN {freq} < 0.0 THEN 0.0 ELSE {freq} END" in expr


@pytest.mark.usefixtures("declared")
def test_the_compiled_clause_is_assembled_with_its_parameters() -> None:
    spec = _spec(_dimension({"frequency_property": "dock_count"}))
    assembler = ScoringAssembler(spec)
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {})
    assert "$tp_recency_w_depot_freshness" in clause
    assert assembler.last_query_params["tp_recency_w_depot_freshness"] == pytest.approx(0.75)


# ── Refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"frequency_weight": 0.2}, "metadata.frequency_weight requires metadata.frequency_property"),
        ({"frequency_scale": 10.0}, "metadata.frequency_scale requires metadata.frequency_property"),
        ({"acceleration_weight": 0.2}, "metadata.acceleration_weight requires metadata.acceleration_property"),
        (
            {
                "frequency_property": "dock_count",
                "frequency_weight": 0.7,
                "acceleration_property": "dock_rate_rising",
                "acceleration_weight": 0.5,
            },
            "auxiliary signal weights sum to 1.2000, exceeding 1.0",
        ),
    ],
    ids=["freq-weight-alone", "freq-scale-alone", "accel-weight-alone", "weights-over-one"],
)
@pytest.mark.usefixtures("declared")
def test_inconsistent_declarations_are_refused(metadata: dict[str, Any], message: str) -> None:
    dimension = _dimension(metadata)
    with pytest.raises(ValueError, match=re.escape(message)):
        _compile(dimension)


@pytest.mark.usefixtures("declared")
@pytest.mark.parametrize("prop", ["dock_count) DETACH DELETE candidate //", "1bad", "a b"])
def test_property_names_pass_sanitize_label(prop: str) -> None:
    frequency = _dimension({"frequency_property": prop})
    acceleration = _dimension({"acceleration_property": prop})
    with pytest.raises(ValueError, match="Invalid label"):
        _compile(frequency)
    with pytest.raises(ValueError, match="Invalid label"):
        _compile(acceleration)


@pytest.mark.parametrize(
    ("metadata", "field"),
    [
        ({"frequency_property": "dock_count", "frequency_weight": 1.5}, "frequency_weight"),
        ({"frequency_property": "dock_count", "frequency_weight": -0.1}, "frequency_weight"),
        ({"frequency_property": "dock_count", "frequency_scale": 0.0}, "frequency_scale"),
        ({"acceleration_property": "dock_rate_rising", "acceleration_weight": 2}, "acceleration_weight"),
    ],
)
def test_out_of_range_metadata_is_refused_at_validation(metadata: dict[str, Any], field: str) -> None:
    dimension = _dimension(metadata)
    with pytest.raises(ValidationError, match=field):
        _spec(dimension)


def test_metadata_temporalproximity_does_not_read_is_refused() -> None:
    message = "metadata key(s) success_value are not read by computation 'temporalproximity'"
    dimension = _dimension({"success_value": "docked"})
    with pytest.raises(ValidationError, match=re.escape(message)):
        _spec(dimension)


def test_temporalproximity_keys_are_refused_on_other_computations() -> None:
    message = "metadata key(s) frequency_property are not read by computation 'lognormalized'"
    dimension = _dimension({"frequency_property": "dock_count"}, computation="lognormalized")
    with pytest.raises(ValidationError, match=re.escape(message)):
        _spec(dimension)


# ── Committed packs: what the flag changes ───────────────────────────────────


def _committed_temporalproximity() -> list[tuple[str, Any]]:
    found = []
    for spec_path in sorted(DESIGN_PACKS_ROOT.glob("*/spec.yaml")):
        spec = DomainSpec.model_validate(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
        found.extend(
            (spec_path.parent.name, dim)
            for dim in spec.scoring.dimensions
            if dim.computation.value == "temporalproximity"
        )
    return found


def test_no_committed_pack_declares_the_legacy_auxiliary_properties() -> None:
    """The legacy terms read properties no committed pack declares, so they always read null there.

    Under the flag those terms are dropped and recency takes their weight:
    the documented change (FEATURE_GATES §37) is that such a pack's recency
    ranges over [0, 1] instead of [0, 0.6]; the order of candidates by recency
    is unchanged, because both forms are the same monotone decay scaled.
    """
    dims = _committed_temporalproximity()
    assert dims, "expected at least one committed temporalproximity dimension"
    for pack, _ in dims:
        text = (DESIGN_PACKS_ROOT / pack / "spec.yaml").read_text(encoding="utf-8")
        assert "touch_count_30d" not in text
        assert "is_accelerating" not in text


def test_the_served_committed_temporalproximity_pack_is_plasticos() -> None:
    """D2 evidence coverage: tests/integration/test_temporalproximity_rescale_fixtures.py records every served pack.

    legal-discovery and research-agent declare temporalproximity but are dormant
    (not served); a pack that becomes served must be added to that fixture.
    """
    served: list[str] = []
    for path in sorted(DESIGN_PACKS_ROOT.glob("*/spec.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        uses = any(d.get("computation") == "temporalproximity" for d in raw["scoring"]["dimensions"])
        if uses and raw["domain"].get("activation") != "dormant":
            served.append(path.parent.name)
    assert served == ["plasticos"]
