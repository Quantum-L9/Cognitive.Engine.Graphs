"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, temporalproximity, plasticos]
owner: engine-team
status: active
--- /L9_META ---

SC-001 (audit L5): the declared-signal temporalproximity compiler is the default.

Driven end to end on a real graph with default settings: facilities are written
by the engine's own write path (``handle_sync`` on the committed ``plasticos``
pack, whose ``recency`` dimension reads ``last_transaction_date`` with
``maxvalue: 180``), then scored by ``handle_match``. Under the default:

- a just-transacted facility scores recency 1.0 (the legacy compiler capped it at 0.6);
- a facility last transacted ``maxvalue`` days ago scores ``exp(-1)``;
- a future transaction date never outscores a fresh one (D2 floor at age 0);
- a stray boolean auxiliary property on one candidate does not fail the match,
  because the declared path reads only properties the dimension declares.

``last_transaction_date`` is sent as a timezone-aware ISO-8601 string, the only
form JSON can carry. It is stored as a native ``DateTime`` by the D1 sync
conversion (``sync_temporal_native_values_enabled``, campaign lane L1), which
this test therefore requires in the stack it runs on.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

PLASTICOS = "plasticos"
_BUYER = "supply_opportunity_to_buyer_facility"
_MAXVALUE_DAYS = 180  # the committed pack's recency horizon (spec.yaml `recency.maxvalue`)


def _facility(facility_id: str, last_transaction: datetime | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "facility_id": facility_id,
        "name": "Buyer",
        "lat": 40.0,
        "lon": -75.0,
        "credit_score": 100.0,
        "min_density": 0.9,
        "max_density": 1.0,
        "min_mfi": 2.0,
        "max_mfi": 12.0,
        "contamination_tolerance": 0.0,
        "food_grade_certified": False,
        "capacity_tons_month": 40.0,
        "community_id": 7,
        "rate_per_ton": 1000.0,
        "processed_polymers": [
            {"code": "HDPE", "line_id": f"{facility_id}-line", "direction": "in", "form": "PALLETS"},
        ],
    }
    if last_transaction is not None:
        row["last_transaction_date"] = last_transaction.isoformat()
    return row


async def _sync(rows: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": rows})


async def _match() -> dict[str, dict[str, Any]]:
    from engine.handlers import handle_match

    result = await handle_match(
        PLASTICOS,
        {
            "query": {
                "polymer_type": "HDPE",
                "contamination_pct": 0.0,
                "volume_tons": 18.0,
                "lat": 40.0,
                "lon": -75.0,
                "target_price": 1000.0,
                "community_id": 7,
            },
            "match_direction": _BUYER,
            "top_n": 10,
        },
    )
    return {row["candidate"]["facility_id"]: row for row in result["candidates"]}


def _assert_default_settings() -> None:
    from engine.config.settings import settings

    assert settings.temporalproximity_declared_signals_enabled is True, (
        "this test asserts the shipped default; it must not run with TEMPORALPROXIMITY_DECLARED_SIGNALS_ENABLED=false"
    )


@pytest.mark.asyncio
async def test_default_recency_is_pure_declared_decay(design_pack_deps, graph_driver) -> None:
    _assert_default_settings()
    now = datetime.now(tz=UTC)
    await _sync(
        [
            _facility("facility:fresh", now),
            # maxvalue days and one hour back: whole-day age is exactly maxvalue
            # whenever the match runs within the next 23 hours.
            _facility("facility:horizon", now - timedelta(days=_MAXVALUE_DAYS, hours=1)),
            _facility("facility:future", now + timedelta(days=30)),
            _facility("facility:undated", None),
        ]
    )

    got = await _match()
    assert set(got) == {"facility:fresh", "facility:horizon", "facility:future", "facility:undated"}

    fresh = got["facility:fresh"]["dimension_scores"]["recency"]
    horizon = got["facility:horizon"]["dimension_scores"]["recency"]
    future = got["facility:future"]["dimension_scores"]["recency"]

    assert fresh == pytest.approx(1.0, abs=1e-6), "the legacy compiler capped a fresh date at 0.6"
    assert horizon == pytest.approx(math.exp(-1.0), abs=1e-6)
    assert future <= fresh + 1e-12, "a future date must never outscore a fresh one"
    assert future == pytest.approx(1.0, abs=1e-6)
    assert got["facility:undated"]["dimension_scores"]["recency"] == pytest.approx(0.0, abs=1e-6)

    # Every other dimension is equal across the four, so the composite orders by recency.
    assert got["facility:fresh"]["score"] > got["facility:horizon"]["score"] > got["facility:undated"]["score"]
    assert got["facility:future"]["score"] == pytest.approx(got["facility:fresh"]["score"], abs=1e-6)


@pytest.mark.asyncio
async def test_boolean_auxiliary_property_on_one_candidate_does_not_fail_the_match(
    design_pack_deps, graph_driver
) -> None:
    """The legacy compiler read ``candidate.is_accelerating`` through ``toFloat`` on every pack.

    No engine write path can put that property on a node: declared-projection
    sync drops undeclared keys. A node carrying it is legacy data written before
    declared projection, so this one property is set directly; everything else
    is written by ``handle_sync``.
    """
    _assert_default_settings()
    now = datetime.now(tz=UTC)
    await _sync([_facility("facility:legacy-flag", now), _facility("facility:clean", now)])
    await graph_driver.execute_write(
        cypher="MATCH (f:Facility {facility_id: $id}) SET f.is_accelerating = true",
        parameters={"id": "facility:legacy-flag"},
        database=PLASTICOS,
    )

    got = await _match()
    assert set(got) == {"facility:legacy-flag", "facility:clean"}
    flagged = got["facility:legacy-flag"]
    assert flagged["dimension_scores"]["recency"] == pytest.approx(1.0, abs=1e-6)
    assert flagged["score"] == pytest.approx(got["facility:clean"]["score"], abs=1e-6)
