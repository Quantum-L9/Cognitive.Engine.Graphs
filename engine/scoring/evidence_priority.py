"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [scoring, evidence, priority, poc]
owner: engine-team
status: active
--- /L9_META ---

Graph-evidence priority policy ``graph_evidence_priority_v1`` (QM.POC0.CEG.SCORE.001).

A domain-neutral, CEG-local relative priority for an authorised cohort of
entity refs, computed from typed relationship evidence:

    score = 100 * max(approved_relevance(relationship_type) * evidence_confidence)

over the unique, qualified observations of each entity. The score is a
relative graph-evidence priority indicator — never a conversion probability,
an ICP fit, or a prediction of sales uplift.

Rules the policy holds, each tested in ``tests/unit/test_evidence_priority.py``:

* Relevance is server-owned. A relationship type with no approved coefficient
  contributes nothing; there is no default relevance and the caller cannot set one.
* Confidence is reliability, not commercial value. It is never defaulted: an
  observation without an explicit confidence in ``[0, 1]`` does not qualify.
* Evidence is deduplicated by its canonical identity, so repeating it cannot
  raise a score. Repeats that disagree are a conflict. The identity is
  ``evidence_ref``, or ``(relationship_type, evidence_ref)`` under
  ``evidence_identity_per_type_enabled`` (A1).
* Missing provenance, non-admitted or negative evidence, stale or conflicting
  evidence, and unsupported relationship types abstain with ``score: None``.
  Nothing is zero-filled.

The module is pure: it reads no graph, loads no domain pack and never touches
the match path (``handle_match``, ``GateCompiler``, ``TraversalAssembler``,
``ScoringAssembler``). Evidence arrives through an ``EvidenceBinding`` the
server supplies. No binding exists yet: the relationship vocabulary, relevance
map and evidence materialisation must first be proven from a real, admitted
EIE -> Gate -> CEG projection, so the local handler reports ``blocked`` until then.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from engine.models.payloads import ENTITY_REF_PATTERN

POLICY_VERSION = "graph_evidence_priority_v1"
MAX_COHORT_SIZE = 25
MAX_ENTITY_REF_LENGTH = 256
SCORE_SCALE = 100.0
SCORE_DECIMALS = 6

REQUEST_KEYS = frozenset({"entity_refs", "top_n"})
ABSTAIN_REASON = "insufficient_evidence"

_ENTITY_REF = re.compile(ENTITY_REF_PATTERN)


class ScoreRequestError(ValueError):
    """A score request was refused before any evidence was read."""


class ScoreRequest(BaseModel):
    """A validated cohort request. Built only by ``parse_score_request``."""

    model_config = ConfigDict(frozen=True)

    entity_refs: tuple[str, ...]
    top_n: int


class EvidenceObservation(BaseModel):
    """One typed relationship observation for an entity, as an evidence reader returns it.

    ``evidence_ref`` is the canonical evidence identity used for deduplication;
    ``source_ref`` is its provenance. ``admission`` and ``polarity`` are
    explicit: only ``admitted`` and ``positive`` evidence can qualify.
    """

    model_config = ConfigDict(frozen=True)

    entity_ref: str
    evidence_ref: str
    relationship_type: str
    source_ref: str | None = None
    confidence: float | None = None
    admission: Literal["admitted", "inferred_unverified", "rejected"]
    polarity: Literal["positive", "negative"]
    stale: bool = False
    conflicted: bool = False


class EvidenceRead(BaseModel):
    """What one evidence read returned, and whether it came from a connected graph."""

    model_config = ConfigDict(frozen=True)

    connected: bool
    database: str
    source_revision: str | None = None
    observations: tuple[EvidenceObservation, ...] = Field(default_factory=tuple)


class EvidenceBinding(Protocol):
    """Server-owned evidence source plus its approved relevance map.

    Both halves are bound together because both are blocked on the same proof:
    the relationship vocabulary a real projection materialises. ``read`` must
    read only ``database`` and must not write or provision anything.
    """

    @property
    def approved_relevance(self) -> Mapping[str, float]:
        """Server-approved relevance coefficient in ``[0, 1]`` per relationship type."""

    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        """Read typed relationship evidence for ``entity_refs`` from ``database`` only."""


