"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, scoring, confidence]
owner: engine-team
status: active
--- /L9_META ---

SCR-13 remainder (ceg-scoring-audit L10) against a real Neo4j.

Depots are written by ``handle_sync`` on the orbital-depots endpoint and read
by ``handle_match``. The orbital-logistics pack scores ``0.6 * reliability +
0.3 * port_capacity`` with ``port_capacity = log(1 + ports) / log(1001)``.

Weighted monoculture (the default) flags a depot when one of ``0.6 r`` and
``0.3 p`` is more than 70% of their sum; raw monoculture compares ``r`` and
``p`` themselves.
Low dimensional agreement flags ``|r - p| > 0.6`` (``1 - 2 * std < 0.4``).

=========  ===========  =====  ======  ==================  ==================
depot      reliability  ports  p       weighted flags      raw flags
=========  ===========  =====  ======  ==================  ==================
clean      0.50         500    0.8998  []                  []
mono       0.90         100    0.6680  [mono]              []
agree      0.25         900    0.9847  [low]               [mono, low]
both       0.95         2      0.1590  [mono, low]         [mono, low]
=========  ===========  =====  ======  ==================  ==================
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.settings import settings
from engine.scoring.confidence import FLAG_LOW_DIMENSIONAL_AGREEMENT as LOW
from engine.scoring.confidence import FLAG_MONOCULTURE as MONO
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, serve

pytestmark = pytest.mark.integration

DEPOTS = (("clean", 0.50, 500), ("mono", 0.90, 100), ("agree", 0.25, 900), ("both", 0.95, 2))
WEIGHTED = {"clean": [], "mono": [MONO], "agree": [LOW], "both": [MONO, LOW]}
RAW = {"clean": [], "mono": [], "agree": [MONO, LOW], "both": [MONO, LOW]}


async def _flags(graph_driver: Any, domain_spec: Any) -> dict[str, dict[str, Any]]:
    from engine.handlers import handle_match, handle_sync

    batch = [
        {"depot_id": name, "reliability": r, "docking_ports": ports, "accepts_hazmat": True}
        for name, r, ports in DEPOTS
    ]
    synced = await handle_sync(ORBITAL, {"entity_type": "orbital-depots", "batch": batch})
    assert synced.get("status") in ("ok", "success"), synced
    serve(graph_driver, domain_spec)
    result = await handle_match(ORBITAL, {"query": {}, "match_direction": DIRECTION, "top_n": 10})
    return {row["candidate"]["depot_id"]: row for row in result["candidates"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("weighted", "expected"), [(True, WEIGHTED), (False, RAW)], ids=["weighted", "raw"])
async def test_every_candidate_carries_a_list_of_zero_one_or_two_flags(
    *,
    weighted: bool,
    expected: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    engine_deps: Any,
    graph_driver: Any,
    domain_spec: Any,
    clean_db: Any,
) -> None:
    monkeypatch.setattr(settings, "monoculture_weighted_contribution", weighted)
    rows = await _flags(graph_driver, domain_spec)

    assert {name: row["confidence_flag"] for name, row in rows.items()} == expected
    for row in rows.values():
        assert isinstance(row["confidence_flag"], list)
        assert all(isinstance(flag, str) for flag in row["confidence_flag"])
        assert ("confidence_detail" in row) is bool(row["confidence_flag"])


@pytest.mark.asyncio
async def test_flag_off_restores_absent_string_or_pair(
    monkeypatch: pytest.MonkeyPatch, engine_deps: Any, graph_driver: Any, domain_spec: Any, clean_db: Any
) -> None:
    monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
    rows = await _flags(graph_driver, domain_spec)

    assert "confidence_flag" not in rows["clean"]
    assert rows["mono"]["confidence_flag"] == MONO
    assert rows["agree"]["confidence_flag"] == LOW
    assert rows["both"]["confidence_flag"] == [MONO, LOW]


@pytest.mark.asyncio
async def test_checks_off_leave_the_key_absent(
    monkeypatch: pytest.MonkeyPatch, engine_deps: Any, graph_driver: Any, domain_spec: Any, clean_db: Any
) -> None:
    monkeypatch.setattr(settings, "confidence_check_enabled", False)
    rows = await _flags(graph_driver, domain_spec)

    assert set(rows) == {name for name, _, _ in DEPOTS}
    assert all("confidence_flag" not in row and "confidence_detail" not in row for row in rows.values())
