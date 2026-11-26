"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [scoring, evidence, graph, reader, poc]
owner: engine-team
status: active
--- /L9_META ---

Neo4j-backed ``EvidenceBinding`` for ``graph_evidence_priority_v1`` (QM.POC0.CEG.GRAPH_EVIDENCE.002A).

Reads, for the requested entity refs, the relationship evidence that relationship
sync (``engine/sync/generator.py``) recorded: only edges stamped with
``RELATIONSHIP_EVIDENCE_SCHEMA`` and the tenant, so a link that merely exists
(taxonomy, child sync, GDS, anything written another way) is never evidence.
The scored entity is the relationship's source node, identified by its declared
stable id (``_subject_ref``).

Reads go through ``GraphDriver.execute_read``: a managed READ transaction that
never provisions a database. A missing database, an unconnected driver or a
graph outage (connection loss, session expiry, timeout, open circuit) raises
``EvidenceUnavailableError`` so the local handler answers ``blocked``; a wrong
query or a programming error is not an outage and propagates.

CEG admission of a raw assertion: only ``assertion_status == "observed"`` is
``admitted``; ``reported`` and ``inferred`` stay ``inferred_unverified`` and never
qualify. An edge whose ``expires_at`` has passed is stale. Commercial relevance
is the server-owned map this binding is constructed with, never the source's.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired, TransientError
from pydantic import ValidationError as PydanticValidationError

from engine.config.schema import DomainSpec
from engine.config.tenant_mapping import relevance_is_active
from engine.graph.circuit_breaker import CircuitOpenError
from engine.graph.driver import DatabaseNotProvisionedError, GraphDriver
from engine.scoring.evidence_priority import (
    MAX_COHORT_SIZE,
    EvidenceObservation,
    EvidenceRead,
    EvidenceUnavailableError,
    MalformedEvidence,
    approved_relevance_map,
)
from engine.sync.generator import RELATIONSHIP_EVIDENCE_SCHEMA
from engine.utils.security import sanitize_label

if TYPE_CHECKING:
    from engine.config.schema import DomainSpec

logger = logging.getLogger(__name__)

# Bounded read: more evidence edges than this for one cohort is refused rather
# than silently truncated into a different score.
MAX_EVIDENCE_EDGES = 1000

_READ_EVIDENCE = """
MATCH ()-[r{types}]->()
WHERE r._evidence_schema = $schema AND r._tenant = $tenant AND r._subject_ref IN $refs
RETURN r._subject_ref AS entity_ref, type(r) AS relationship_type, properties(r) AS props
ORDER BY entity_ref, props.evidence_ref
LIMIT $limit
"""


def approved_relevance_from_spec(domain_spec: DomainSpec) -> Mapping[str, float]:
    """Approved relevance per evidence edge type, as the tenant spec declares it (D-8).

    Read-only and empty when the spec declares none, or when its version stage is
    below ``-reviewed`` (D-8b): an unreviewed declaration never reaches scoring,
    so every entity abstains rather than scoring on an unapproved coefficient.
    """
    declared = {
        edge.type: edge.evidencerelevance for edge in domain_spec.ontology.edges if edge.evidencerelevance is not None
    }
    if declared and not relevance_is_active(domain_spec):
        logger.info(
            "evidencerelevance declared by %s at version %r is inactive until -reviewed or -production",
            domain_spec.domain.id,
            domain_spec.domain.version,
        )
        return approved_relevance_map({})
    return approved_relevance_map(declared)


# Two timeouts arrive as ``ClientError``: the server's transaction timeout (by
# code) and the driver's connection-pool acquisition timeout (by message; the
# 5.x driver raises it without a code).
_TRANSACTION_TIMEOUT_CODE_PREFIX = "Neo.ClientError.Transaction.TransactionTimedOut"
_POOL_ACQUISITION_TIMEOUT_PREFIX = "failed to obtain a connection from the pool"


def _is_graph_outage(exc: Exception) -> bool:
    """Whether ``exc`` means the graph could not answer, as opposed to a wrong question.

    Connection loss, session expiry, transient server errors (which include an
    unavailable database), timeouts and an open circuit are outages: the score
    is ``blocked``. Malformed Cypher, type errors, authorization failures and
    programming errors are not, and propagate so they stay visible.
    """
    if isinstance(exc, CircuitOpenError | ServiceUnavailable | SessionExpired | TransientError | TimeoutError):
        return True
    if not isinstance(exc, ClientError):
        return False
    return (exc.code or "").startswith(_TRANSACTION_TIMEOUT_CODE_PREFIX) or str(exc).startswith(
        _POOL_ACQUISITION_TIMEOUT_PREFIX
    )


def _admission(status: Any) -> Literal["admitted", "inferred_unverified"]:
    return "admitted" if status == "observed" else "inferred_unverified"


def _is_stale(expires_at: Any, now: datetime) -> bool:
    if expires_at is None:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True
    return expiry.tzinfo is None or expiry <= now


def _observation(row: dict[str, Any], now: datetime) -> EvidenceObservation | None:
    props = row["props"]
    try:
        return EvidenceObservation(
            entity_ref=row["entity_ref"],
            evidence_ref=props.get("evidence_ref", ""),
            relationship_type=row["relationship_type"],
            source_ref=props.get("source_ref"),
            confidence=props.get("confidence"),
            admission=_admission(props.get("assertion_status")),
            polarity=props.get("polarity"),
            stale=_is_stale(props.get("expires_at"), now),
        )
    except PydanticValidationError:
        return None


