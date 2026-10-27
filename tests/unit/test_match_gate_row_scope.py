"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, match, gates, traversal]
owner: engine-team
status: active
--- /L9_META ---

RU-011 / CEG-COR-033: hard gates filter candidate rows, not an OPTIONAL MATCH.

In Cypher a ``WHERE`` that directly follows ``OPTIONAL MATCH`` belongs to that
OPTIONAL MATCH: a failing predicate nulls the optional variables and keeps the
row, so a gate-failing candidate survived whenever the last traversal step was
``required: false``. ``WITH *`` between the traversal and the gates makes the
gates a row filter while keeping every bound variable in scope.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import DomainSpec, TraversalStepSpec
from engine.config.settings import settings
from engine.handlers import handle_match, init_dependencies
from tests.fixtures.domain_contracts import synthetic_loader

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"


class _CapturingDriver:
    def __init__(self) -> None:
        self.cyphers: list[str] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.cyphers.append(cypher)
        return []


def _spec(*, last_required: bool) -> DomainSpec:
    spec = synthetic_loader().load_domain(DOMAIN)
    steps = [
        TraversalStepSpec(
            name="stocks_grade",
            pattern="(candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)",
            required=last_required,
        )
    ]
    return spec.model_copy(update={"traversal": spec.traversal.model_copy(update={"steps": steps})})


async def _cypher(spec: DomainSpec) -> str:
    driver = _CapturingDriver()
    loader = MagicMock()
    loader.load_domain.return_value = spec
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION})
    return driver.cyphers[0]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-033")
class TestGateRowScope:
    async def test_with_star_precedes_where_after_optional_last_step(self) -> None:
        cypher = await _cypher(_spec(last_required=False))
        assert "OPTIONAL MATCH (candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)\nWITH *\nWHERE " in cypher

    async def test_with_star_precedes_where_after_required_last_step(self) -> None:
        cypher = await _cypher(_spec(last_required=True))
        assert "\nMATCH (candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)\nWITH *\nWHERE " in cypher

    async def test_gates_still_precede_scoring(self) -> None:
        cypher = await _cypher(_spec(last_required=False))
        assert cypher.index("WITH *\nWHERE ") < cypher.index("WITH candidate,")

    async def test_flag_off_restores_where_after_traversal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gate_where_row_scope", False)
        cypher = await _cypher(_spec(last_required=False))
        assert "WITH *" not in cypher
        assert "OPTIONAL MATCH (candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)\nWHERE " in cypher
