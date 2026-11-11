"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, evidence, priority, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_evidence_priority.py
Unit tests for engine/scoring/evidence_priority.py — policy graph_evidence_priority_v1.

Evidence here is explicitly typed and synthetic. The relationship types
(``SYNTHETIC_POSITIVE_REL`` ...) are test-only names, not an ontology: the real
vocabulary is unproven until an admitted EIE -> Gate -> CEG projection exists.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from engine.scoring.evidence_priority import (
    ABSTAIN_REASON,
    MAX_COHORT_SIZE,
    POLICY_VERSION,
    EvidenceObservation,
    EvidenceRead,
    ScoreRequest,
    ScoreRequestError,
    approved_relevance_map,
    parse_score_request,
    score_cohort,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

POSITIVE = "SYNTHETIC_POSITIVE_REL"
WEAKER = "SYNTHETIC_WEAKER_REL"
UNAPPROVED = "SYNTHETIC_UNAPPROVED_REL"
RELEVANCE = approved_relevance_map({POSITIVE: 1.0, WEAKER: 0.5})


def obs(entity_ref: str, evidence_ref: str, **overrides: Any) -> EvidenceObservation:
    fields: dict[str, Any] = {
        "entity_ref": entity_ref,
        "evidence_ref": evidence_ref,
        "relationship_type": POSITIVE,
        "source_ref": f"source:{evidence_ref}",
        "confidence": 0.87,
        "admission": "admitted",
        "polarity": "positive",
    }
    fields.update(overrides)
    return EvidenceObservation(**fields)


def read(*observations: EvidenceObservation) -> EvidenceRead:
    return EvidenceRead(connected=False, database="tenant-a", observations=observations)


def request(*refs: str, top_n: int | None = None) -> ScoreRequest:
    return parse_score_request({"entity_refs": list(refs), "top_n": top_n or len(refs)})


A = "hubspot.company:101"
B = "hubspot.company:102"
C = "hubspot.company:103"


# ── Policy ─────────────────────────────────────────────────────────────────────


def test_score_is_100_times_max_relevance_times_confidence() -> None:
    result = score_cohort(
        request(A),
        read(obs(A, "evidence:42"), obs(A, "evidence:43", relationship_type=WEAKER, confidence=0.9)),
        RELEVANCE,
    )
    [entry] = result["scored"]
    assert result["policy_version"] == POLICY_VERSION
    assert entry["score"] == 87.0
    assert entry["rank"] == 1
    assert entry["factors"] == [
        {
            "kind": "qualified_relationship",
            "relationship_type": POSITIVE,
            "contribution": 87.0,
            "evidence_refs": ["evidence:42"],
        }
    ]
    assert entry["qualifying_evidence_refs"] == ["evidence:42", "evidence:43"]


def test_replay_is_deterministic_including_tie_order() -> None:
    evidence = read(obs(C, "e:3"), obs(A, "e:1"), obs(B, "e:2"))
    first = score_cohort(request(C, B, A), evidence, RELEVANCE)
    second = score_cohort(request(C, B, A), evidence, RELEVANCE)
    assert first == second
    assert [e["entity_ref"] for e in first["scored"]] == [A, B, C]  # tied scores: ref ascending
    assert [e["rank"] for e in first["scored"]] == [1, 2, 3]


def test_graph_relationship_changes_priority_for_identical_crm_cohort() -> None:
    cohort = request(A, B)
    with_relation = score_cohort(cohort, read(obs(A, "e:1", confidence=0.4), obs(B, "e:2", confidence=0.9)), RELEVANCE)
    assert [e["entity_ref"] for e in with_relation["scored"]] == [B, A]

    without_b = score_cohort(cohort, read(obs(A, "e:1", confidence=0.4)), RELEVANCE)
    assert [e["entity_ref"] for e in without_b["scored"]] == [A]
    assert without_b["abstained"] == [
        {"entity_ref": B, "score": None, "reason": ABSTAIN_REASON, "detail": "no_evidence"}
    ]


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"relationship_type": UNAPPROVED}, "no_qualifying_relationship"),
        ({"polarity": "negative"}, "no_qualifying_relationship"),
        ({"admission": "inferred_unverified"}, "no_qualifying_relationship"),
        ({"admission": "rejected"}, "no_qualifying_relationship"),
        ({"stale": True}, "no_qualifying_relationship"),
        ({"confidence": None}, "no_qualifying_relationship"),
        ({"confidence": 1.5}, "no_qualifying_relationship"),
        ({"confidence": float("nan")}, "no_qualifying_relationship"),
        ({"confidence": 0.0}, "no_qualifying_relationship"),
        ({"source_ref": None}, "no_qualifying_relationship"),
        ({"source_ref": ""}, "no_qualifying_relationship"),
        ({"conflicted": True}, "conflicting_evidence"),
    ],
)
def test_unqualified_evidence_abstains_never_zero(overrides: dict[str, Any], detail: str) -> None:
    result = score_cohort(request(A), read(obs(A, "e:1", **overrides)), RELEVANCE)
    assert result["scored"] == []
    assert result["abstained"] == [{"entity_ref": A, "score": None, "reason": ABSTAIN_REASON, "detail": detail}]


