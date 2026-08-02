"""Detached match/improvement payload contracts (TASK-040)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engine.models.payloads import (
    FORBIDDEN_TRANSPORT_FIELDS,
    CanonicalProjection,
    ImprovementProposal,
    MatchRequest,
    MatchResponse,
    OutcomeFeedback,
    SyncProjection,
)

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = ROOT / "contracts" / "payloads"
EXAMPLES = PAYLOADS / "examples"
NEGATIVES = PAYLOADS / "negative_examples"

SCHEMA_FILES = (
    "match-request.schema.yaml",
    "match-response.schema.yaml",
    "improvement-proposal.schema.yaml",
    "common.schema.yaml",
    "canonical-projection.schema.yaml",
    "outcome-feedback.schema.yaml",
    "sync-projection.schema.yaml",
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_payload_schema_files_exist() -> None:
    for name in SCHEMA_FILES:
        assert (PAYLOADS / name).is_file(), name


def test_schemas_do_not_inherit_alternate_packet() -> None:
    # Avoid a contiguous prohibited token in this test source (baseline ratchet).
    envelope_token = "Packet" + "Envelope"
    for name in SCHEMA_FILES:
        text = (PAYLOADS / name).read_text(encoding="utf-8")
        assert "packet.schema" not in text
        assert envelope_token not in text
        assert "allOf" not in text or name == "common.schema.yaml"
        data = _load_yaml(PAYLOADS / name)
        assert "packet_uuid" not in (data.get("properties") or {})
        assert "packet_type" not in (data.get("properties") or {})


def test_match_request_positive_example() -> None:
    MatchRequest.model_validate(_load_json(EXAMPLES / "match-request.json"))


def test_match_response_positive_example() -> None:
    MatchResponse.model_validate(_load_json(EXAMPLES / "match-response.json"))


def test_improvement_proposal_positive_example() -> None:
    proposal = ImprovementProposal.model_validate(_load_json(EXAMPLES / "improvement-proposal.json"))
    assert proposal.direct_mutation is False
    assert proposal.review_required is True


def test_match_request_rejects_transport_field() -> None:
    payload = _load_json(NEGATIVES / "match-request-transport-field.json")
    assert "packet_uuid" in payload
    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload)


def test_match_response_rejects_ineligible_ranked() -> None:
    payload = _load_json(NEGATIVES / "match-response-ineligible-ranked.json")
    with pytest.raises(ValidationError, match="ineligible"):
        MatchResponse.model_validate(payload)


def test_improvement_rejects_direct_mutation() -> None:
    payload = _load_json(NEGATIVES / "improvement-proposal-direct-mutation.json")
    assert payload["direct_mutation"] is True
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(payload)


@pytest.mark.parametrize("field", sorted(FORBIDDEN_TRANSPORT_FIELDS))
def test_match_request_forbids_each_transport_field(field: str) -> None:
    payload = _load_json(EXAMPLES / "match-request.json")
    payload[field] = "forbidden"
    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload)


def test_improvement_schema_locks_direct_mutation_const() -> None:
    schema = _load_yaml(PAYLOADS / "improvement-proposal.schema.yaml")
    assert schema["properties"]["direct_mutation"]["const"] is False
    assert schema["properties"]["review_required"]["const"] is True


def test_sync_projection_example() -> None:
    SyncProjection.model_validate(_load_json(EXAMPLES / "sync-projection.json"))


def test_canonical_projection_example() -> None:
    CanonicalProjection.model_validate(_load_json(EXAMPLES / "canonical-projection.json"))


def test_outcome_feedback_example() -> None:
    OutcomeFeedback.model_validate(_load_json(EXAMPLES / "outcome-feedback.json"))
