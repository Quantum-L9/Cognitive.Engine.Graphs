"""IdeaOS projection hydration and portfolio-query compilation for CEG."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
DOMAIN_ID = "idea-portfolio"
_STATE_ID = "canonical"
_MODEL_CONFIG = ConfigDict(extra="forbid", populate_by_name=True)


class IdeaPortfolioHydrationError(ValueError):
    """Raised when portfolio hydration cannot be safely admitted or applied."""


class EvidenceState(StrEnum):
    VERIFIED = "VERIFIED"
    SUPPORTED_INFERENCE = "SUPPORTED_INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"


class AssertionKind(StrEnum):
    CAPABILITY = "capability"
    SUBSTRATE = "substrate"
    PROOF_ASSET = "proof_asset"
    DATA_ASSET = "data_asset"
    MARKET = "market"
    CUSTOMER_TYPE = "customer_type"
    DEPENDENCY = "dependency"


class AssertionRelation(StrEnum):
    PRODUCES = "produces"
    REQUIRES = "requires"
    TARGETS = "targets"
    USES = "uses"
    DEPENDS_ON = "depends_on"


_ALLOWED_RELATIONS: dict[AssertionKind, frozenset[AssertionRelation]] = {
    kind: frozenset({AssertionRelation.PRODUCES, AssertionRelation.REQUIRES, AssertionRelation.USES})
    for kind in (
        AssertionKind.CAPABILITY,
        AssertionKind.SUBSTRATE,
        AssertionKind.PROOF_ASSET,
        AssertionKind.DATA_ASSET,
    )
}
_ALLOWED_RELATIONS.update(
    {
        AssertionKind.MARKET: frozenset({AssertionRelation.TARGETS}),
        AssertionKind.CUSTOMER_TYPE: frozenset({AssertionRelation.TARGETS}),
        AssertionKind.DEPENDENCY: frozenset({AssertionRelation.DEPENDS_ON}),
    }
)
_RANK_ELIGIBLE = frozenset({EvidenceState.VERIFIED, EvidenceState.SUPPORTED_INFERENCE})


class IdeaLifecycle(BaseModel):
    model_config = _MODEL_CONFIG
    stage: str = Field(min_length=1)
    decision: Literal["GO", "CONDITIONAL_GO", "HOLD", "NO_GO"] | None = None
    proof_state: str | None = None
    execution_state: str | None = None


class IdeaAssertion(BaseModel):
    model_config = _MODEL_CONFIG
    kind: AssertionKind
    relation: AssertionRelation
    key: str = Field(min_length=1)
    evidence_state: EvidenceState
    source_refs: list[str]

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if self.relation not in _ALLOWED_RELATIONS[self.kind]:
            raise ValueError(f"relation {self.relation.value!r} is not valid for assertion kind {self.kind.value!r}")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("assertion source_refs must be unique")
        if self.evidence_state != EvidenceState.UNKNOWN and not self.source_refs:
            raise ValueError("non-UNKNOWN assertions require at least one source_ref")
        return self


class IdeaGraphProjection(BaseModel):
    """CEG admission model for the IdeaOS idea-graph-projection/v1 wire contract."""

    model_config = _MODEL_CONFIG
    schema_id: Literal["ideaos.idea-graph-projection/v1"] = Field(alias="schema")
    idea_id: str = Field(min_length=1)
    source_refs: list[str]
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    lifecycle: IdeaLifecycle
    assertions: list[IdeaAssertion]
    unknowns: list[str]

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if not self.source_refs:
            raise ValueError("CEG hydration requires at least one projection source_ref")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("projection source_refs must be unique")
        if len(self.unknowns) != len(set(self.unknowns)):
            raise ValueError("projection unknowns must be unique")
        keys = [(a.kind.value, a.relation.value, _canonical_key(a.key)) for a in self.assertions]
        if len(keys) != len(set(keys)):
            raise ValueError("projection contains duplicate semantic assertions")
        return self


class IdeaPortfolioSyncRecord(BaseModel):
    model_config = _MODEL_CONFIG
    schema_id: Literal["ceg.idea-portfolio-sync-record/v1"] = Field(alias="schema")
    operation: Literal["upsert", "tombstone"]
    projection: IdeaGraphProjection | None = None
    idea_id: str | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.operation == "upsert":
            if self.projection is None:
                raise ValueError("upsert sync record requires projection")
            if self.idea_id is not None and self.idea_id != self.projection.idea_id:
                raise ValueError("sync record idea_id does not match projection idea_id")
        elif self.idea_id is None or self.projection is not None:
            raise ValueError("tombstone requires idea_id and forbids projection")
        return self

    @property
    def resolved_idea_id(self) -> str:
        if self.projection is not None:
            return self.projection.idea_id
        if self.idea_id is None:
            raise IdeaPortfolioHydrationError("validated tombstone lacks idea_id")
        return self.idea_id


class IdeaPortfolioHydrationEnvelope(BaseModel):
    model_config = _MODEL_CONFIG
    schema_id: Literal["ceg.idea-portfolio-hydration/v1"] = Field(alias="schema")
    source_snapshot_ref: str = Field(min_length=1)
    source_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_graph_revision: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    records: list[IdeaPortfolioSyncRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        idea_ids = [record.resolved_idea_id for record in self.records]
        if len(idea_ids) != len(set(idea_ids)):
            raise ValueError("hydration envelope may contain at most one record per idea_id")
        return self


class GraphWriter(Protocol):
    async def execute_write(
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | Any: ...


@dataclass(frozen=True)
class CompiledAssertion:
    assertion_id: str
    facet_id: str
    kind: str
    key: str
    relation: str
    evidence_state: str
    source_refs_json: str


@dataclass(frozen=True)
class HydrationPlan:
    envelope: IdeaPortfolioHydrationEnvelope
    batch_digest: str
    graph_revision: str


@dataclass(frozen=True)
class WriteCommand:
    cypher: str
    parameters: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _canonical_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _facet_id(kind: AssertionKind | str, key: str) -> str:
    kind_value = kind.value if isinstance(kind, AssertionKind) else kind
    return "facet:" + hashlib.sha256(f"{kind_value}\x00{_canonical_key(key)}".encode()).hexdigest()


def _assertion_id(idea_id: str, assertion: IdeaAssertion) -> str:
    value = f"{idea_id}\x00{assertion.relation.value}\x00{assertion.kind.value}\x00{_canonical_key(assertion.key)}"
    return "assertion:" + hashlib.sha256(value.encode()).hexdigest()


def projection_digest(projection: IdeaGraphProjection) -> str:
    return _sha256_text(_canonical_json(projection.model_dump(mode="json", by_alias=True)))


def compile_assertions(projection: IdeaGraphProjection) -> list[CompiledAssertion]:
    return [
        CompiledAssertion(
            assertion_id=_assertion_id(projection.idea_id, assertion),
            facet_id=_facet_id(assertion.kind, assertion.key),
            kind=assertion.kind.value,
            key=_canonical_key(assertion.key),
            relation=assertion.relation.value,
            evidence_state=assertion.evidence_state.value,
            source_refs_json=_canonical_json(sorted(assertion.source_refs)),
        )
        for assertion in projection.assertions
    ]


def build_portfolio_match_query(projection: IdeaGraphProjection | dict[str, Any]) -> dict[str, Any]:
    """Compile only source-backed rank-eligible assertions into match input."""
    model = projection if isinstance(projection, IdeaGraphProjection) else IdeaGraphProjection.model_validate(projection)
    by_relation = {relation.value: [] for relation in AssertionRelation}
    for raw, compiled in zip(model.assertions, compile_assertions(model), strict=True):
        if raw.evidence_state in _RANK_ELIGIBLE and raw.source_refs:
            by_relation[compiled.relation].append(compiled.facet_id)

    def ids(relation: AssertionRelation) -> list[str]:
        return sorted(set(by_relation[relation.value]))

    def encoded(relation: AssertionRelation) -> str:
        values = ids(relation)
        return "" if not values else "|" + "|".join(values) + "|"

    return {
        "idea_id": model.idea_id,
        "requires_facets": encoded(AssertionRelation.REQUIRES),
        "requires_count": len(ids(AssertionRelation.REQUIRES)),
        "produces_facets": encoded(AssertionRelation.PRODUCES),
        "produces_count": len(ids(AssertionRelation.PRODUCES)),
        "uses_facets": encoded(AssertionRelation.USES),
        "uses_count": len(ids(AssertionRelation.USES)),
        "targets_facets": encoded(AssertionRelation.TARGETS),
        "targets_count": len(ids(AssertionRelation.TARGETS)),
        "depends_on_facets": encoded(AssertionRelation.DEPENDS_ON),
        "self_dependency_facet_id": _facet_id(AssertionKind.DEPENDENCY, model.idea_id),
    }


def compile_hydration_plan(envelope: IdeaPortfolioHydrationEnvelope | dict[str, Any]) -> HydrationPlan:
    model = envelope if isinstance(envelope, IdeaPortfolioHydrationEnvelope) else IdeaPortfolioHydrationEnvelope.model_validate(envelope)
    payload = [record.model_dump(mode="json", by_alias=True) for record in model.records]
    batch_digest = _sha256_text(_canonical_json(payload))
    parent = model.expected_graph_revision or "GENESIS"
    revision = _sha256_text(f"ceg.idea-portfolio-graph/v1\x00{parent}\x00{model.source_snapshot_digest}\x00{batch_digest}")
    return HydrationPlan(model, batch_digest, revision)


_UPSERT_CYPHER = """
MERGE (idea:Idea {idea_id: $idea_id})
SET idea.source_digest=$source_digest, idea.projection_digest=$projection_digest,
    idea.graph_revision=$graph_revision, idea.lifecycle_stage=$lifecycle_stage,
    idea.decision=$decision, idea.proof_state=$proof_state, idea.execution_state=$execution_state,
    idea.unknowns_json=$unknowns_json, idea.self_dependency_facet_id=$self_dependency_facet_id,
    idea.active=true, idea.hydrated_at=datetime(), idea.tombstoned_at=null, idea._tenant=$tenant
