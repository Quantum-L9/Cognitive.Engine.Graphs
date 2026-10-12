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

Detached, domain-neutral payload contracts (TASK-040, Stage 2 R03).
Payloads ride inside TransportPacket; transport fields and direct mutation are forbidden.

Every payload root carries a ``domain`` and is admitted only against a
``DomainContract`` derived from the authorized, versioned domain spec
(``DomainContract.from_spec``). The domain id, eligible match directions,
projection entity types, outcome types and source systems a payload may name
resolve against that contract — never against a literal baked into the engine
and never against an unrestricted caller string. Validation without a contract
in the Pydantic validation context fails closed.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

if TYPE_CHECKING:
    from engine.config.schema import DomainSpec

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
# Domain ids are directory names under the domains root (DomainPackLoader).
DOMAIN_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# Match directions, entity types, outcome types and source systems are
# identifiers the domain spec declares; structural shape only — membership is
# checked against the DomainContract.
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

DOMAIN_CONTRACT_CONTEXT_KEY = "domain_contract"

# Source systems every CEG deployment knows about regardless of domain: the
# engine itself, a human operator, and an otherwise-unidentified external
# source. A domain adds its own systems through feature_catalog owners.
CORE_SOURCE_SYSTEMS = frozenset({"ceg", "operator", "external_source"})


