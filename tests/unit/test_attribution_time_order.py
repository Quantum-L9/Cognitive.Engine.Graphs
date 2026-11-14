"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, causal, attribution]
owner: engine-team
status: active
--- /L9_META ---

SI-27: first_touch / last_touch / position_based order touchpoints by time.

``causal_attribution_time_ordered`` (default off). Off, the trace Cypher and the
weights are the legacy ones (order by path distance). On, order-dependent models
order by each source label's declared ``CausalEdgeSpec.source_timestamp_property``;
a touchpoint with no declaration or a NULL value is excluded and counted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.causal.attribution import (
    AttributionCalculator,
    TouchpointTimestampConflictError,
    _time_ordered_rows,
    _timestamp_key,
)
from engine.config.schema import CausalEdgeSpec, DomainSpec
from engine.config.settings import settings
from tests.unit.test_causal_edges import _spec_with_attribution

pytestmark = pytest.mark.unit

FLAG = "causal_attribution_time_ordered"

# Distance order: near (d1), mid (d2), far (d3). Time order: far, near, mid.
ROWS = [
    {
        "touchpoint_id": "near",
        "touchpoint_ts": datetime(2026, 5, 1, tzinfo=UTC),
        "distance": 1,
        "confidences": [1.0],
        "ages_days": [1],
    },
    {
        "touchpoint_id": "mid",
        "touchpoint_ts": datetime(2026, 6, 1, tzinfo=UTC),
        "distance": 2,
        "confidences": [1.0],
        "ages_days": [1],
    },
    {
        "touchpoint_id": "far",
        "touchpoint_ts": datetime(2026, 1, 1, tzinfo=UTC),
        "distance": 3,
        "confidences": [1.0],
        "ages_days": [1],
    },
    {"touchpoint_id": "undated", "touchpoint_ts": None, "distance": 1, "confidences": [1.0], "ages_days": [1]},
]


def _spec(timestamp_property: str | None = "occurred_at") -> DomainSpec:
    spec = _spec_with_attribution()
    edge = spec.causal.causal_edges[0].model_copy(update={"source_timestamp_property": timestamp_property})
    return spec.model_copy(update={"causal": spec.causal.model_copy(update={"causal_edges": [edge]})})


def _calc(rows: list[dict[str, Any]], spec: DomainSpec | None = None) -> AttributionCalculator:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=rows)
    return AttributionCalculator(driver, spec or _spec())


def _cypher(calc: AttributionCalculator) -> str:
    return calc._driver.execute_query.call_args.args[0]


class TestSchema:
    def test_field_optional(self) -> None:
        assert CausalEdgeSpec(edge_type="E", source_label="A", target_label="B").source_timestamp_property is None


class TestFlagOffIsLegacy:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, FLAG, False)

    def test_default_is_off(self) -> None:
        assert type(settings).model_fields[FLAG].default is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["first_touch", "last_touch", "position_based", "linear"])
    async def test_cypher_and_result_identical(self, model) -> None:
        legacy_rows = [{k: v for k, v in r.items() if k != "touchpoint_ts"} for r in ROWS]
        declared = _calc(legacy_rows)
        undeclared = _calc(legacy_rows, _spec(None))
        a = await declared.compute_attribution("o1", model=model)
        b = await undeclared.compute_attribution("o1", model=model)
        assert _cypher(declared) == _cypher(undeclared)
        assert "touchpoint_ts" not in _cypher(declared)
        assert a == b
        assert "untimestamped_touchpoints" not in a

    @pytest.mark.asyncio
    async def test_first_touch_is_nearest(self) -> None:
        res = await _calc(ROWS).compute_attribution("o1", model="first_touch")
        assert res["touchpoints"]["near"] == 1.0