WITH idea
OPTIONAL MATCH (idea)-[old:PRODUCES|REQUIRES|TARGETS|USES|DEPENDS_ON]->(:PortfolioFacet)
DELETE old
WITH DISTINCT idea
FOREACH (row IN $produces |
  MERGE (facet:PortfolioFacet {facet_id: row.facet_id})
  SET facet.kind=row.kind, facet.key=row.key, facet.last_seen_revision=$graph_revision, facet._tenant=$tenant
  MERGE (idea)-[rel:PRODUCES]->(facet)
  SET rel.assertion_id=row.assertion_id, rel.kind=row.kind, rel.evidence_state=row.evidence_state,
      rel.source_refs_json=row.source_refs_json, rel.projection_digest=$projection_digest, rel.graph_revision=$graph_revision)
FOREACH (row IN $requires |
  MERGE (facet:PortfolioFacet {facet_id: row.facet_id})
  SET facet.kind=row.kind, facet.key=row.key, facet.last_seen_revision=$graph_revision, facet._tenant=$tenant
  MERGE (idea)-[rel:REQUIRES]->(facet)
  SET rel.assertion_id=row.assertion_id, rel.kind=row.kind, rel.evidence_state=row.evidence_state,
      rel.source_refs_json=row.source_refs_json, rel.projection_digest=$projection_digest, rel.graph_revision=$graph_revision)
