"""Detached match/improvement payload contracts (TASK-040), domain-neutral (Stage 2 R03).

The repository's example fixtures are PlasticOS-shaped and are admitted against
``PLASTICOS_EXAMPLE_CONTRACT``; the neutrality proofs below admit the same
shapes against two unrelated synthetic domains and show that every
contract-resolved field (domain, direction, entity type, outcome type, source
system) is accepted only when the authorized domain declares it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engine.models.payloads import (
    FORBIDDEN_TRANSPORT_FIELDS,
    CanonicalProjection,
    DomainContract,
    ImprovementProposal,
    MatchRequest,
    MatchResponse,
    OutcomeFeedback,
    SyncProjection,
    admit_payload,
)
from tests.fixtures.domain_contracts import (
    PLASTICOS_EXAMPLE_CONTRACT,
    SYNTHETIC_VOCABULARY,
    example_payload_for,
    synthetic_contract,
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

CTX = PLASTICOS_EXAMPLE_CONTRACT.context()


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


def test_schemas_pin_no_domain_and_no_domain_vocabulary() -> None:
    """The contract shape is domain-neutral: membership lives in the DomainContract, not the schema."""
    common = _load_yaml(PAYLOADS / "common.schema.yaml")
    assert "const" not in common["$defs"]["domain"]
    assert "enum" not in common["$defs"]["source_ref"]["properties"]["system"]
    for name in ("match-request", "match-response"):
        assert "enum" not in _load_yaml(PAYLOADS / f"{name}.schema.yaml")["properties"]["direction"]
    assert "enum" not in _load_yaml(PAYLOADS / "canonical-projection.schema.yaml")["properties"]["entity_type"]
    outcome_item = _load_yaml(PAYLOADS / "outcome-feedback.schema.yaml")["properties"]["outcomes"]["items"]
    assert "enum" not in outcome_item["properties"]["outcome_type"]


# ── PlasticOS example fixtures, admitted against the example contract ────────


def test_match_request_positive_example() -> None:
    MatchRequest.model_validate(_load_json(EXAMPLES / "match-request.json"), context=CTX)


def test_match_response_positive_example() -> None:
    MatchResponse.model_validate(_load_json(EXAMPLES / "match-response.json"), context=CTX)


def test_improvement_proposal_positive_example() -> None:
    proposal = ImprovementProposal.model_validate(_load_json(EXAMPLES / "improvement-proposal.json"), context=CTX)
    assert proposal.direct_mutation is False
    assert proposal.review_required is True


def test_match_request_rejects_transport_field() -> None:
    payload = _load_json(NEGATIVES / "match-request-transport-field.json")
    assert "packet_uuid" in payload
    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload, context=CTX)


def test_match_response_rejects_ineligible_ranked() -> None:
    payload = _load_json(NEGATIVES / "match-response-ineligible-ranked.json")
    with pytest.raises(ValidationError, match="ineligible"):
        MatchResponse.model_validate(payload, context=CTX)


def test_improvement_rejects_direct_mutation() -> None:
    payload = _load_json(NEGATIVES / "improvement-proposal-direct-mutation.json")
    assert payload["direct_mutation"] is True
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(payload, context=CTX)


@pytest.mark.parametrize("field", sorted(FORBIDDEN_TRANSPORT_FIELDS))
def test_match_request_forbids_each_transport_field(field: str) -> None:
    payload = _load_json(EXAMPLES / "match-request.json")
    payload[field] = "forbidden"
    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload, context=CTX)


def test_improvement_schema_locks_direct_mutation_const() -> None:
    schema = _load_yaml(PAYLOADS / "improvement-proposal.schema.yaml")
    assert schema["properties"]["direct_mutation"]["const"] is False
    assert schema["properties"]["review_required"]["const"] is True


def test_sync_projection_example() -> None:
    SyncProjection.model_validate(_load_json(EXAMPLES / "sync-projection.json"), context=CTX)


def test_canonical_projection_example() -> None:
    CanonicalProjection.model_validate(_load_json(EXAMPLES / "canonical-projection.json"), context=CTX)


def test_outcome_feedback_example() -> None:
    OutcomeFeedback.model_validate(_load_json(EXAMPLES / "outcome-feedback.json"), context=CTX)


# ── Domain neutrality proofs ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "example"),
    [
        (MatchRequest, "match-request.json"),
        (MatchResponse, "match-response.json"),
        (ImprovementProposal, "improvement-proposal.json"),
        (SyncProjection, "sync-projection.json"),
        (SyncProjection, "sync-projection-tombstone.json"),
        (CanonicalProjection, "canonical-projection.json"),
        (OutcomeFeedback, "outcome-feedback.json"),
    ],
)
def test_validation_without_a_domain_contract_fails_closed(model: type, example: str) -> None:
    with pytest.raises(ValidationError, match="requires a DomainContract"):
        model.model_validate(_load_json(EXAMPLES / example))


@pytest.mark.parametrize("domain_id", sorted(SYNTHETIC_VOCABULARY))
@pytest.mark.parametrize(
    ("model", "example"),
    [
        (MatchRequest, "match-request.json"),
        (MatchResponse, "match-response.json"),
        (ImprovementProposal, "improvement-proposal.json"),
        (SyncProjection, "sync-projection.json"),
        (SyncProjection, "sync-projection-tombstone.json"),
        (CanonicalProjection, "canonical-projection.json"),
        (OutcomeFeedback, "outcome-feedback.json"),
    ],
)
def test_unknown_but_authorized_domain_is_accepted(domain_id: str, model: type, example: str) -> None:
    """The same payload shapes admit against a domain the engine has never heard of."""
    contract = synthetic_contract(domain_id)
    admitted = admit_payload(model, example_payload_for(domain_id, example), contract)
    assert admitted.domain == domain_id


def test_wrong_domain_is_rejected() -> None:
    payload = example_payload_for("orbital-logistics", "match-request.json")
    with pytest.raises(ValidationError, match="not the admitted domain"):
        admit_payload(MatchRequest, payload, synthetic_contract("cheese-aging"))


def test_undeclared_match_direction_is_rejected() -> None:
    contract = synthetic_contract("orbital-logistics")
    payload = example_payload_for("orbital-logistics", "match-request.json")
    payload["direction"] = "batch_to_cave"  # valid for the other domain, not this one
    with pytest.raises(ValidationError, match="match direction 'batch_to_cave' is not declared"):
        admit_payload(MatchRequest, payload, contract)
    payload["direction"] = "supply_opportunity_to_buyer_facility"  # the former built-in literal
    with pytest.raises(ValidationError, match="is not declared"):
        admit_payload(MatchRequest, payload, contract)


def test_undeclared_entity_type_is_rejected() -> None:
    contract = synthetic_contract("cheese-aging")
    payload = example_payload_for("cheese-aging", "canonical-projection.json")
    payload["entity_type"] = "OrbitalDepot"
    with pytest.raises(ValidationError, match="entity_type 'OrbitalDepot' is not declared"):
        admit_payload(CanonicalProjection, payload, contract)
    payload["entity_type"] = "material_specification"  # the former built-in enum member
    with pytest.raises(ValidationError, match="is not declared"):
        admit_payload(CanonicalProjection, payload, contract)


def test_undeclared_outcome_type_is_rejected() -> None:
    contract = synthetic_contract("orbital-logistics")
    payload = example_payload_for("orbital-logistics", "outcome-feedback.json")
    payload["outcomes"][0]["outcome_type"] = "repeat_business"  # a PlasticOS signal
    with pytest.raises(ValidationError, match="outcome_type 'repeat_business' is not declared"):
        admit_payload(OutcomeFeedback, payload, contract)


def test_undeclared_source_system_is_rejected() -> None:
    contract = synthetic_contract("orbital-logistics")
    payload = example_payload_for("orbital-logistics", "sync-projection.json")
    payload["records"][0]["source_record"]["system"] = "odoo"
    with pytest.raises(ValidationError, match="source system 'odoo' is not declared"):
        admit_payload(SyncProjection, payload, contract)


def test_core_source_systems_are_always_admitted() -> None:
    contract = synthetic_contract("cheese-aging")
    payload = example_payload_for("cheese-aging", "sync-projection.json")
    payload["records"][0]["source_record"]["system"] = "operator"
    admit_payload(SyncProjection, payload, contract)


def test_contract_from_spec_matches_declared_vocabulary() -> None:
    contract = synthetic_contract("orbital-logistics")
    assert contract == DomainContract(
        domain_id="orbital-logistics",
        domain_version="0.1.0",
        match_directions=frozenset({"window_to_depot"}),
        entity_types=frozenset({"OrbitalDepot", "LaunchWindow", "PropellantGrade"}),
        source_systems=frozenset({"ceg", "operator", "external_source", "station_ledger"}),
        outcome_types=frozenset({"docked", "aborted"}),
    )


def test_declared_vocabulary_with_hyphens_or_dots_is_admitted() -> None:
    """DomainSpec puts no grammar on directions, signals or owners; the payload layer adds none either.

    Shape only (one token, no whitespace) — membership against the contract is
    the authoritative check, so a pack that declares ``window-to-depot`` must be
    usable through the typed contracts exactly as the loader accepted it.
    """
    base = synthetic_contract("orbital-logistics")
    contract = base.model_copy(update={"match_directions": base.match_directions | {"window-to-depot"}})
    payload = example_payload_for("orbital-logistics", "match-request.json")
    payload["direction"] = "window-to-depot"
    assert admit_payload(MatchRequest, payload, contract).direction == "window-to-depot"

    schema_pattern = yaml.safe_load((PAYLOADS / "common.schema.yaml").read_text(encoding="utf-8"))["$defs"][
        "identifier"
    ]["pattern"]
    from engine.models.payloads import IDENTIFIER_PATTERN

    assert schema_pattern == IDENTIFIER_PATTERN
    assert re.fullmatch(IDENTIFIER_PATTERN, "window-to-depot")
    assert re.fullmatch(IDENTIFIER_PATTERN, "Orbital.Depot")
    assert not re.fullmatch(IDENTIFIER_PATTERN, "two words")


# ── RG-015 (RU-012 / CEG-COR-063, R1a-DEC-04 "admit both") ───────────────────
# HANDLER_PAYLOADS.md names MatchRequest for the match action, but handle_match
# only read the legacy MatchPayload shape and rejected the example with
# "Missing required field match_direction". With
# match_request_contract_admission a payload carrying contract_version is
# admitted as MatchRequest against the tenant's DomainContract and mapped onto
# the legacy internals (direction, query.facts, top_n).


class _MatchDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, *, cypher: str, parameters: dict, database: str) -> list[dict]:
        self.calls.append((cypher, parameters))
        return [{"candidate": {"facility_id": "F-1"}, "score": 0.8, "dimension_scores": {}}]


def _plasticos_spec():  # type: ignore[no-untyped-def]
    from engine.config.schema import DomainSpec

    return DomainSpec.model_validate(yaml.safe_load((ROOT / "domains" / "plasticos" / "spec.yaml").read_text()))


def _wire_plasticos() -> _MatchDriver:
    from unittest.mock import MagicMock

    from engine.handlers import init_dependencies

    driver = _MatchDriver()
    loader = MagicMock()
    loader.load_domain.return_value = _plasticos_spec()
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


def _example_match_request() -> dict:
    return _load_json(EXAMPLES / "match-request.json")


@pytest.mark.finding("CEG-COR-063")
async def test_match_request_example_is_admitted_and_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """repro I/match_contract: the example returns candidates.

    The example's facts (polymer_code, form_code, quantity_lbs) are not the
    plasticos queryschema vocabulary; RU-010 rejects the missing required
    polymer_type, so the verbatim example is shown with strict_null_gates off.
    """
    from engine.config.settings import settings
    from engine.handlers import handle_match

    monkeypatch.setattr(settings, "strict_null_gates", False)
    driver = _wire_plasticos()
    payload = _example_match_request()
    result = await handle_match("plasticos", payload)

    assert result["candidates"]
    _cypher, params = driver.calls[0]
    assert params["polymer_code"] == "HDPE"  # query.facts became the query
    assert params["top_n"] == payload["top_n"]
    assert result["match_direction"] == payload["direction"]


@pytest.mark.finding("CEG-COR-063")
async def test_match_request_with_pack_vocabulary_returns_candidates() -> None:
    from engine.handlers import handle_match

    _wire_plasticos()
    payload = _example_match_request()
    payload["query"]["facts"] = {"polymer_type": "HDPE"}
    result = await handle_match("plasticos", payload)
    assert result["candidates"]


async def test_match_request_example_meets_required_parameter_check() -> None:
    """Default flags: admission succeeds; RU-010 then names the unmet required gate parameter."""
    from engine.handlers import ValidationError, handle_match

    _wire_plasticos()
    with pytest.raises(ValidationError, match="polymer_type"):
        await handle_match("plasticos", _example_match_request())


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda p: p.update(direction="buyer_to_seller"), "match direction"),
        (lambda p: p.update(domain="freight-matching"), "admitted domain"),
        (lambda p: p.update(packet_uuid="00000000-0000-0000-0000-000000000000"), "transport fields"),
        (lambda p: p.update(weights={"geo": 0.5}), "Extra inputs"),
    ],
    ids=["undeclared-direction", "other-domain", "transport-field", "extra-field"],
)
async def test_match_request_admission_failures_are_validation_errors(mutate, needle: str) -> None:  # type: ignore[no-untyped-def]
    from engine.handlers import ValidationError, handle_match

    driver = _wire_plasticos()
    payload = _example_match_request()
    mutate(payload)
    with pytest.raises(ValidationError, match=needle):
        await handle_match("plasticos", payload)
    assert driver.calls == []


async def test_legacy_match_payload_is_unaffected() -> None:
    from engine.handlers import handle_match

    driver = _wire_plasticos()
    result = await handle_match(
        "plasticos",
        {"query": {"polymer_type": "HDPE"}, "match_direction": "supply_opportunity_to_buyer_facility", "top_n": 3},
    )
    assert result["candidates"]
    assert len(driver.calls) == 1


async def test_match_request_admission_off_restores_the_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.handlers import ValidationError, handle_match

    monkeypatch.setattr(settings, "match_request_contract_admission", False)
    _wire_plasticos()
    with pytest.raises(ValidationError, match="match_direction"):
        await handle_match("plasticos", _example_match_request())