def test_zero_relevance_type_does_not_qualify() -> None:
    relevance = approved_relevance_map({POSITIVE: 0.0})
    result = score_cohort(request(A), read(obs(A, "e:1")), relevance)
    assert result["abstained"][0]["score"] is None


def test_disagreeing_copies_of_one_evidence_ref_are_a_conflict() -> None:
    result = score_cohort(request(A), read(obs(A, "e:1"), obs(A, "e:1", confidence=0.2)), RELEVANCE)
    assert result["abstained"][0]["detail"] == "conflicting_evidence"


def _one_ref_under_two_types() -> EvidenceRead:
    """One evidence_ref recorded under two relationship types: two distinct typed observations."""
    return read(obs(A, "e:1", relationship_type=POSITIVE), obs(A, "e:1", relationship_type=WEAKER, confidence=0.4))


def test_identity_per_type_on_one_ref_under_two_types_is_not_a_conflict() -> None:
    """A1: evidence identity is (relationship_type, evidence_ref); the sync writes one edge per type."""
    result = score_cohort(request(A), _one_ref_under_two_types(), RELEVANCE, identity_per_type=True)
    assert result["abstained"] == []
    assert result["scored"][0]["score"] == 87.0
    assert result["scored"][0]["qualifying_evidence_refs"] == ["e:1"]
    assert [factor["relationship_type"] for factor in result["scored"][0]["factors"]] == [POSITIVE]


def test_identity_per_type_on_still_conflicts_within_one_type() -> None:
    result = score_cohort(
        request(A), read(obs(A, "e:1"), obs(A, "e:1", confidence=0.2)), RELEVANCE, identity_per_type=True
    )
    assert result["abstained"][0]["detail"] == "conflicting_evidence"


def test_identity_per_type_off_keeps_ref_only_identity() -> None:
    for result in (
        score_cohort(request(A), _one_ref_under_two_types(), RELEVANCE),
        score_cohort(request(A), _one_ref_under_two_types(), RELEVANCE, identity_per_type=False),
    ):
        assert result["scored"] == []
        assert result["abstained"][0]["detail"] == "conflicting_evidence"


def test_duplicate_evidence_and_repeated_paths_cannot_raise_score() -> None:
    single = score_cohort(request(A), read(obs(A, "e:1")), RELEVANCE)
    repeated = score_cohort(request(A), read(*(obs(A, "e:1") for _ in range(50))), RELEVANCE)
    assert repeated["scored"][0]["score"] == single["scored"][0]["score"]
    assert repeated["scored"][0]["qualifying_evidence_refs"] == ["e:1"]


def test_many_distinct_observations_stay_bounded() -> None:
    evidence = read(*(obs(A, f"e:{i}", confidence=1.0) for i in range(200)))
    score = score_cohort(request(A), evidence, RELEVANCE)["scored"][0]["score"]
    assert 0.0 <= score <= 100.0
    assert score == 100.0


def test_observations_outside_the_cohort_are_ignored() -> None:
    result = score_cohort(request(A), read(obs(B, "e:other")), RELEVANCE)
    assert result["scored"] == []
    assert "e:other" not in repr(result)


def test_top_n_truncates_ranked_list_but_reports_all_abstentions() -> None:
    result = score_cohort(
        request(A, B, C, top_n=1),
        read(obs(A, "e:1", confidence=0.5), obs(B, "e:2", confidence=0.6)),
        RELEVANCE,
    )
    assert [e["entity_ref"] for e in result["scored"]] == [B]
    assert [e["entity_ref"] for e in result["abstained"]] == [C]


