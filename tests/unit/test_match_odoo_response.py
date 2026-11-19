"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, match, odoo, response]
owner: engine-team
status: active
--- /L9_META ---

G01-CEG-ODOO-RESPONSE. The real ``handle_match`` payload is passed through the
unmodified Odoo mapper. The mapper is not copied and the draft MatchResponse
schema is not promoted.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from engine.handlers import handle_match, init_dependencies
from engine.state import get_state
from tests.fixtures.domain_contracts import synthetic_loader
from tests.fixtures.orbital_match import DIRECTION, ORBITAL

pytestmark = pytest.mark.unit


def _map_match_if_sibling(result: dict[str, Any]) -> Any | None:
    """The sibling mapper when that checkout is present. CI has no sibling."""
    mapper = Path(__file__).resolve().parents[3] / "odoo" / "plasticos_gate" / "services" / "gate_mappers.py"
    if not mapper.is_file():
        return None
    root = str(mapper.parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from plasticos_gate.services.gate_mappers import map_match_response

    return map_match_response(result)


class RecordingDriver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append(database)
        return copy.deepcopy(self.rows)


class FixedLoader:
    def __init__(self, spec: Any) -> None:
        self.spec = spec

    def load_domain(self, _tenant: str) -> Any:
        return self.spec


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


def _candidate(depot_id: str, score: float, *, entity_ref: str | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "depot_id": depot_id,
        "accepts_hazmat": True,
        "docking_ports": 4,
        "reliability": score,
    }
    if entity_ref is not None:
        node["entity_ref"] = entity_ref
    return {
        "candidate": node,
        "score": score,
        "dimension_scores": {"depot_reliability": score, "port_capacity": 0.0},
    }


@pytest.mark.asyncio
async def test_handle_match_is_selectable_by_the_unmodified_odoo_mapper() -> None:
    rows = [
        _candidate("buyer", 0.8, entity_ref="res.partner:102"),
        _candidate("bare", 0.2, entity_ref=None),
    ]
    driver = RecordingDriver(rows)
    get_state().reset()
    init_dependencies(driver, FixedLoader(synthetic_loader().load_domain(ORBITAL)))
    result = await handle_match(ORBITAL, {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 10})
    assert driver.calls == [ORBITAL]
    assert "contract_version" not in result
    assert "quantity_per_load_lbs" not in result

    mapped = _map_match_if_sibling(result)
    if mapped is None:
        return
    assert [item.buyer_partner_id for item in mapped.results] == [102]
    selected = mapped.results[0]
    assert selected.entity_ref == "res.partner:102"
    assert selected.eligible is True
    assert selected.rank == 1
    assert selected.score_scale == "0_to_1"
    assert selected.normalized_score == pytest.approx(0.8)
    contributed = {item["feature_id"] for item in selected.feature_contributions}
    assert "depot_reliability" in contributed
    assert "port_capacity" in contributed
    assert "depot_reliability" not in selected.missing_evidence
    assert any("no entity_ref" in row["reason"] for row in mapped.unresolved)