FOREACH (row IN $targets |
  MERGE (facet:PortfolioFacet {facet_id: row.facet_id})
  SET facet.kind=row.kind, facet.key=row.key, facet.last_seen_revision=$graph_revision, facet._tenant=$tenant
  MERGE (idea)-[rel:TARGETS]->(facet)
  SET rel.assertion_id=row.assertion_id, rel.kind=row.kind, rel.evidence_state=row.evidence_state,
      rel.source_refs_json=row.source_refs_json, rel.projection_digest=$projection_digest, rel.graph_revision=$graph_revision)
FOREACH (row IN $uses |
  MERGE (facet:PortfolioFacet {facet_id: row.facet_id})
  SET facet.kind=row.kind, facet.key=row.key, facet.last_seen_revision=$graph_revision, facet._tenant=$tenant
  MERGE (idea)-[rel:USES]->(facet)
  SET rel.assertion_id=row.assertion_id, rel.kind=row.kind, rel.evidence_state=row.evidence_state,
      rel.source_refs_json=row.source_refs_json, rel.projection_digest=$projection_digest, rel.graph_revision=$graph_revision)
FOREACH (row IN $depends_on |
  MERGE (facet:PortfolioFacet {facet_id: row.facet_id})
  SET facet.kind=row.kind, facet.key=row.key, facet.last_seen_revision=$graph_revision, facet._tenant=$tenant
  MERGE (idea)-[rel:DEPENDS_ON]->(facet)
  SET rel.assertion_id=row.assertion_id, rel.kind=row.kind, rel.evidence_state=row.evidence_state,
      rel.source_refs_json=row.source_refs_json, rel.projection_digest=$projection_digest, rel.graph_revision=$graph_revision)
