"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, feedback, signal-weights]
owner: engine-team
status: active
--- /L9_META ---

Integration — learned weights count the outcomes the handler writes (CEG-FEEDBACK-1, FEATURE_GATES §36).

Outcomes are written through the real ``handle_outcomes`` handler on the
plasticos design pack (enterprise testcontainer), never seeded with
``<dim>_score``. The suite first proves how ``fingerprint.dimension_scores`` is
persisted (a sorted-key JSON string; no ``<dim>_score`` property is written),
then shows ``SignalWeightCalculator`` counting those outcomes per dimension
with ``signal_weight_dimension_scores_enabled`` on, and counting none (the
baseline, as on main) with it off.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.settings import settings
from engine.models.outcomes import encode_outcome_map

pytestmark = pytest.mark.integration

_TENANT = "plasticos"
_DIM = "geo_proximity"
_OTHER_DIM = "price_alignment"
# Ten outcomes, six wins; geo_proximity recorded on the first five (four wins),
# price_alignment on outcomes 6-8 (two wins).
_WINS = {1, 2, 3, 4, 7, 8}


def _scores(i: int) -> dict[str, float] | None:
    if i <= 5:
        return {_DIM: 0.9 - i / 100}
    if i <= 8:
        return {_OTHER_DIM: 0.4}
    return None


@pytest.fixture
def domain_spec(plasticos_domain_spec: Any) -> Any:
    """The plasticos design pack's spec: the recorded dimensions are that pack's."""
    return plasticos_domain_spec


@pytest.fixture
async def handler_outcomes(design_pack_deps, graph_driver, monkeypatch) -> Any:
    """Ten outcomes written by handle_outcomes (feedback loop off: only the write runs)."""
    from engine.handlers import handle_outcomes

    monkeypatch.setattr(settings, "feedback_enabled", False)
    assert settings.outcome_schema_unified, "dimension_scores is written only by the unified outcome schema"
    for i in range(1, 11):
        fingerprint: dict[str, Any] = {"active_dimensions": [_DIM, _OTHER_DIM], "candidate_count": 3}
        scores = _scores(i)
        if scores is not None:
            fingerprint["dimension_scores"] = scores
        result = await handle_outcomes(
            _TENANT,
            {
                "match_id": f"feedback1-m{i}",
                "candidate_id": f"feedback1-c{i}",
                "outcome": "success" if i in _WINS else "failure",
                "fingerprint": fingerprint,
            },
        )
        assert result["status"] == "recorded"
    return graph_driver


async def _change_records(graph_driver: Any) -> dict[str, dict[str, Any]]:
    rows = await graph_driver.execute_query(
        "MATCH (c:DimensionWeightChange {tenant: $tenant}) "
        "RETURN c.dimension AS dimension, c.sample_size AS sample_size, c.lift AS lift, "
        "c.outcome_count AS outcomes, c.win_count AS wins, c.applied AS applied",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )
    return {row["dimension"]: row for row in rows}


async def test_dimension_scores_persist_as_codec_json_and_no_score_property(handler_outcomes, domain_spec):
    rows = await handler_outcomes.execute_query(
        "MATCH (o:TransactionOutcome {tenant: $tenant}) "
        "RETURN o.match_id AS match_id, o.dimension_scores AS stored, "
        "o.geo_proximity_score AS legacy, keys(o) AS keys ORDER BY o.match_id",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )
    assert len(rows) == 10
    by_match = {row["match_id"]: row for row in rows}
    for i in range(1, 11):
        row = by_match[f"feedback1-m{i}"]
        scores = _scores(i)
        if scores is None:
            assert row["stored"] is None
        else:
            # A Neo4j property cannot hold a map: the handler stores the RU-005 JSON text.
            assert isinstance(row["stored"], str)
            assert row["stored"] == encode_outcome_map(scores)
        assert row["legacy"] is None
        assert not any(key.endswith("_score") for key in row["keys"])


async def test_flag_on_counts_handler_written_outcomes(handler_outcomes, domain_spec, monkeypatch):
    from engine.feedback.signal_weights import SignalWeightCalculator

    monkeypatch.setattr(settings, "signal_weight_dimension_scores_enabled", True)
    monkeypatch.setattr(settings, "learned_weight_change_record_enabled", True)
    monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", False)

    weights = await SignalWeightCalculator(handler_outcomes, domain_spec).recalculate_weights()

    records = await _change_records(handler_outcomes)
    assert records[_DIM]["sample_size"] == 5
    assert records[_OTHER_DIM]["sample_size"] == 3
    assert all(r["outcomes"] == 10 and r["wins"] == 6 and r["applied"] is False for r in records.values())
    # geo_proximity: 4/5 wins against a 6/10 base rate -> lift 4/3; the weight left the baseline.
    assert records[_DIM]["lift"] == pytest.approx(0.8 / 0.6, rel=1e-5)
    assert records[_OTHER_DIM]["lift"] == pytest.approx((2 / 3) / 0.6, rel=1e-5)
    baseline = domain_spec.feedbackloop.signal_weights.baseline_weight
    assert weights[_DIM] != pytest.approx(baseline)
    # A dimension no outcome recorded stays at the baseline with no sample.
    unrecorded = [d.name for d in domain_spec.scoring.dimensions if d.name not in (_DIM, _OTHER_DIM)]
    assert unrecorded
    for name in unrecorded:
        assert records[name]["sample_size"] == 0
        assert weights[name] == pytest.approx(baseline)


async def test_flag_off_behaves_as_main_and_counts_none(handler_outcomes, domain_spec, monkeypatch):
    from engine.feedback.signal_weights import SignalWeightCalculator

    monkeypatch.setattr(settings, "signal_weight_dimension_scores_enabled", False)
    monkeypatch.setattr(settings, "learned_weight_change_record_enabled", True)
    monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", False)

    weights = await SignalWeightCalculator(handler_outcomes, domain_spec).recalculate_weights()

    records = await _change_records(handler_outcomes)
    baseline = domain_spec.feedbackloop.signal_weights.baseline_weight
    assert records, "a recalculation over ten outcomes records every dimension"
    assert all(r["sample_size"] == 0 for r in records.values())
    assert all(w == pytest.approx(baseline) for w in weights.values())


async def test_flag_on_still_counts_a_legacy_score_property(handler_outcomes, domain_spec, monkeypatch):
    """Outcomes stored in the old `<dim>_score` shape keep counting: nothing is migrated or dropped."""
    from engine.feedback.signal_weights import SignalWeightCalculator

    monkeypatch.setattr(settings, "signal_weight_dimension_scores_enabled", True)
    monkeypatch.setattr(settings, "learned_weight_change_record_enabled", True)
    monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", False)
    # Outcome 10 recorded no dimension_scores; give it the legacy property instead.
    await handler_outcomes.execute_query(
        "MATCH (o:TransactionOutcome {tenant: $tenant, match_id: 'feedback1-m10'}) SET o.geo_proximity_score = 0.5",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )

    await SignalWeightCalculator(handler_outcomes, domain_spec).recalculate_weights()

    records = await _change_records(handler_outcomes)
    assert records[_DIM]["sample_size"] == 6
