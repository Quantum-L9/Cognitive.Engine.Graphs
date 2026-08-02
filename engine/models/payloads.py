"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [models]
tags: [payload, match, sync, improvement, transport-boundary]
owner: engine-team
status: active
--- /L9_META ---

Detached PlasticOS payload contracts (TASK-040).
Payloads ride inside TransportPacket; transport fields and direct mutation are forbidden.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORBIDDEN_TRANSPORT_FIELDS = frozenset(
    {
        "packet_uuid",
        "packet_type",
        "tenant_uuid",
        "source_context",
        "target_context",
        "correlation_id",
        "causation_id",
        "root_packet_uuid",
        "parent_packet_uuids",
        "generation",
        "trace",
        "authority",
        "payload_fingerprint",
        "tenant_id",
        "trace_id",
        "hop_trace",
        "transport_hash",
        "signature",
        "source_node",
        "destination_node",
    }
)

ENTITY_REF_PATTERN = r"^[a-z0-9_.-]+:[^\s]+$"
_ENTITY_REF = re.compile(ENTITY_REF_PATTERN)


class MatchDirection(StrEnum):
    SUPPLY_TO_BUYER = "supply_opportunity_to_buyer_facility"
    BUYER_TO_SUPPLY = "buyer_demand_to_supply_opportunity"


class FieldValueState(StrEnum):
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    STALE = "stale"
    CONFLICTING = "conflicting"
    SUPERSEDED = "superseded"


class ScoreScale(StrEnum):
    ZERO_TO_ONE = "0_to_1"
    ZERO_TO_HUNDRED = "0_to_100"
    UNNORMALIZED = "unnormalized_declared"


class ProposalType(StrEnum):
    SCHEMA_CHANGE = "schema_change"
    FIELD_MAPPING = "field_mapping"
    POLICY_CHANGE = "policy_change"
    QUALITY_RULE = "quality_rule"
    CAPABILITY_UPDATE = "capability_update"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


def _require_entity_refs(values: list[str]) -> list[str]:
    for value in values:
        if not _ENTITY_REF.match(value):
            raise ValueError(f"invalid entity_ref: {value}")
    return values


class PayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_transport_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            hit = FORBIDDEN_TRANSPORT_FIELDS.intersection(data)
            if hit:
                raise ValueError(f"transport fields forbidden on payload: {sorted(hit)}")
        return data


class SemverRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class EvidenceSummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: str
    value_state: FieldValueState
    evidence_ref: str = Field(pattern=ENTITY_REF_PATTERN)


class MatchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: dict[str, Any]
    evidence_summary: list[EvidenceSummaryItem]
    governed_filters: dict[str, Any] = Field(default_factory=dict)