def approved_relevance_map(entries: Mapping[str, float]) -> Mapping[str, float]:
    """Freeze a server-owned relevance map after validating every coefficient is in ``[0, 1]``."""
    frozen: dict[str, float] = {}
    for relationship_type, coefficient in entries.items():
        if not isinstance(relationship_type, str) or not relationship_type:
            msg = f"relevance key {relationship_type!r} must be a non-empty relationship type"
            raise ValueError(msg)
        not_a_number = f"relevance for {relationship_type!r} must be a number, got {type(coefficient).__name__}"
        if isinstance(coefficient, bool | str | bytes):
            raise ValueError(not_a_number)
        try:
            value = float(coefficient)
        except (TypeError, ValueError) as exc:
            raise ValueError(not_a_number) from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            msg = f"relevance for {relationship_type!r} must be in [0, 1], got {coefficient!r}"
            raise ValueError(msg)
        frozen[relationship_type] = value
    return MappingProxyType(frozen)


def parse_score_request(payload: Any) -> ScoreRequest:
    """Validate a cohort request. Every refusal happens here, before any read."""
    if not isinstance(payload, dict):
        msg = f"score payload must be an object, got {type(payload).__name__}"
        raise ScoreRequestError(msg)

    extra = sorted(set(payload) - REQUEST_KEYS)
    if extra:
        msg = (
            f"score payload accepts only {sorted(REQUEST_KEYS)}; refusing {extra}. Tenant, database, "
            f"policy, weights, relevance, score and graph queries are server-owned"
        )
        raise ScoreRequestError(msg)

    refs = payload.get("entity_refs")
    if not isinstance(refs, list):
        msg = "entity_refs must be a list of entity ref strings"
        raise ScoreRequestError(msg)
    if not 1 <= len(refs) <= MAX_COHORT_SIZE:
        msg = f"entity_refs must hold 1..{MAX_COHORT_SIZE} refs, got {len(refs)}"
        raise ScoreRequestError(msg)
    for ref in refs:
        if not isinstance(ref, str) or len(ref) > MAX_ENTITY_REF_LENGTH or not _ENTITY_REF.fullmatch(ref):
            msg = f"entity ref {ref!r} does not match {ENTITY_REF_PATTERN} (max {MAX_ENTITY_REF_LENGTH} chars)"
            raise ScoreRequestError(msg)
    if len(set(refs)) != len(refs):
        duplicates = sorted({ref for ref in refs if refs.count(ref) > 1})
        msg = f"entity_refs must be unique; duplicated {duplicates}"
        raise ScoreRequestError(msg)

    top_n = payload.get("top_n")
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        msg = f"top_n must be an integer, got {type(top_n).__name__}"
        raise ScoreRequestError(msg)
    if not 1 <= top_n <= min(MAX_COHORT_SIZE, len(refs)):
        msg = f"top_n must be in 1..{min(MAX_COHORT_SIZE, len(refs))} (cohort size), got {top_n}"
        raise ScoreRequestError(msg)

    return ScoreRequest(entity_refs=tuple(refs), top_n=top_n)


def _abstain(entity_ref: str, detail: str) -> dict[str, Any]:
    return {"entity_ref": entity_ref, "score": None, "reason": ABSTAIN_REASON, "detail": detail}


def _contribution(observation: EvidenceObservation, approved_relevance: Mapping[str, float]) -> float | None:
    """Return ``relevance * confidence`` for a qualifying observation, else None."""
    if observation.admission != "admitted" or observation.polarity != "positive":
        return None
    if observation.stale or observation.conflicted:
        return None
    if not observation.evidence_ref or not observation.source_ref:
        return None
    relevance = approved_relevance.get(observation.relationship_type)
    if relevance is None:
        return None
    confidence = observation.confidence
    if confidence is None or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    product = relevance * confidence
    return product if product > 0.0 else None