def test_result_reports_reader_connection_and_revision_honestly() -> None:
    result = score_cohort(request(A), read(obs(A, "e:1")), RELEVANCE)
    assert result["evidence_reader"] == {"connected": False, "database": "tenant-a"}
    assert result["source_revision"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {POSITIVE: 1.2},
        {POSITIVE: -0.1},
        {POSITIVE: float("inf")},
        {"": 1.0},
        {POSITIVE: True},
        {POSITIVE: "0.5"},
        {POSITIVE: None},
        {POSITIVE: object()},
    ],
)
def test_relevance_map_rejects_out_of_range_coefficients(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        approved_relevance_map(bad)


def test_relevance_map_is_immutable() -> None:
    with pytest.raises(TypeError):
        RELEVANCE[UNAPPROVED] = 1.0  # type: ignore[index]


# ── Request validation (all before any read) ───────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"top_n": 1},
        {"entity_refs": A, "top_n": 1},
        {"entity_refs": [], "top_n": 1},
        {"entity_refs": [f"hubspot.company:{i}" for i in range(MAX_COHORT_SIZE + 1)], "top_n": 1},
        {"entity_refs": [A, A], "top_n": 1},
        {"entity_refs": [A], "top_n": 0},
        {"entity_refs": [A], "top_n": 2},
        {"entity_refs": [A], "top_n": True},
        {"entity_refs": [A], "top_n": "1"},
        {"entity_refs": [A]},
        {"entity_refs": ["no-colon"], "top_n": 1},
        {"entity_refs": ["hubspot.company:1 OR 1=1"], "top_n": 1},
        {"entity_refs": [42], "top_n": 1},
        {"entity_refs": ["x:" + "a" * 300], "top_n": 1},
    ],
)
def test_malformed_requests_are_refused(payload: Any) -> None:
    with pytest.raises(ScoreRequestError):
        parse_score_request(payload)


@pytest.mark.parametrize(
    "forbidden", ["tenant", "domain_id", "database", "policy", "weights", "relevance", "score", "cypher"]
)
def test_server_owned_fields_are_refused(forbidden: str) -> None:
    with pytest.raises(ScoreRequestError, match=forbidden):
        parse_score_request({"entity_refs": [A], "top_n": 1, forbidden: "x"})


def test_valid_request_round_trips() -> None:
    parsed = parse_score_request({"entity_refs": [A, B], "top_n": 2})
    assert parsed.entity_refs == (A, B)
    assert parsed.top_n == 2


# ── Independence from the match path ───────────────────────────────────────────


def test_scorer_imports_nothing_from_the_match_path() -> None:
    """The policy module's own imports name no loader, compiler, assembler or handler."""
    source = (ROOT / "engine" / "scoring" / "evidence_priority.py").read_text(encoding="utf-8")
    imported = {node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import) for alias in node.names
    }
    forbidden = {
        "engine.config.loader",
        "engine.gates.compiler",
        "engine.traversal.assembler",
        "engine.scoring.assembler",
        "engine.handlers",
        "engine.graph.driver",
    }
    assert not imported & forbidden, sorted(imported & forbidden)


def test_scorer_runs_with_domain_loader_and_compilers_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 8: the policy scores with loader, compilers and handle_match replaced by failing stubs.

    ``engine/__init__.py`` imports the loader and ``engine.handlers`` at package
    level, so those modules cannot be blocked from import; their symbols are
    made unusable instead.
    """
    import engine.config.loader
    import engine.gates.compiler
    import engine.handlers
    import engine.scoring.assembler
    import engine.traversal.assembler

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        msg = "match-path symbol used by the scorer"
        raise AssertionError(msg)

    monkeypatch.setattr(engine.config.loader, "DomainPackLoader", unavailable)
    monkeypatch.setattr(engine.gates.compiler, "GateCompiler", unavailable)
    monkeypatch.setattr(engine.traversal.assembler, "TraversalAssembler", unavailable)
    monkeypatch.setattr(engine.scoring.assembler, "ScoringAssembler", unavailable)
    monkeypatch.setattr(engine.handlers, "handle_match", unavailable)

    result = score_cohort(request(A), read(obs(A, "e:1", confidence=0.5)), RELEVANCE)
    assert result["scored"][0]["score"] == 50.0
