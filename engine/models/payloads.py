"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [models]
tags: [payload, match, improvement, transport-boundary]
owner: engine-team
status: active
--- /L9_META ---

Detached PlasticOS payload contracts (TASK-040).

These models describe business payloads carried inside Gate_SDK
TransportPacket. They deliberately exclude transport/envelope fields and
forbid direct production mutation via improvement proposals.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
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


class MatchDirection(str, Enum):
    SUPPLY_TO_BUYER = "supply_opportunity_to_buyer_facility"
    BUYER_TO_SUPPLY = "buyer_demand_to_supply_opportunity"


class FieldValueState(str, Enum):
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    STALE = "stale"
    CONFLICTING = "conflicting"
    SUPERSEDED = "superseded"


class ScoreScale(str, Enum):
    ZERO_TO_ONE = "0_to_1"
    ZERO_TO_HUNDRED = "0_to_100"
    UNNORMALIZED = "unnormalized_declared"


class ProposalType(str, Enum):
    SCHEMA_CHANGE = "schema_change"
    FIELD_MAPPING = "field_mapping"
    POLICY_CHANGE = "policy_change"
    QUALITY_RULE = "quality_rule"
    CAPABILITY_UPDATE = "capability_update"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class PayloadBase(BaseModel):
    """Payload root with transport-field rejection."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_transport_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
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
        import re

        pat = re.compile(ENTITY_REF_PATTERN)
        for value in values:
            if not pat.match(value):
                raise ValueError(f"invalid entity_ref: {value}")
        return values


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
        import re

        pat = re.compile(ENTITY_REF_PATTERN)
        for value in values:
            if not pat.match(value):
                raise ValueError(f"invalid entity_ref: {value}")
        if len(values) != len(set(values)):
            raise ValueError("evidence_refs must be unique")
        return values