RETURN idea.idea_id AS idea_id,
       size($produces)+size($requires)+size($targets)+size($uses)+size($depends_on) AS assertion_count
""".strip()


def compile_upsert_command(projection: IdeaGraphProjection, *, graph_revision: str) -> WriteCommand:
    grouped = {relation.value: [] for relation in AssertionRelation}
    for assertion in compile_assertions(projection):
        grouped[assertion.relation].append(
            {
                "assertion_id": assertion.assertion_id,
                "facet_id": assertion.facet_id,
                "kind": assertion.kind,
                "key": assertion.key,
                "evidence_state": assertion.evidence_state,
                "source_refs_json": assertion.source_refs_json,
            }
        )
    return WriteCommand(
        _UPSERT_CYPHER,
        {
            "tenant": DOMAIN_ID,
            "idea_id": projection.idea_id,
            "source_digest": projection.source_digest,
            "projection_digest": projection_digest(projection),
            "graph_revision": graph_revision,
            "lifecycle_stage": projection.lifecycle.stage,
            "decision": projection.lifecycle.decision,
            "proof_state": projection.lifecycle.proof_state,
            "execution_state": projection.lifecycle.execution_state,
            "unknowns_json": _canonical_json(sorted(projection.unknowns)),
            "self_dependency_facet_id": _facet_id(AssertionKind.DEPENDENCY, projection.idea_id),
            **grouped,
        },
    )


def compile_tombstone_command(idea_id: str, *, graph_revision: str) -> WriteCommand:
    return WriteCommand(
        """MERGE (idea:Idea {idea_id: $idea_id})
SET idea.active=false, idea.graph_revision=$graph_revision, idea.tombstoned_at=datetime(), idea._tenant=$tenant
WITH idea OPTIONAL MATCH (idea)-[old:PRODUCES|REQUIRES|TARGETS|USES|DEPENDS_ON]->(:PortfolioFacet)
DELETE old RETURN idea.idea_id AS idea_id""",
        {"tenant": DOMAIN_ID, "idea_id": idea_id, "graph_revision": graph_revision},
    )


_LOCK_STATE_CYPHER = """MERGE (state:IdeaPortfolioHydrationState {state_id: $state_id})
SET state._cas_lock=coalesce(state._cas_lock, 0)+1, state._tenant=$tenant
RETURN state.current_revision AS current_revision"""
_FINALIZE_STATE_CYPHER = """MATCH (state:IdeaPortfolioHydrationState {state_id: $state_id})
SET state.current_revision=$graph_revision, state.source_snapshot_ref=$source_snapshot_ref,
    state.source_snapshot_digest=$source_snapshot_digest, state.batch_digest=$batch_digest,
    state.completed_at=datetime(), state._tenant=$tenant