def _malformed(row: dict[str, Any]) -> MalformedEvidence:
    """The identity of an edge that did not parse; an unreadable ``evidence_ref`` is unknown (``None``)."""
    ref = row["props"].get("evidence_ref")
    return MalformedEvidence(
        entity_ref=row["entity_ref"],
        relationship_type=row["relationship_type"],
        evidence_ref=ref if isinstance(ref, str) and ref else None,
    )


def _single_revision(rows: Sequence[dict[str, Any]]) -> str | None:
    """The source revision when every read edge reports the same one; never invented."""
    revisions = {row["props"].get("source_revision") for row in rows}
    if len(revisions) == 1:
        revision = revisions.pop()
        return revision if isinstance(revision, str) else None
    return None


def declared_evidence_types(domain_spec: DomainSpec) -> tuple[str, ...]:
    """The relationship types the tenant spec declares for ``targetedge`` sync endpoints, sanitized."""
    endpoints = domain_spec.sync.endpoints if domain_spec.sync else []
    return tuple(sorted({sanitize_label(e.targetedge) for e in endpoints if e.targetedge}))


def read_evidence_query(relationship_types: Sequence[str] | None) -> str:
    """The evidence read, type-bounded when ``relationship_types`` is given (labels via ``sanitize_label``)."""
    if relationship_types is None:
        return _READ_EVIDENCE.format(types="")
    if not relationship_types:
        msg = "a type-bounded evidence read needs at least one relationship type"
        raise ValueError(msg)
    return _READ_EVIDENCE.format(types=":" + "|".join(sanitize_label(t) for t in relationship_types))


class GraphEvidenceBinding:
    """``EvidenceBinding`` over the tenant's Neo4j database, read-only and non-provisioning.

    ``relationship_types`` bounds the read to those types (production callers
    pass ``declared_evidence_types(spec)``; an empty declaration reads nothing).
    ``None`` keeps the untyped scan, the pre-bound behaviour.
    """

    def __init__(
        self,
        driver: GraphDriver,
        approved_relevance: Mapping[str, float],
        *,
        relationship_types: Sequence[str] | None = None,
    ) -> None:
        if isinstance(relationship_types, str):
            msg = "relationship_types must be a sequence of relationship types, not one string"
            raise TypeError(msg)
        self._driver = driver
        self._approved_relevance = approved_relevance
        self._relationship_types = None if relationship_types is None else tuple(relationship_types)
        self._query = None if self._relationship_types == () else read_evidence_query(self._relationship_types)

    @property
    def approved_relevance(self) -> Mapping[str, float]:
        return self._approved_relevance

    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        if len(entity_refs) > MAX_COHORT_SIZE:
            msg = f"evidence read accepts at most {MAX_COHORT_SIZE} refs"
            raise ValueError(msg)
        try:
            self._driver.get_driver()
        except RuntimeError as exc:
            msg = "graph driver is not connected"
            raise EvidenceUnavailableError(msg) from exc
        if self._query is None:
            # The tenant spec declares no evidence relationship type: there is nothing to read.
            return EvidenceRead(connected=True, database=database)
        params = {
            "schema": RELATIONSHIP_EVIDENCE_SCHEMA,
            "tenant": database,
            "refs": list(entity_refs),
            "limit": MAX_EVIDENCE_EDGES + 1,
        }
        try:
            rows = await self._driver.execute_read(self._query, params, database=database)
        except DatabaseNotProvisionedError as exc:
            msg = f"tenant database {database!r} does not exist; a score read never creates it"
            raise EvidenceUnavailableError(msg) from exc
        except Exception as exc:
            if not _is_graph_outage(exc):
                raise
            msg = f"graph evidence for tenant database {database!r} is unavailable ({type(exc).__name__})"
            raise EvidenceUnavailableError(msg) from exc
        if len(rows) > MAX_EVIDENCE_EDGES:
            msg = f"more than {MAX_EVIDENCE_EDGES} evidence edges for this cohort; refusing a truncated read"
            raise EvidenceUnavailableError(msg)

        from engine.config.settings import settings

        # SCR-06: a malformed edge is reported, so its entity abstains instead of
        # reading as having no evidence. Off: the edge is dropped (legacy).
        track_malformed = bool(settings.score_malformed_evidence_abstention_enabled)
        now = datetime.now(tz=UTC)
        observations = []
        malformed = []
        for row in rows:
            observation = _observation(row, now)
            # An edge without a readable evidence_ref parses (as ""), but its
            # identity is unknown: with tracking on it is malformed, not an
            # observation that could never share a valid edge's identity.
            if observation is not None and track_malformed and not observation.evidence_ref:
                observation = None
            if observation is None:
                logger.warning("Skipping malformed evidence edge in tenant database %s", database)
                if track_malformed:
                    malformed.append(_malformed(row))
                continue
            observations.append(observation)
        return EvidenceRead(
            connected=True,
            database=database,
            source_revision=_single_revision(rows),
            observations=tuple(observations),
            malformed=tuple(malformed),
        )