def _score_entity(
    entity_ref: str,
    observations: Sequence[EvidenceObservation],
    approved_relevance: Mapping[str, float],
    *,
    identity_per_type: bool,
) -> dict[str, Any]:
    """Score one entity, or return its abstention."""
    if not observations:
        return _abstain(entity_ref, "no_evidence")

    unique: dict[tuple[str, str], EvidenceObservation] = {}
    for observation in observations:
        identity = (observation.relationship_type if identity_per_type else "", observation.evidence_ref)
        seen = unique.get(identity)
        if seen is not None and seen != observation:
            return _abstain(entity_ref, "conflicting_evidence")
        unique[identity] = observation
    if any(observation.conflicted for observation in unique.values()):
        return _abstain(entity_ref, "conflicting_evidence")

    qualified: list[tuple[float, EvidenceObservation]] = []
    for observation in unique.values():
        product = _contribution(observation, approved_relevance)
        if product is not None:
            qualified.append((product, observation))
    if not qualified:
        return _abstain(entity_ref, "no_qualifying_relationship")

    best = max(product for product, _ in qualified)
    score = round(SCORE_SCALE * best, SCORE_DECIMALS)
    determining: dict[str, list[str]] = {}
    for product, observation in qualified:
        if product == best:
            determining.setdefault(observation.relationship_type, []).append(observation.evidence_ref)
    factors = [
        {
            "kind": "qualified_relationship",
            "relationship_type": relationship_type,
            "contribution": score,
            "evidence_refs": sorted(refs),
        }
        for relationship_type, refs in sorted(determining.items())
    ]
    return {
        "entity_ref": entity_ref,
        "score": score,
        "factors": factors,
        "qualifying_evidence_refs": sorted({observation.evidence_ref for _, observation in qualified}),
    }


def score_cohort(
    request: ScoreRequest,
    read: EvidenceRead,
    approved_relevance: Mapping[str, float],
) -> dict[str, Any]:
    """Apply ``graph_evidence_priority_v1`` to one evidence read of the requested cohort.

    Observations for refs outside the cohort are ignored. Scored entities sort
    by score descending, then entity ref ascending; ``top_n`` truncates the
    scored list. Abstentions are always returned in full, sorted by entity ref.
    """
    from engine.config.settings import settings

    identity_per_type = settings.evidence_identity_per_type_enabled
    by_entity: dict[str, list[EvidenceObservation]] = {ref: [] for ref in request.entity_refs}
    for observation in read.observations:
        if observation.entity_ref in by_entity:
            by_entity[observation.entity_ref].append(observation)

    scored: list[dict[str, Any]] = []
    abstained: list[dict[str, Any]] = []
    for entity_ref, observations in by_entity.items():
        outcome = _score_entity(entity_ref, observations, approved_relevance, identity_per_type=identity_per_type)
        (abstained if outcome["score"] is None else scored).append(outcome)

    scored.sort(key=lambda item: (-item["score"], item["entity_ref"]))
    ranked = [{**item, "rank": rank} for rank, item in enumerate(scored[: request.top_n], start=1)]
    abstained.sort(key=lambda item: item["entity_ref"])

    return {
        "status": "ok",
        "policy_version": POLICY_VERSION,
        "evidence_reader": {"connected": read.connected, "database": read.database},
        "source_revision": read.source_revision,
        "cohort_size": len(request.entity_refs),
        "top_n": request.top_n,
        "scored": ranked,
        "abstained": abstained,
    }


def blocked_result(request: ScoreRequest, reason: str) -> dict[str, Any]:
    """The honest answer when no evidence binding exists: nothing was read, nothing is scored."""
    return {
        "status": "blocked",
        "policy_version": POLICY_VERSION,
        "evidence_reader": {"connected": False, "database": None, "reason": reason},
        "source_revision": None,
        "cohort_size": len(request.entity_refs),
        "top_n": request.top_n,
        "scored": [],
        "abstained": [],
    }
