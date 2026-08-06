"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, contract, payload, match, fixtures]
owner: engine-team
status: active
--- /L9_META ---

Conformance tests for the campaign-named canonical match fixtures (TASK-005).

``contracts/match_request.json`` and ``contracts/match_response.json`` must
validate against the CEG-owned MatchRequest / MatchResponse models, and the
conformance check must reject a fixture with a renamed/removed required field
(e.g. a candidate missing ``entity_ref`` or a response missing ``query_id``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.models.payloads import MatchRequest, MatchResponse

_CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts"


def _load(name: str) -> dict:
    return json.loads((_CONTRACTS_ROOT / name).read_text(encoding="utf-8"))


def test_match_request_fixture_validates() -> None:
    model = MatchRequest.model_validate(_load("match_request.json"))
    assert model.query_id == "odoo-match-71-1"
    assert model.query_entity_ref == "res.partner:71"


def test_match_response_fixture_validates() -> None:
    model = MatchResponse.model_validate(_load("match_response.json"))
    assert model.query_id == "odoo-match-71-1"
    assert model.candidates[0].entity_ref == "res.partner:102"


def test_response_rejects_candidate_missing_entity_ref() -> None:
    payload = _load("match_response.json")
    del payload["candidates"][0]["entity_ref"]
    with pytest.raises(ValidationError):
        MatchResponse.model_validate(payload)


def test_response_rejects_missing_query_id() -> None:
    payload = _load("match_response.json")
    del payload["query_id"]
    with pytest.raises(ValidationError):
        MatchResponse.model_validate(payload)


def test_request_rejects_missing_query_entity_ref() -> None:
    payload = _load("match_request.json")
    del payload["query_entity_ref"]
    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload)