class MatchRequest(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    query_id: str = Field(min_length=1)
    direction: MatchDirection
    query_entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    query: MatchQuery
    top_n: int = Field(ge=1, le=1000)
    projection_version: str = Field(min_length=1)
    policy_ref: SemverRef
    field_dictionary_version: str | None = Field(default=None, min_length=1)


class FailedGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_id: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def _refs(cls, values: list[str]) -> list[str]:
        return _require_entity_refs(values)


class FeatureContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: str
    contribution: float
    evidence_refs: list[str] = Field(default_factory=list)


class MatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    eligible: bool
    score: float | None
    score_scale: ScoreScale
    rank: int | None = Field(default=None, ge=1)
    failed_gates: list[FailedGate]
    feature_contributions: list[FeatureContribution]
    missing_evidence: list[str]
    explanation: str | None = None

    @model_validator(mode="after")
    def eligible_rank_rule(self) -> MatchCandidate:
        if not self.eligible and self.rank is not None:
            raise ValueError("ineligible candidates must not receive an eligible rank")
        return self


class MatchResponse(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    query_id: str = Field(min_length=1)
    direction: MatchDirection
    candidates: list[MatchCandidate]
    total_candidates: int | None = Field(default=None, ge=0)
    execution_time_ms: float = Field(ge=0)
    domain_spec_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    projection_version: str | None = Field(default=None, min_length=1)


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    operation: Literal["add", "replace", "deprecate"]
    rationale: str = Field(min_length=1)
    proposed_value: Any = None


class ImprovementProposal(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    proposal_id: str = Field(min_length=1)
    subject_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    proposal_type: ProposalType
    proposed_changes: list[ProposedChange] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    status: ProposalStatus
    direct_mutation: Literal[False] = False
    review_required: Literal[True] = True
    created_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @field_validator("evidence_refs")
    @classmethod
    def unique_refs(cls, values: list[str]) -> list[str]:
        _require_entity_refs(values)
        if len(values) != len(set(values)):
            raise ValueError("evidence_refs must be unique")
        return values


class SyncOperation(StrEnum):
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"


class SourceSystem(StrEnum):
    ODOO = "odoo"
    EIE = "eie"
    CEG = "ceg"
    OPERATOR = "operator"
    EXTERNAL_SOURCE = "external_source"


class ProjectionEntityType(StrEnum):
    MATERIAL_SPECIFICATION = "material_specification"
    MATERIAL_OBSERVATION = "material_observation"
    SUPPLY_OPPORTUNITY = "supply_opportunity"
    BUYER_DEMAND = "buyer_demand"
    FACILITY_MATERIAL_RULE = "facility_material_rule"
    CAPACITY_OBSERVATION = "capacity_observation"


class OutcomeType(StrEnum):
    REVIEWED = "reviewed"
    CONTACTED = "contacted"
    QUOTED = "quoted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    CLAIM_RAISED = "claim_raised"
    PAID = "paid"
    REPEAT_BUSINESS = "repeat_business"


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: SourceSystem
    record_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    revision: int | str = 0


class SyncProjectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    entity_type: str = Field(min_length=1)
    revision: int = Field(ge=0)
    operation: SyncOperation
    properties: dict[str, Any] = Field(default_factory=dict)
    relationship_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_record: SourceRecord
    field_dictionary_version: str = Field(min_length=1)

    @field_validator("relationship_refs", "evidence_refs")
    @classmethod
    def _entity_refs(cls, values: list[str]) -> list[str]:
        return _require_entity_refs(values)


class SyncProjection(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    projection_version: str = Field(min_length=1)
    records: list[SyncProjectionRecord] = Field(min_length=1, max_length=10000)


class SyncApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    projection_version: str
    store_revision_map: dict[str, int] = Field(default_factory=dict)


class CanonicalProjection(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    entity_type: ProjectionEntityType
    revision: int | str
    source_record: SourceRecord
    projection_version: str = Field(min_length=1)
    field_dictionary_version: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    relationship_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    generated_at: datetime
    tombstone: bool = False

    @field_validator("relationship_refs", "evidence_refs")
    @classmethod
    def _unique_entity_refs(cls, values: list[str]) -> list[str]:
        _require_entity_refs(values)
        if len(values) != len(set(values)):
            raise ValueError("entity refs must be unique")
        return values


class OutcomeFeedbackItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match_result_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    candidate_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    outcome_type: OutcomeType
    occurred_at: datetime
    source_record: SourceRecord
    idempotency_key: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class OutcomeFeedback(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    outcomes: list[OutcomeFeedbackItem] = Field(min_length=1)


class OutcomeApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)


class OutcomeFeedbackStore:
    """Idempotent outcome feedback applicator keyed by idempotency_key."""

    def __init__(self) -> None:
        self._by_key: dict[str, OutcomeFeedbackItem] = {}

    def get(self, idempotency_key: str) -> OutcomeFeedbackItem | None:
        return self._by_key.get(idempotency_key)

    def snapshot(self) -> dict[str, OutcomeFeedbackItem]:
        return dict(self._by_key)

    def apply(self, feedback: OutcomeFeedback) -> OutcomeApplyResult:
        accepted: list[str] = []
        reused: list[str] = []
        for item in feedback.outcomes:
            if item.idempotency_key in self._by_key:
                reused.append(item.idempotency_key)
                continue
            self._by_key[item.idempotency_key] = item
            accepted.append(item.idempotency_key)
        return OutcomeApplyResult(accepted=accepted, reused=reused)
