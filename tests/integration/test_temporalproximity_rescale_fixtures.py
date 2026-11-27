"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, temporal, neo4j]
owner: engine-team
status: active
--- /L9_META ---

D2 comparison fixtures: legacy vs declared temporalproximity on the committed packs (FEATURE_GATES §37).

``temporalproximity_declared_signals_enabled`` stays default ``False``
(operator decision D2, 2026-11-26); activation is a later, separate decision.
These tests record its evidence on a real graph. Facilities are written by
``handle_sync`` and scored by ``handle_match`` on the committed ``plasticos``
design pack, once with the flag off (legacy) and once on (declared):

* no committed pack declares the legacy auxiliary properties or any metadata
  key, so for a past or present date ``value_declared = value_legacy / 0.6``;
* a future date: legacy ``0.6 * exp(+age/horizon)`` outscores a fresh date;
  the declared path floors the age at 0, so it equals the fresh score;
* the composite ranking of the dated facilities.

``legal-discovery`` and ``research-agent`` also declare ``temporalproximity``
but are dormant packs (``domain.activation: dormant``), not served, and their
dimensions name no candidateprop; ``test_the_served_committed_temporalproximity_pack_is_plasticos``
pins that (``tests/unit/test_temporalproximity_declared_signals.py``), so a newly served pack
has to be added here.

Storage note: ``last_transaction_date`` is a declared ``datetime``; native
temporal storage at sync is lane L1 (decision D1). These tests run on the
combined stack.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engine.config.settings import settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_PACK = "plasticos"
_DIRECTION = "supply_opportunity_to_buyer_facility"
_FLAG = "temporalproximity_declared_signals_enabled"
_LEGACY_RECENCY_WEIGHT = 0.6


def _iso(days_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_DATED = {"fresh": 0, "month-old": -30, "quarter-old": -90, "future": 30}


def _facility(facility_id: str, days_from_now: int) -> dict[str, Any]:
    return {
        "facility_id": facility_id,
        "name": f"PET facility ({facility_id})",
        "contamination_tolerance": 0.10,
        "direction": "in",
        "form": "PELLET",
        "last_transaction_date": _iso(days_from_now),
        "processed_polymers": [{"code": "PET", "line_id": f"pet-{facility_id}"}],
    }


async def _sync_dated() -> None:
    from engine.handlers import handle_sync

    batch = [_facility(fid, days) for fid, days in _DATED.items()]
    result = await handle_sync(_PACK, {"entity_type": "facilities", "batch": batch})
    assert result.get("status") in ("ok", "success"), result


async def _match() -> dict[str, dict[str, Any]]:
    from engine.handlers import handle_match

    result = await handle_match(
        _PACK,
        {"query": {"polymer_type": "PET", "contamination_pct": 0.04}, "match_direction": _DIRECTION, "top_n": 20},
    )
    return {row["candidate"]["facility_id"]: row for row in result["candidates"]}


async def _both() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    await _sync_dated()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, _FLAG, False)
        legacy = await _match()
        mp.setattr(settings, _FLAG, True)
        declared = await _match()
    assert set(legacy) == set(declared) == set(_DATED)
    return legacy, declared


def _recency(rows: dict[str, dict[str, Any]], facility_id: str) -> float:
    return float(rows[facility_id]["dimension_scores"]["recency"])


def _ranking(rows: dict[str, dict[str, Any]]) -> list[str]:
    return [fid for fid, _row in sorted(rows.items(), key=lambda item: (-item[1]["score"], item[0]))]


async def test_declared_recency_is_legacy_recency_rescaled_by_one_over_0_6(design_pack_deps, record_property) -> None:
    legacy, declared = await _both()
    for facility_id in ("fresh", "month-old", "quarter-old"):
        value_legacy = _recency(legacy, facility_id)
        value_declared = _recency(declared, facility_id)
        record_property(f"recency[{facility_id}]", {"legacy": value_legacy, "declared": value_declared})
        assert value_declared == pytest.approx(value_legacy / _LEGACY_RECENCY_WEIGHT, abs=1e-6)
    assert _recency(declared, "fresh") == pytest.approx(1.0, abs=1e-6)
    assert _recency(legacy, "fresh") == pytest.approx(_LEGACY_RECENCY_WEIGHT, abs=1e-6)


async def test_a_future_date_never_outscores_a_fresh_one_on_the_declared_path(
    design_pack_deps, record_property
) -> None:
    legacy, declared = await _both()
    record_property("recency[future]", {"legacy": _recency(legacy, "future"), "declared": _recency(declared, "future")})
    # Declared (D2 floor): the future date's age is 0 days, exactly the fresh score.
    assert _recency(declared, "future") == pytest.approx(_recency(declared, "fresh"), abs=1e-6)
    assert _recency(declared, "future") <= 1.0
    # Legacy, unchanged byte for byte (FEATURE_GATES §37): exp of a positive exponent beats fresh.
    assert _recency(legacy, "future") > _recency(legacy, "fresh")


async def test_composite_ranking_under_each_path(design_pack_deps, record_property) -> None:
    legacy, declared = await _both()
    legacy_rank, declared_rank = _ranking(legacy), _ranking(declared)
    record_property("ranking", {"legacy": legacy_rank, "declared": declared_rank})
    record_property(
        "scores",
        {fid: {"legacy": legacy[fid]["score"], "declared": declared[fid]["score"]} for fid in _DATED},
    )
    # The facilities differ only in their date, so recency alone orders the past/present ones on both paths.
    past = ["fresh", "month-old", "quarter-old"]
    assert [fid for fid in legacy_rank if fid in past] == past
    assert [fid for fid in declared_rank if fid in past] == past
    # Legacy ranks the future date first; declared ties it with fresh (equal score).
    assert legacy_rank[0] == "future"
    assert declared["future"]["score"] == pytest.approx(declared["fresh"]["score"], abs=1e-9)
    # The recency contribution grows by 1/0.6 under the flag, so every dated composite rises.
    for fid in past:
        assert declared[fid]["score"] > legacy[fid]["score"]
