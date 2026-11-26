"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, outcomes, scoring, feedback]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_outcome_preference_history.py
L8 item 3, producer side: ``handle_outcomes`` writes the outcome history in the
shape ``preference_attention`` reads — ``(qe)-[outcome_relation]->(o)`` with the
outcome under the dimension's success property and the candidate's community
on the outcome node — when the payload names ``query_entity_id``. Statement
shape and refusals without Neo4j.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.handlers import ValidationError, handle_outcomes, init_dependencies
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

TENANT = "orbital-preference"
DIRECTION = "window_to_depot"


def _raw(**metadata: Any) -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    raw["domain"]["id"] = TENANT
    nodes = {n["label"]: n for n in raw["ontology"]["nodes"]}
    nodes["OrbitalDepot"]["properties"].append({"name": "community_id", "type": "int"})
    raw["scoring"]["dimensions"].append(
        {
            "name": "launch_preference",
            "source": "computed",
            "computation": "preference_attention",
            "candidateprop": "community_id",
            "weightkey": "w_preference",
            "defaultweight": 0.1,
            "metadata": {"success_value": "success", "success_property": "outcome_type", **metadata},
        }
    )
    return raw


def _spec(raw: dict[str, Any] | None = None) -> DomainSpec:
    return DomainSpec.model_validate(raw if raw is not None else _raw())


class _Graph:
    """Records every statement; answers the outcome write and the history link."""

    def __init__(self, linked: int = 1) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self._linked = linked

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        parameters = parameters or {}
        self.calls.append((cypher, parameters, database))
        if "CREATE (o:" in cypher:
            return [{"outcome_id": parameters["outcome_id"]}]
        if "MERGE (qe)" in cypher:
            return [{"linked": self._linked}]
        return []


def _wire(spec: DomainSpec, graph: _Graph) -> _Graph:
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [spec.domain.id]
    init_dependencies(graph, loader)  # type: ignore[arg-type]
    return graph


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "outcome_preference_history_enabled", True)
    monkeypatch.setattr(settings, "feedback_enabled", False)
    monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "match_id": "m-1",
        "candidate_id": "depot-1",
        "outcome": "success",
        "fingerprint": {"match_direction": DIRECTION},
        "query_entity_id": "window-1",
        **extra,
    }


def _history(graph: _Graph) -> list[tuple[str, dict[str, Any], str | None]]:
    return [call for call in graph.calls if "MERGE (qe)" in call[0]]


async def test_the_outcome_is_linked_from_the_query_entity_in_the_read_shape() -> None:
    graph = _wire(_spec(), _Graph())
    result = await handle_outcomes(TENANT, _payload())

    [(cypher, parameters, database)] = _history(graph)
    assert database == TENANT
    assert "MATCH (o:DockingResult {outcome_id: $outcome_id})" in cypher
    assert "MATCH (qe:LaunchWindow) WHERE qe.window_id = $query_entity_id" in cypher
    assert "(c:OrbitalDepot AND c.depot_id = $candidate_id)" in cypher
    assert "MERGE (qe)-[:RESULTED_IN]->(o)" in cypher
    assert "SET o.outcome_type = $outcome, o.community_id = c.community_id" in cypher
    assert parameters == {
        "outcome_id": result["outcome_id"],
        "outcome": "success",
        "candidate_id": "depot-1",
        "query_entity_id": "window-1",
    }
    assert result["preference_history"] == {"query_entity_id": "window-1", "linked_dimensions": ["launch_preference"]}
    # The outcome node itself is written first, unchanged.
    assert graph.calls.index(_history(graph)[0]) > next(i for i, c in enumerate(graph.calls) if "CREATE (o:" in c[0])


async def test_declared_metadata_names_the_relation_and_both_community_properties() -> None:
    raw = _raw(outcome_relation="CHOSE", outcome_community_property="chosen_community")
    raw["ontology"]["edges"].append(
        {
            "type": "CHOSE",
            "from": "LaunchWindow",
            "to": "OrbitalDepot",
            "direction": "DIRECTED",
            "category": "transaction",
            "managedby": "api",
        }
    )
    graph = _wire(_spec(raw), _Graph())
    await handle_outcomes(TENANT, _payload())
    [(cypher, _params, _db)] = _history(graph)
    assert "MERGE (qe)-[:CHOSE]->(o)" in cypher
    assert "o.chosen_community = c.community_id" in cypher


async def test_a_query_entity_that_is_not_in_the_graph_links_nothing() -> None:
    graph = _wire(_spec(), _Graph(linked=0))
    result = await handle_outcomes(TENANT, _payload())
    assert result["preference_history"]["linked_dimensions"] == []


async def test_no_query_entity_id_writes_no_history() -> None:
    graph = _wire(_spec(), _Graph())
    payload = _payload()
    del payload["query_entity_id"]
    result = await handle_outcomes(TENANT, payload)
    assert _history(graph) == []
    assert "preference_history" not in result


async def test_a_pack_without_preference_attention_writes_no_history() -> None:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    raw["domain"]["id"] = TENANT
    graph = _wire(_spec(raw), _Graph())
    result = await handle_outcomes(TENANT, _payload())
    assert _history(graph) == []
    assert "preference_history" not in result


@pytest.mark.parametrize("bad", ["", True, 1.5, {"id": 1}])
async def test_a_malformed_query_entity_id_is_refused_before_anything_is_written(bad: Any) -> None:
    graph = _wire(_spec(), _Graph())
    with pytest.raises(ValidationError, match="query_entity_id"):
        await handle_outcomes(TENANT, _payload(query_entity_id=bad))
    assert graph.calls == []


async def test_an_outcome_node_the_writer_does_not_write_is_refused_before_anything_is_written() -> None:
    graph = _wire(_spec(_raw(outcome_node="LaunchResult")), _Graph())
    with pytest.raises(ValidationError, match="reads outcome node 'LaunchResult'"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


async def test_an_ambiguous_query_entity_label_is_refused() -> None:
    raw = copy.deepcopy(_raw())
    raw["matchentities"]["queryentity"].append({"label": "OrbitClass", "matchdirection": DIRECTION})
    graph = _wire(_spec(raw), _Graph())
    with pytest.raises(ValidationError, match="one query entity label"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


async def test_flag_off_the_query_entity_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "outcome_preference_history_enabled", False)
    graph = _wire(_spec(), _Graph())
    result = await handle_outcomes(TENANT, _payload())
    assert _history(graph) == []
    assert "preference_history" not in result
