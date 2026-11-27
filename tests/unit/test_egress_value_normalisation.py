"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, packet, egress, temporal]
owner: engine-team
status: active
--- /L9_META ---

Egress value normalisation (ceg-scoring-audit L1, D1 / ND-SERIAL, SC-003).

A Neo4j temporal in a response row (a native ``last_transaction_date``, or the
``updated_at`` an UNWINDMATCHSET sync stamps with ``datetime()``) crashed
``deflate_egress`` with a raw ``TypeError``; a stored ``Infinity`` crashed it
with a raw ``ValueError``. ``deflate_egress`` now normalises every temporal to
ISO-8601 once, in place, so the response packet and the dict the chassis
returns carry the same JSON-safe value; a non-finite float raises the typed
``EgressValueError`` naming where it sits, never the value.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from neo4j.time import Date as NeoDate
from neo4j.time import DateTime as NeoDateTime
from neo4j.time import Duration as NeoDuration
from neo4j.time import Time as NeoTime

from engine.config.settings import settings
from engine.packet.chassis_contract import (
    EgressValueError,
    deflate_egress,
    inflate_ingress,
    normalise_egress_values,
)

pytestmark = pytest.mark.unit


def _request() -> Any:
    return inflate_ingress(action="match", payload={"query": {}}, tenant="plasticos", trace_id="trace-1")


def _deflate(engine_data: dict[str, Any]) -> Any:
    return deflate_egress(
        request=_request(), engine_data=engine_data, processing_ms=1.0, responding_node="graph-engine"
    )


def _match_rows() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "candidate_id": "f1",
                "score": 0.75,
                "properties": {
                    "last_transaction_date": NeoDateTime(2026, 11, 20, 12, 0, 0, tzinfo=UTC),
                    "updated_at": NeoDateTime(2026, 11, 26, 8, 30, 15, 123456789, tzinfo=UTC),
                    "opened_on": NeoDate(2026, 1, 2),
                    "window": NeoDuration(days=3, hours=4),
                    "tags": [NeoDate(2026, 3, 4), "x"],
                },
            }
        ],
        "total": 1,
    }


def test_neo4j_temporals_in_candidate_rows_serialise_to_iso_8601() -> None:
    engine_data = _match_rows()
    response = _deflate(engine_data)
    props = response.payload["data"]["candidates"][0]["properties"]
    assert props["last_transaction_date"] == "2026-11-20T12:00:00.000000000+00:00"
    assert props["updated_at"] == "2026-11-26T08:30:15.123456789+00:00"  # nanoseconds kept
    assert props["opened_on"] == "2026-01-02"
    assert props["window"] == "P3DT4H"
    assert props["tags"] == ["2026-03-04", "x"]
    json.dumps(response.payload)  # wire-safe


def test_normalisation_is_in_place_so_the_chassis_returns_the_same_values() -> None:
    # chassis/actions.py returns the handler's own dict as "data" after deflate_egress.
    engine_data = _match_rows()
    response = _deflate(engine_data)
    assert engine_data["candidates"][0]["properties"]["opened_on"] == "2026-01-02"
    assert response.payload["data"] == engine_data
    json.dumps(engine_data)


def test_normalisation_is_deterministic() -> None:
    first, second = _match_rows(), _match_rows()
    normalise_egress_values(first)
    normalise_egress_values(second)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.parametrize(
    ("value", "iso"),
    [
        (datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone(timedelta(hours=2))), "2026-05-06T07:08:09+02:00"),
        (date(2026, 5, 6), "2026-05-06"),
        (time(7, 8, 9), "07:08:09"),
        (timedelta(days=1, seconds=30), "P1DT30S"),
        (NeoTime(7, 8, 9), "07:08:09.000000000"),
    ],
)
def test_python_temporals_are_normalised_too(value: Any, iso: str) -> None:
    data = {"rows": [{"v": value}], "pair": (value, 1)}
    normalise_egress_values(data)
    assert data["rows"][0]["v"] == iso
    assert data["pair"] == (iso, 1)


def test_finite_values_are_left_alone() -> None:
    data = {"a": 1, "b": 0.5, "c": "s", "d": None, "e": True, "f": [1.0, {"g": -0.0}]}
    expected = json.loads(json.dumps(data))
    normalise_egress_values(data)
    assert data == expected


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_float_raises_a_typed_named_error(value: float) -> None:
    data = {"candidates": [{"candidate_id": "f1", "properties": {"rate_per_ton": value}}]}
    with pytest.raises(EgressValueError) as raised:
        _deflate(data)
    message = str(raised.value)
    assert "data.candidates[0].properties.rate_per_ton" in message
    assert "non-finite float" in message
    assert raised.value.path == "data.candidates[0].properties.rate_per_ton"
    assert "inf" not in message.lower().replace("non-finite", "")
    assert "nan" not in message.lower()


def test_an_unknown_type_is_named_not_a_raw_type_error() -> None:
    class Opaque:
        pass

    with pytest.raises(EgressValueError, match=r"data\.x: Opaque is not JSON-serialisable"):
        _deflate({"x": Opaque()})


def test_flag_off_restores_the_legacy_raw_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "egress_value_normalisation_enabled", False)
    with pytest.raises(TypeError):
        _deflate(_match_rows())
