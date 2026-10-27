"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, outcomes, handlers]
status: active
--- /L9_META ---

CEG-FIX-001 — handle_outcomes write-path discipline for a rejected RESULTED_IN edge.

tests/integration/test_outcomes_causal_integrity.py proves the graph effect on a
real Neo4j. These unit tests cover what that cannot observe: the second,
CausalCompiler-driven RESULTED_IN write, whose MERGE is skipped for a rejected
edge, and the parameter that gates the edge in the primary outcome write.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.schema import CausalEdgeSpec, CausalSubgraphSpec
from engine.config.settings import settings
from engine.handlers import handle_outcomes
from engine.state import _reset_singleton, get_state

pytestmark = [pytest.mark.unit, pytest.mark.finding("CEG-FIX-001")]

_VALID = {"source_ts": "2026-03-01T00:00:00+00:00", "target_ts": "2026-03-02T00:00:00+00:00", "confidence": 0.9}
_LATE = {"source_ts": "2026-03-02T00:00:00+00:00", "target_ts": "2026-03-01T00:00:00+00:00", "confidence": 0.9}
_WEAK = {"source_ts": "2026-03-01T00:00:00+00:00", "target_ts": "2026-03-02T00:00:00+00:00", "confidence": 0.2}


def _spec(*, causal_edges: list[CausalEdgeSpec] | None = None) -> MagicMock:
    spec = MagicMock()
    spec.domain.id = "test_db"
    spec.feedbackloop.enabled = False
    # #318 made the outcome node label the domain's (`feedbackloop.outcome_node_label`,
    # schema default "TransactionOutcome") and passes it through sanitize_label(); a
    # bare MagicMock attribute is not a str and fails that regex. Same convention as
    # tests/unit/test_wave2_handlers.py.
    spec.feedbackloop.outcome_node_label = "TransactionOutcome"
    spec.counterfactual.enabled = False
    spec.compliance = None
    spec.causal = CausalSubgraphSpec(
        enabled=True,
        causal_edges=(
            causal_edges
            if causal_edges is not None
            else [
                CausalEdgeSpec(
                    edge_type="RESULTED_IN",
                    source_label="Facility",
                    target_label="TransactionOutcome",
                    temporal_validation=True,
                    confidence_threshold=0.5,
                ),
            ]
        ),
    )
    return spec


async def _run(payload_extra: dict[str, Any], spec: MagicMock) -> tuple[dict[str, Any], list[tuple[str, dict]]]:
    calls: list[tuple[str, dict]] = []

    async def _record(*, cypher: str, parameters: dict, database: str) -> list[dict]:
        calls.append((cypher, parameters))
        return [{"outcome_id": "out_x"}]

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_record)
    loader = MagicMock()
    loader.load_domain.return_value = spec

    _reset_singleton()
    state = get_state()
    state._graph_driver = driver
    state._domain_loader = loader
    state._initialized = True
    try:
        result = await handle_outcomes(
            "t1",
            {"match_id": "m1", "candidate_id": "c1", "outcome": "success", **payload_extra},
        )
    finally:
        _reset_singleton()
    return result, calls


def _outcome_write(calls: list[tuple[str, dict]]) -> dict:
    (params,) = [p for c, p in calls if "CREATE (o:TransactionOutcome" in c]
    return params


def _compiler_edge_writes(calls: list[tuple[str, dict]]) -> list[dict]:
    return [p for c, p in calls if "MERGE (source)-[r:RESULTED_IN" in c]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [_LATE, _WEAK], ids=["temporal", "confidence"])
async def test_rejected_edge_is_gated_out_of_both_write_paths(extra: dict[str, Any]) -> None:
    result, calls = await _run(extra, _spec())

    assert _outcome_write(calls)["create_resulted_in"] is False
    assert _compiler_edge_writes(calls) == []
    assert result == {"status": "recorded", "outcome_id": result["outcome_id"], "rejected_count": 1}


@pytest.mark.asyncio
async def test_undeclared_resulted_in_is_rejected_as_invalid_edge_type() -> None:
    result, calls = await _run(_VALID, _spec(causal_edges=[]))

    assert _outcome_write(calls)["create_resulted_in"] is False
    assert result["rejected_count"] == 1


@pytest.mark.asyncio
async def test_valid_edge_takes_both_write_paths() -> None:
    result, calls = await _run(_VALID, _spec())

    assert _outcome_write(calls)["create_resulted_in"] is True
    assert len(_compiler_edge_writes(calls)) == 1
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_missing_causal_fields_stay_lenient() -> None:
    """Validator contract: absent timestamps and confidence do not reject."""
    result, calls = await _run({}, _spec())

    assert _outcome_write(calls)["create_resulted_in"] is True
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_mixed_offset_timestamps_record_the_outcome() -> None:
    """Uncomparable timestamps stay lenient instead of failing the request."""
    mixed = {"source_ts": "2026-03-02T00:00:00+00:00", "target_ts": "2026-03-01T00:00:00", "confidence": 0.9}
    result, calls = await _run(mixed, _spec())

    assert result["status"] == "recorded"
    assert _outcome_write(calls)["create_resulted_in"] is True


@pytest.mark.asyncio
async def test_flag_off_keeps_log_only_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "causal_rejection_enforcement_enabled", False)
    result, calls = await _run(_LATE, _spec())

    assert _outcome_write(calls)["create_resulted_in"] is True
    assert len(_compiler_edge_writes(calls)) == 1
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_causal_disabled_never_consults_the_validator() -> None:
    spec = _spec()
    spec.causal = CausalSubgraphSpec(enabled=False)
    result, calls = await _run(_LATE, spec)

    assert _outcome_write(calls)["create_resulted_in"] is True
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_compiler_edge_uses_declared_identities_and_bound_threshold() -> None:
    """RU-068 / CEG-COR-057: the causal edge matches the candidate by its declared id
    and the outcome by `outcome_id`, under one WHERE with a bound threshold."""
    spec = _spec()
    endpoint = MagicMock()
    endpoint.targetnode = "Facility"
    endpoint.idproperty = "facility_id"
    spec.sync.endpoints = [endpoint]

    _result, calls = await _run(_VALID, spec)

    ((cypher, params),) = [(c, p) for c, p in calls if "MERGE (source)-[r:RESULTED_IN" in c]
    assert "MATCH (source:Facility {facility_id: $source_id})" in cypher
    assert "MATCH (target:TransactionOutcome {outcome_id: $target_id})" in cypher
    assert cypher.count("WHERE") == 1
    assert params["confidence_threshold"] == 0.5
    assert params["source_id"] == "c1"