RETURN state.current_revision AS graph_revision"""


class IdeaPortfolioHydrator:
    """Apply one revision-chained corpus delta in one managed Neo4j transaction."""

    def __init__(self, graph_writer: GraphWriter, *, enabled: bool = False) -> None:
        self.graph_writer = graph_writer
        self.enabled = enabled

    async def apply(self, envelope: IdeaPortfolioHydrationEnvelope | dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise IdeaPortfolioHydrationError("idea-portfolio hydration is disabled by configuration")
        plan = compile_hydration_plan(envelope)

        async def apply_transaction(tx: Any) -> dict[str, Any]:
            state = await tx.run(_LOCK_STATE_CYPHER, {"state_id": _STATE_ID, "tenant": DOMAIN_ID})
            rows = await state.data()
            current = rows[0].get("current_revision") if rows else None
            if current == plan.graph_revision:
                return self._receipt(plan, "reused", [], [])
            if current != plan.envelope.expected_graph_revision:
                raise IdeaPortfolioHydrationError("hydration revision conflict: expected parent does not match committed graph revision")

            applied: list[str] = []
            tombstoned: list[str] = []
            for record in plan.envelope.records:
                if record.operation == "upsert":
                    if record.projection is None:
                        raise IdeaPortfolioHydrationError("validated upsert lacks projection")
                    command = compile_upsert_command(record.projection, graph_revision=plan.graph_revision)
                    result = await tx.run(command.cypher, command.parameters)
                    await result.consume()
                    applied.append(record.projection.idea_id)
                else:
                    command = compile_tombstone_command(record.resolved_idea_id, graph_revision=plan.graph_revision)
                    result = await tx.run(command.cypher, command.parameters)
                    await result.consume()
                    tombstoned.append(record.resolved_idea_id)

            final = await tx.run(
                _FINALIZE_STATE_CYPHER,
                {
                    "state_id": _STATE_ID,
                    "tenant": DOMAIN_ID,
                    "graph_revision": plan.graph_revision,
                    "source_snapshot_ref": plan.envelope.source_snapshot_ref,
                    "source_snapshot_digest": plan.envelope.source_snapshot_digest,
                    "batch_digest": plan.batch_digest,
                },
            )
            await final.consume()
            return self._receipt(plan, "applied", applied, tombstoned)

        result = await self.graph_writer.execute_write(apply_transaction, database=DOMAIN_ID)
        if not isinstance(result, dict):
            raise IdeaPortfolioHydrationError("graph writer returned an invalid hydration receipt")
        return result

    @staticmethod
    def _receipt(
        plan: HydrationPlan,
        status: Literal["applied", "reused"],
        applied: list[str],
        tombstoned: list[str],
    ) -> dict[str, Any]:
        return {
            "schema": "ceg.idea-portfolio-hydration-receipt/v1",
            "status": status,
            "graph_revision": plan.graph_revision,
            "parent_graph_revision": plan.envelope.expected_graph_revision,
            "batch_digest": plan.batch_digest,
            "source_snapshot_ref": plan.envelope.source_snapshot_ref,
            "source_snapshot_digest": plan.envelope.source_snapshot_digest,
            "applied": applied,
            "tombstoned": tombstoned,
        }


__all__ = [
    "DOMAIN_ID",
    "AssertionKind",
    "AssertionRelation",
    "EvidenceState",
    "HydrationPlan",
    "IdeaGraphProjection",
    "IdeaPortfolioHydrationEnvelope",
    "IdeaPortfolioHydrationError",
    "IdeaPortfolioHydrator",
    "IdeaPortfolioSyncRecord",
    "WriteCommand",
    "build_portfolio_match_query",
    "compile_assertions",
    "compile_hydration_plan",
    "compile_tombstone_command",
    "compile_upsert_command",
    "projection_digest",
]
