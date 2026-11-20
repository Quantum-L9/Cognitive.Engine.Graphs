"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, match, identity]
owner: engine-team
status: active
--- /L9_META ---

G01-CEG-ODOO-RESPONSE for PlasticOS. ``handle_match`` projects a stored
namespaced id and the unmodified Odoo mapper decides which of those ids it can
select. An absent sibling checkout does not pass this test.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest

from engine.config.loader import DomainPackLoader
from engine.handlers import handle_match, init_dependencies
from engine.state import get_state
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT
from tests.fixtures.match_response_contract import require_unmodified_odoo_mapper

pytestmark = pytest.mark.integration

SELECTABLE = "res.partner:102"
MISSING = "site-missing"
FOREIGN = "facility:foreign"
DIRECTION = "supply_opportunity_to_buyer_facility"


class RecordingDriver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append(database)
        if "DimensionWeight" in cypher:
            return []
        return copy.deepcopy(self.rows)


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


def _row(facility_id: str, score: float) -> dict[str, Any]:
    return {
        "candidate": {
            "facility_id": facility_id,
            "name": facility_id,
            "contamination_tolerance": 5.0,
        },
        "score": score,
        "dimension_scores": {"contamination_tolerance_score": 0.95},
        "absent_sources": [],
        "failed_gates": [],
    }


@pytest.mark.asyncio
async def test_plasticos_match_projects_source_identity_and_odoo_selects_only_the_partner() -> None:
    rows = [_row(SELECTABLE, 0.8), _row(MISSING, 0.5), _row(FOREIGN, 0.4)]
    driver = RecordingDriver(rows)
    init_dependencies(driver, DomainPackLoader(str(DESIGN_PACKS_ROOT)))
    result = await handle_match(
        "plasticos",
        {
            "query": {
                "polymer_type": "HDPE",
                "density": 0.95,
                "mfi": 8.0,
                "contamination_pct": 1.0,
                "requires_food_grade": False,
                "volume_tons": 18.0,
                "lat": 40.0,
                "lon": -75.0,
                "target_price": 1000.0,
                "community_id": 7,
                "form": "PALLETS",
                "color": "NATURAL",
                "material_grade": "A",
                "quantity_per_load_lbs": 40000,
            },
            "match_direction": DIRECTION,
            "top_n": 10,
        },
    )
    assert driver.calls
    by_id = {row["candidate"]["facility_id"]: row for row in result["candidates"]}
    assert by_id[SELECTABLE]["entity_ref"] == SELECTABLE
    assert "entity_ref" not in by_id[MISSING]
    assert by_id[FOREIGN]["entity_ref"] == FOREIGN
    assert by_id[SELECTABLE]["eligible"] is True

    mapped = require_unmodified_odoo_mapper(result)
    assert [item.entity_ref for item in mapped.results] == [SELECTABLE]
    assert mapped.results[0].buyer_partner_id == 102
    unresolved = {row["entity_ref"] for row in mapped.unresolved}
    assert None in unresolved
    assert FOREIGN in unresolved
    assert SELECTABLE not in unresolved
