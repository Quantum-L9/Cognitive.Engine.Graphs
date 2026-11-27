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
from engine.handlers import ExecutionError, ValidationError, handle_outcomes, init_dependencies
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
    # #567's strict validation: a preference_attention dimension needs a traversal step
    # that binds `qe`, here the queryentity label the producer links outcomes from.
    raw["ontology"]["edges"].append(
        {
            "type": "CONSIDERED",
            "from": "LaunchWindow",
            "to": "OrbitalDepot",
            "direction": "DIRECTED",
            "category": "transaction",
            "managedby": "api",
        }
    )
    raw["traversal"] = {
        "steps": [
            {
                "name": "bind_query_window",
                "pattern": "(qe:LaunchWindow)-[:CONSIDERED]->(candidate:OrbitalDepot)",
                "required": False,
                "matchdirections": [DIRECTION],
            }
        ]
    }
    return raw


def _spec(raw: dict[str, Any] | None = None) -> DomainSpec:
    return DomainSpec.model_validate(raw if raw is not None else _raw())


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _Graph:
    """Records every statement, in or out of a transaction; keeps each outcome's bound query entity."""

    def __init__(self, linked: int = 1, fail_history: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.transactions = 0
        self.bound: dict[str, Any] = {}
        self.outcomes: dict[str, dict[str, Any]] = {}
        self._linked = linked
        self._fail_history = fail_history

    def _answer(self, cypher: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        if "CREATE (o:" in cypher:
            return [{"outcome_id": parameters["outcome_id"]}]
        if "MERGE (o:" in cypher:
            # The keyed writer: the first write creates, a later one replays the stored identity.
            created = parameters["outcome_id"] not in self.outcomes
            stored = self.outcomes.setdefault(parameters["outcome_id"], dict(parameters))
            row = {"outcome_id": parameters["outcome_id"], "created": created}
            return [
                row | {f"stored_{name}": stored.get(name) for name in ("match_id", "candidate_id", "outcome", "tenant")}
            ]
        if "o.query_entity_id AS prior" in cypher:
            prior = self.bound.get(parameters["outcome_id"])
            self.bound.setdefault(parameters["outcome_id"], parameters["query_entity_id"])
            return [{"prior": prior}]
        if "MERGE (qe)" in cypher:
            if self._fail_history:
                raise RuntimeError("history write lost its connection")
            return [{"linked": self._linked}]
        return []

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        parameters = parameters or {}
        self.calls.append((cypher, parameters, database))
        return self._answer(cypher, parameters)

    async def execute_write(self, work: Any, *, database: str | None = None) -> Any:
        self.transactions += 1
        graph = self

        class _Tx:
            async def run(self, cypher: str, parameters: dict[str, Any]) -> _Result:
                graph.calls.append((cypher, parameters, database))
                return _Result(graph._answer(cypher, parameters))

        # A refused or failed transaction rolls back: the binding and outcomes it wrote vanish.
        bound, outcomes = dict(self.bound), dict(self.outcomes)
        try:
            return await work(_Tx())
        except BaseException:
            self.bound, self.outcomes = bound, outcomes
            raise


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
    assert (
        "SET o.outcome_type = CASE WHEN $outcome = 'success' THEN $success_value ELSE $outcome END, "
        "o.community_id = coalesce(o.community_id, c.community_id)"
    ) in cypher
    assert parameters == {
        "outcome_id": result["outcome_id"],
        "outcome": "success",
        "candidate_id": "depot-1",
        "query_entity_id": "window-1",
        "success_value": "success",
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
    assert "o.chosen_community = coalesce(o.chosen_community, c.community_id)" in cypher


async def test_a_query_entity_that_is_not_in_the_graph_links_nothing() -> None:
    _wire(_spec(), _Graph(linked=0))
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


@pytest.mark.parametrize("bad", ["", True, 1.5, {"id": 1}, 2**63, -(2**63) - 1])
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


async def test_the_outcome_and_its_history_commit_in_one_transaction() -> None:
    graph = _wire(_spec(), _Graph())
    await handle_outcomes(TENANT, _payload())
    assert graph.transactions == 1
    statements = [c[0] for c in graph.calls]
    assert any("CREATE (o:" in c for c in statements)
    assert any("MERGE (qe)" in c for c in statements)


async def test_a_history_failure_fails_the_whole_write_so_a_plain_retry_is_a_first_write() -> None:
    graph = _wire(_spec(), _Graph(fail_history=True))
    with pytest.raises(ExecutionError, match="Outcome write failed"):
        await handle_outcomes(TENANT, _payload())
    # Everything ran in the one transaction that failed (a real driver rolls it back),
    # so no outcome is left committed without its history and nothing after it ran.
    assert graph.transactions == 1
    assert all(database == TENANT for _c, _p, database in graph.calls)


async def test_a_replay_naming_another_query_entity_is_refused() -> None:
    graph = _wire(_spec(), _Graph())
    await handle_outcomes(TENANT, _payload(outcome_id="out-1"))
    with pytest.raises(ValidationError, match="already recorded for another query entity"):
        await handle_outcomes(TENANT, _payload(outcome_id="out-1", query_entity_id="window-2"))
    # The unchanged replay is accepted and re-links the same entity.
    result = await handle_outcomes(TENANT, _payload(outcome_id="out-1"))
    assert result["preference_history"]["query_entity_id"] == "window-1"
    assert graph.bound == {"out-1": "window-1"}


async def test_no_applicable_dimension_needs_no_unambiguous_query_label() -> None:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    raw["domain"]["id"] = TENANT
    raw["matchentities"]["queryentity"].append({"label": "OrbitClass", "matchdirection": DIRECTION})
    graph = _wire(_spec(raw), _Graph())
    payload = _payload()
    payload["fingerprint"] = {}
    result = await handle_outcomes(TENANT, payload)
    assert result["status"] == "recorded"
    assert _history(graph) == []


async def test_with_declared_community_off_the_history_writes_the_property_the_reader_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "preference_attention_declared_community_enabled", False)
    graph = _wire(_spec(_raw(outcome_community_property="chosen_community")), _Graph())
    await handle_outcomes(TENANT, _payload())
    [(cypher, _params, _db)] = _history(graph)
    assert "o.community_id = coalesce(o.community_id, c.community_id)" in cypher


async def test_a_success_is_stored_as_the_declared_success_value() -> None:
    graph = _wire(_spec(_raw(success_value="approved")), _Graph())
    await handle_outcomes(TENANT, _payload())
    [(_cypher, parameters, _db)] = _history(graph)
    assert parameters["success_value"] == "approved"


@pytest.mark.parametrize("bad", ["failure", "partial"])
async def test_a_success_value_naming_a_non_success_is_refused_before_anything_is_written(bad: str) -> None:
    graph = _wire(_spec(_raw(success_value=bad)), _Graph())
    with pytest.raises(ValidationError, match="not a success"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


@pytest.mark.parametrize("reserved", ["outcome", "match_id", "candidate_id", "tenant", "outcome_id", "query_entity_id"])
async def test_a_success_property_naming_an_outcome_identity_field_is_refused_before_anything_is_written(
    reserved: str,
) -> None:
    graph = _wire(_spec(_raw(success_property=reserved)), _Graph())
    with pytest.raises(ValidationError, match="outcome identity field"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


async def test_a_community_property_naming_an_outcome_identity_field_is_refused_before_anything_is_written() -> None:
    graph = _wire(_spec(_raw(outcome_community_property="match_id")), _Graph())
    with pytest.raises(ValidationError, match="outcome identity field"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


def _with_second_dimension(raw: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    raw["scoring"]["dimensions"].append(
        {
            "name": "launch_preference_b",
            "source": "computed",
            "computation": "preference_attention",
            "candidateprop": "community_id",
            "weightkey": "w_preference_b",
            "defaultweight": 0.0,
            "metadata": {"success_value": "success", "success_property": "outcome_type", **metadata},
        }
    )
    return raw


async def test_two_dimensions_sharing_a_success_property_with_different_values_are_refused() -> None:
    graph = _wire(_spec(_with_second_dimension(_raw(), success_value="approved")), _Graph())
    with pytest.raises(ValidationError, match="different success_values"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


async def test_two_dimensions_sharing_a_success_property_and_value_both_link() -> None:
    graph = _wire(_spec(_with_second_dimension(_raw())), _Graph())
    response = await handle_outcomes(TENANT, _payload())
    assert len(_history(graph)) == 2
    assert response["preference_history"]["linked_dimensions"] == ["launch_preference", "launch_preference_b"]


@pytest.mark.parametrize("missing", ["", "   ", None])
async def test_a_missing_or_empty_success_value_is_refused_before_anything_is_written(missing: Any) -> None:
    raw = _raw()
    raw["scoring"]["dimensions"][-1]["metadata"]["success_value"] = missing
    if missing is None:
        del raw["scoring"]["dimensions"][-1]["metadata"]["success_value"]
    graph = _wire(_spec(raw), _Graph())
    with pytest.raises(ValidationError, match="no non-empty success_value"):
        await handle_outcomes(TENANT, _payload())
    assert graph.calls == []


async def test_a_replay_cannot_add_a_query_entity_to_an_outcome_recorded_without_one() -> None:
    graph = _wire(_spec(), _Graph())
    first = _payload(outcome_id="out-1")
    del first["query_entity_id"]
    await handle_outcomes(TENANT, first)
    assert graph.bound == {}

    with pytest.raises(ValidationError, match="recorded without a query entity"):
        await handle_outcomes(TENANT, _payload(outcome_id="out-1", query_entity_id="window-2"))
    assert graph.bound == {}
    assert _history(graph) == []

    # Replaying it as it was first written is still accepted and writes no history.
    result = await handle_outcomes(TENANT, first)
    assert "preference_history" not in result
    assert graph.bound == {}
    assert _history(graph) == []