class DomainContract(BaseModel):
    """The slice of an authorized domain spec that payload admission resolves against.

    Built from ``DomainSpec`` (``from_spec``) so every value traces back to a
    versioned, validated pack. Tests and verification harnesses may construct
    one directly for a synthetic domain; production callers must derive it from
    the loaded spec.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain_id: str = Field(pattern=DOMAIN_ID_PATTERN)
    domain_version: str = Field(min_length=1)
    match_directions: frozenset[str]
    entity_types: frozenset[str]
    source_systems: frozenset[str]
    outcome_types: frozenset[str]

    @classmethod
    def from_spec(cls, spec: DomainSpec) -> DomainContract:
        """Derive the admission contract from a validated domain spec.

        * ``match_directions`` — ``queryschema.matchdirections``.
        * ``entity_types`` — ontology node labels plus sync endpoint target
          nodes: a projection may only describe an entity the domain's graph
          can hold.
        * ``source_systems`` — ``CORE_SOURCE_SYSTEMS`` plus every
          ``feature_catalog[].owner`` the domain declares.
        * ``outcome_types`` — ``outcome_signals[].signal``.
        """
        entity_types: set[str] = {node.label for node in spec.ontology.nodes}
        if spec.sync is not None:
            entity_types.update(ep.targetnode for ep in spec.sync.endpoints if ep.targetnode)
        return cls(
            domain_id=spec.domain.id,
            domain_version=spec.domain.version,
            match_directions=frozenset(spec.queryschema.matchdirections),
            entity_types=frozenset(entity_types),
            source_systems=frozenset(CORE_SOURCE_SYSTEMS | {entry.owner for entry in spec.feature_catalog}),
            outcome_types=frozenset(signal.signal for signal in spec.outcome_signals),
        )

    def context(self) -> dict[str, Any]:
        """Pydantic validation context binding this contract."""
        return {DOMAIN_CONTRACT_CONTEXT_KEY: self}

    # --- membership checks (raise ValueError so Pydantic reports them) ---

    def require_domain(self, domain: str) -> None:
        if domain != self.domain_id:
            raise ValueError(f"payload domain {domain!r} is not the admitted domain {self.domain_id!r}")

    def require_direction(self, direction: str) -> None:
        if direction not in self.match_directions:
            raise ValueError(
                f"match direction {direction!r} is not declared by domain {self.domain_id!r} "
                f"(declared: {sorted(self.match_directions)})"
            )

    def require_entity_type(self, entity_type: str) -> None:
        if entity_type not in self.entity_types:
            raise ValueError(
                f"entity_type {entity_type!r} is not declared by domain {self.domain_id!r} "
                f"(declared: {sorted(self.entity_types)})"
            )

    def require_source_system(self, system: str) -> None:
        if system not in self.source_systems:
            raise ValueError(
                f"source system {system!r} is not declared by domain {self.domain_id!r} "
                f"(declared: {sorted(self.source_systems)})"
            )

    def require_outcome_type(self, outcome_type: str) -> None:
        if outcome_type not in self.outcome_types:
            raise ValueError(
                f"outcome_type {outcome_type!r} is not declared by domain {self.domain_id!r} "
                f"(declared: {sorted(self.outcome_types)})"
            )


def contract_from_context(info: ValidationInfo) -> DomainContract:
    """Return the bound DomainContract or fail closed."""
    context = info.context or {}
    contract = context.get(DOMAIN_CONTRACT_CONTEXT_KEY)
    if not isinstance(contract, DomainContract):
        raise ValueError(
            "payload admission requires a DomainContract: validate with "
            "model_validate(data, context=contract.context()) or admit_payload(...)"
        )
    return contract


def admit_payload[PayloadT: BaseModel](model: type[PayloadT], data: Any, contract: DomainContract) -> PayloadT:
    """Validate ``data`` as ``model`` against ``contract``."""
    return model.model_validate(data, context=contract.context())


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
    """Root of every payload: forbids transport fields and binds the domain contract."""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(pattern=DOMAIN_ID_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def reject_transport_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            hit = FORBIDDEN_TRANSPORT_FIELDS.intersection(data)
            if hit:
                raise ValueError(f"transport fields forbidden on payload: {sorted(hit)}")
        return data

    @model_validator(mode="after")
    def bind_domain_contract(self, info: ValidationInfo) -> Self:
        contract = contract_from_context(info)
        contract.require_domain(self.domain)
        self._admit(contract)
        return self

    def _admit(self, contract: DomainContract) -> None:
        """Payload-specific membership checks; overridden where a payload names domain vocabulary."""


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
    query_id: str = Field(min_length=1)
    direction: str = Field(pattern=IDENTIFIER_PATTERN)
    query_entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    query: MatchQuery
    top_n: int = Field(ge=1, le=1000)
    projection_version: str = Field(min_length=1)
    policy_ref: SemverRef
    field_dictionary_version: str | None = Field(default=None, min_length=1)

    def _admit(self, contract: DomainContract) -> None:
        contract.require_direction(self.direction)


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
    query_id: str = Field(min_length=1)
    direction: str = Field(pattern=IDENTIFIER_PATTERN)
    candidates: list[MatchCandidate]
    total_candidates: int | None = Field(default=None, ge=0)
    execution_time_ms: float = Field(ge=0)
    domain_spec_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    projection_version: str | None = Field(default=None, min_length=1)

    def _admit(self, contract: DomainContract) -> None:
        contract.require_direction(self.direction)


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    operation: Literal["add", "replace", "deprecate"]
    rationale: str = Field(min_length=1)
    proposed_value: Any = None


class ImprovementProposal(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
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


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: str = Field(pattern=IDENTIFIER_PATTERN)
    record_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    revision: int | str = 0


class SyncProjectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    entity_type: str = Field(pattern=IDENTIFIER_PATTERN)
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
    projection_version: str = Field(min_length=1)
    records: list[SyncProjectionRecord] = Field(min_length=1, max_length=10000)

    def _admit(self, contract: DomainContract) -> None:
        for record in self.records:
            contract.require_entity_type(record.entity_type)
            contract.require_source_system(record.source_record.system)


class SyncApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    projection_version: str
    store_revision_map: dict[str, int] = Field(default_factory=dict)


class CanonicalProjection(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    entity_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    entity_type: str = Field(pattern=IDENTIFIER_PATTERN)
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

    def _admit(self, contract: DomainContract) -> None:
        contract.require_entity_type(self.entity_type)
        contract.require_source_system(self.source_record.system)


class OutcomeFeedbackItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match_result_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    candidate_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    outcome_type: str = Field(pattern=IDENTIFIER_PATTERN)
    occurred_at: datetime
    source_record: SourceRecord
    idempotency_key: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class OutcomeFeedback(PayloadBase):
    contract_version: Literal["1.0.0-draft"]
    outcomes: list[OutcomeFeedbackItem] = Field(min_length=1)

    def _admit(self, contract: DomainContract) -> None:
        for item in self.outcomes:
            contract.require_outcome_type(item.outcome_type)
            contract.require_source_system(item.source_record.system)


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