class TestFlagOnTimeOrder:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, FLAG, True)

    @pytest.mark.asyncio
    async def test_cypher_reads_declared_property(self) -> None:
        calc = _calc(ROWS)
        await calc.compute_attribution("o1", model="first_touch")
        assert "CASE WHEN touchpoint:Facility THEN touchpoint.occurred_at ELSE null END AS touchpoint_ts" in _cypher(
            calc
        )

    @pytest.mark.asyncio
    async def test_first_touch_is_earliest(self) -> None:
        res = await _calc(ROWS).compute_attribution("o1", model="first_touch")
        assert res["touchpoints"] == {"far": 1.0, "near": 0.0, "mid": 0.0}
        assert res["untimestamped_touchpoints"] == 1
        assert res["ordering"] == "event_timestamp"
        assert res["total_touchpoints"] == 3

    @pytest.mark.asyncio
    async def test_last_touch_is_latest(self) -> None:
        res = await _calc(ROWS).compute_attribution("o1", model="last_touch")
        assert res["touchpoints"]["mid"] == 1.0

    @pytest.mark.asyncio
    async def test_position_based(self) -> None:
        res = await _calc(ROWS).compute_attribution("o1", model="position_based")
        assert res["touchpoints"] == {"far": 0.4, "near": 0.2, "mid": 0.4}

    @pytest.mark.asyncio
    async def test_linear_unaffected(self) -> None:
        calc = _calc(ROWS)
        res = await calc.compute_attribution("o1", model="linear")
        assert "touchpoint_ts" not in _cypher(calc)
        assert "untimestamped_touchpoints" not in res
        assert set(res["touchpoints"]) == {"near", "mid", "far", "undated"}

    @pytest.mark.asyncio
    async def test_no_declaration_excludes_all(self) -> None:
        rows = [{**r, "touchpoint_ts": None} for r in ROWS]
        calc = _calc(rows, _spec(None))
        res = await calc.compute_attribution("o1", model="first_touch")
        assert "null AS touchpoint_ts" in _cypher(calc)
        assert res["touchpoints"] == {}
        assert res["untimestamped_touchpoints"] == 4

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        res = await _calc([]).compute_attribution("o1", model="first_touch")
        assert res == {"touchpoints": {}, "model": "first_touch", "chain_depth": 0, "untimestamped_touchpoints": 0}

    def test_conflicting_declarations_refused(self) -> None:
        spec = _spec()
        edges = [
            *spec.causal.causal_edges,
            spec.causal.causal_edges[0].model_copy(update={"source_timestamp_property": "other_at"}),
        ]
        spec = spec.model_copy(update={"causal": spec.causal.model_copy(update={"causal_edges": edges})})
        with pytest.raises(TouchpointTimestampConflictError):
            _calc([], spec)._touchpoint_timestamp_expression()

    def test_unsafe_property_rejected(self) -> None:
        with pytest.raises(ValueError):
            _calc([], _spec("x} RETURN 1 //"))._touchpoint_timestamp_expression()


class TestOrderingHelpers:
    def test_keys(self) -> None:
        assert _timestamp_key(None) is None
        assert _timestamp_key(True) is None
        assert _timestamp_key("not a time") is None
        assert _timestamp_key(float("nan")) is None
        assert _timestamp_key(5) == ("number", 5.0)
        # a naive ISO timestamp is read as UTC
        assert _timestamp_key("2026-01-01T00:00:00") == _timestamp_key(datetime(2026, 1, 1, tzinfo=UTC))
        assert _timestamp_key("2026-01-01T00:00:00+00:00") == _timestamp_key(date(2026, 1, 1))

    def test_ties_by_id(self) -> None:
        t = datetime(2026, 1, 1, tzinfo=UTC)
        rows, excluded = _time_ordered_rows(
            [{"touchpoint_id": "b", "touchpoint_ts": t}, {"touchpoint_id": "a", "touchpoint_ts": t}]
        )
        assert [r["touchpoint_id"] for r in rows] == ["a", "b"]
        assert excluded == 0

    def test_mixed_kinds_refused(self) -> None:
        with pytest.raises(TouchpointTimestampConflictError):
            _time_ordered_rows(
                [{"touchpoint_id": "a", "touchpoint_ts": 5}, {"touchpoint_id": "b", "touchpoint_ts": date(2026, 1, 1)}]
            )
