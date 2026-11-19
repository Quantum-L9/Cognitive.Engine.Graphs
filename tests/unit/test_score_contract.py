"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, match, score, contract]
owner: engine-team
status: active
--- /L9_META ---

G04-SCORE-CONTRACT. The subject is the payload ``handle_match`` returns and the
payload ``handle_score`` / ``handle_score_remote`` return.

The draft match-response schema stays unpublished. Its example fixture is a
diagnostic of that file, not evidence that the live payload implements it.
The Odoo mapper is imported unmodified. Match quantity comes from the frozen
approved supply-opportunity payload.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from engine.handlers import (
    FeatureNotEnabled,
    ValidationError,
    handle_match,
    handle_score,
    handle_score_remote,
    init_dependencies,
)
from engine.scoring.evidence_priority import EvidenceObservation, EvidenceRead, approved_relevance_map
from engine.state import get_state
from tests.fixtures.domain_contracts import synthetic_loader
from tests.fixtures.match_response_contract import (
    UNPUBLISHED_REQUIRED,
    diagnostic_request_quantity_lbs,
    example_response_matches_draft_schema,
    frozen_opportunity_quantity,
    map_match_if_sibling,
    schema_messages,
    schema_text,
)
from tests.fixtures.orbital_match import DIRECTION, ORBITAL

pytestmark = pytest.mark.unit

_REL = "SYNTHETIC_POSITIVE_REL"
_SCORED_REF = "hubspot.company:101"
_ABSTAIN_REF = "hubspot.company:202"
_ZERO_CONFIDENCE_REF = "hubspot.company:303"


class RecordingDriver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []
        self.parameters: dict[str, Any] = {}

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append(database)
        self.parameters = parameters
        return copy.deepcopy(self.rows)


class FixedLoader:
    def __init__(self, spec: Any) -> None:
        self.spec = spec

    def load_domain(self, _tenant: str) -> Any:
        return self.spec


class _ScoreBinding:
    def __init__(self) -> None:
        self.reads: list[tuple[str, tuple[str, ...]]] = []

    @property
    def approved_relevance(self) -> Mapping[str, float]:
        return approved_relevance_map({_REL: 1.0})

    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        self.reads.append((database, tuple(entity_refs)))
        observations = (
            EvidenceObservation(
                entity_ref=_SCORED_REF,
                evidence_ref="evidence:a1",
                relationship_type=_REL,
                source_ref="source:evidence:a1",
                confidence=0.8,
                admission="admitted",
                polarity="positive",
            ),
            EvidenceObservation(
                entity_ref=_ZERO_CONFIDENCE_REF,
                evidence_ref="evidence:zero",
                relationship_type=_REL,
                source_ref="source:evidence:zero",
                confidence=0.0,
                admission="admitted",
                polarity="positive",
            ),
        )
        kept = tuple(item for item in observations if item.entity_ref in entity_refs)
        return EvidenceRead(connected=False, database=database, observations=kept)


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


def _row(depot_id: str, score: Any, entity_ref: str | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "depot_id": depot_id,
        "accepts_hazmat": True,
        "docking_ports": 4,
        "reliability": 0.8 if isinstance(score, (int, float)) and math.isfinite(score) else 0.0,
    }
    if entity_ref is not None:
        node["entity_ref"] = entity_ref
    return {
        "candidate": node,
        "score": score,
        "dimension_scores": {"depot_reliability": 0.8, "port_capacity": 0.0},
    }


def _publication_status() -> str:
    return schema_text().split("x-publication-status:", 1)[1].splitlines()[0].strip()


def _assert_draft_stays_unpublished(messages: list[str]) -> None:
    assert _publication_status() == "draft-unpublished"
    assert example_response_matches_draft_schema() is True
    for name in UNPUBLISHED_REQUIRED:
        assert f"'{name}' is a required property" in messages
    assert "'direction' is a required property" not in messages
    assert "'failed_gates' is a required property" not in messages
    assert any(message.startswith("Additional properties are not allowed") for message in messages)
    assert "'entity_ref' is a required property" in messages


@pytest.mark.asyncio
async def test_handle_match_payload_against_draft_schema_and_odoo_mapper() -> None:
    quantity_lbs = diagnostic_request_quantity_lbs()
    frozen = frozen_opportunity_quantity()
    if frozen is not None:
        assert frozen["unit"] == "lb"
        assert frozen["target"] == quantity_lbs
    query = {"min_ports": 1, "quantity_lbs": quantity_lbs}
    assert "volume_tons" not in query
    assert "capacity_tons_month" not in query
    assert "quantity_per_load_lbs" not in query
    rows = [
        _row("buyer", 0.8, "res.partner:102"),
        _row("bare", 0.2, None),
        _row("missing", None, "res.partner:7"),
        _row("nan", float("nan"), "res.partner:8"),
    ]
    driver = RecordingDriver(rows)
    init_dependencies(driver, FixedLoader(synthetic_loader().load_domain(ORBITAL)))
    result = await handle_match(ORBITAL, {"query": query, "match_direction": DIRECTION, "top_n": 10})

    assert driver.calls == [ORBITAL]
    assert driver.parameters["quantity_lbs"] == quantity_lbs
    assert result["status"] == "success"
    assert result["match_direction"] == DIRECTION
    assert result["direction"] == DIRECTION
    for name in UNPUBLISHED_REQUIRED:
        assert name not in result
    assert "quantity_per_load_lbs" not in result
    by_ref = {row.get("entity_ref"): row for row in result["candidates"]}
    assert by_ref["res.partner:102"]["eligible"] is True
    assert by_ref["res.partner:102"]["rank"] == 1
    assert by_ref["res.partner:102"]["score_scale"] == "0_to_1"
    assert by_ref["res.partner:7"]["score"] is None
    assert by_ref["res.partner:7"]["eligible"] is False
    assert by_ref["res.partner:7"]["rank"] is None
    assert by_ref["res.partner:8"]["score"] is None
    assert by_ref["res.partner:8"]["eligible"] is False
    for row in result["candidates"]:
        assert row["failed_gates"] == []
        score = row["score"]
        assert score is None or (isinstance(score, float) and math.isfinite(score))

    _assert_draft_stays_unpublished(schema_messages(result))
    mapped = map_match_if_sibling(result)
    if mapped is None:
        return
    assert mapped.match_direction == DIRECTION
    assert mapped.contract_version is None
    assert mapped.domain is None
    assert [item.buyer_partner_id for item in mapped.results] == [102]
    selected = mapped.results[0]
    assert selected.entity_ref == "res.partner:102"
    assert selected.eligible is True
    assert selected.score_scale == "0_to_1"
    assert selected.normalized_score == pytest.approx(0.8)
    assert selected.rank == 1
    assert selected.gates_failed == []
    unresolved_refs = {row["entity_ref"] for row in mapped.unresolved}
    assert {"res.partner:7", "res.partner:8"} <= unresolved_refs
    assert _publication_status() == "draft-unpublished"


@pytest.mark.asyncio
async def test_score_action_contract_default_off_scale_identity_and_evidence() -> None:
    from engine.config.settings import Settings

    assert Settings().remote_score_enabled is False
    driver = RecordingDriver([])
    init_dependencies(driver, FixedLoader(synthetic_loader().load_domain(ORBITAL)))
    with pytest.raises(FeatureNotEnabled, match="REMOTE_SCORE_ENABLED"):
        await handle_score_remote(ORBITAL, {"entity_refs": [_SCORED_REF], "top_n": 1})
    assert driver.calls == []

    binding = _ScoreBinding()
    with pytest.raises(ValidationError, match="refusing"):
        await handle_score(ORBITAL, {"entity_refs": [_SCORED_REF], "top_n": 1, "score": 99}, binding=binding)
    assert binding.reads == []

    result = await handle_score(
        ORBITAL,
        {"entity_refs": [_SCORED_REF, _ABSTAIN_REF, _ZERO_CONFIDENCE_REF], "top_n": 3},
        binding=binding,
    )
    assert result["status"] == "ok"
    assert result["policy_version"] == "graph_evidence_priority_v1"
    assert "probability" not in result
    assert binding.reads == [(ORBITAL, (_SCORED_REF, _ABSTAIN_REF, _ZERO_CONFIDENCE_REF))]
    assert [item["entity_ref"] for item in result["scored"]] == [_SCORED_REF]
    scored = result["scored"][0]
    assert scored["score"] == pytest.approx(80.0)
    assert 0.0 <= scored["score"] <= 100.0
    assert scored["rank"] == 1
    assert scored["factors"][0]["evidence_refs"] == ["evidence:a1"]
    abstained = {item["entity_ref"]: item for item in result["abstained"]}
    assert abstained[_ABSTAIN_REF]["score"] is None
    assert abstained[_ZERO_CONFIDENCE_REF]["score"] is None
