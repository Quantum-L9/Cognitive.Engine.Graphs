"""
Data model contract tests for OutcomeRecord, graph-schema, and shared models.

Sources:
  engine/models/outcomes.py:OutcomeRecord
  engine/handlers.py:handle_outcomes(), handle_resolve()
  engine/config/schema.py:OntologySpec, NodeSpec, EdgeSpec
  docs/contracts/data/graph-schema.yaml
"""

import pytest

pytestmark = pytest.mark.unit

try:
    from jsonschema import ValidationError, validate
except ImportError:
    pytest.skip("jsonschema not installed", allow_module_level=True)


def _make_outcome(**overrides) -> dict:
    base = {
        "match_id": "match_abc123",
        "candidate_id": "buyer_042",
        "outcome": "success",
        "dimension_scores": {
            "structural": 0.92,
            "geo": 0.71,
            "reinforcement": 0.88,
            "freshness": 0.95,
        },
        "was_selected": True,
    }
    base.update(overrides)
    return base


def test_outcome_schema_has_meta(outcome_record_schema):
    assert outcome_record_schema.get("type") == "object"
    assert "$schema" in outcome_record_schema
    assert "title" in outcome_record_schema


def test_outcome_schema_required_fields(outcome_record_schema):
    required = set(outcome_record_schema.get("required", []))
    for field in ("match_id", "candidate_id", "dimension_scores", "was_selected"):
        assert field in required, (
            f"OutcomeRecord.required must include '{field}'. Source: engine/models/outcomes.py:OutcomeRecord"
        )


def test_outcome_dimension_scores_type(outcome_record_schema):
    prop = outcome_record_schema["properties"]["dimension_scores"]
    assert prop.get("type") == "object"
    additional = prop.get("additionalProperties", {})
    assert additional.get("type") == "number"


def test_outcome_feedback_score_bounded(outcome_record_schema):
    prop = outcome_record_schema["properties"].get("feedback_score", {})
    assert prop.get("minimum") == 0.0, "feedback_score minimum must be 0.0"
    assert prop.get("maximum") == 1.0, "feedback_score maximum must be 1.0"


def test_outcome_feedback_score_nullable(outcome_record_schema):
    prop = outcome_record_schema["properties"].get("feedback_score", {})
    type_val = prop.get("type", "")
    is_nullable = (isinstance(type_val, list) and "null" in type_val) or prop.get("nullable") is True
    assert is_nullable, "feedback_score must be nullable (optional field)"


def test_outcome_schema_has_examples(outcome_record_schema):
    assert outcome_record_schema.get("examples"), "OutcomeRecord schema missing examples"


def test_valid_outcome_passes_schema(outcome_record_schema):
    validate(instance=_make_outcome(), schema=outcome_record_schema)


def test_outcome_missing_match_id_fails(outcome_record_schema):
    data = _make_outcome()
    del data["match_id"]
    with pytest.raises(ValidationError):
        validate(instance=data, schema=outcome_record_schema)


def test_outcome_missing_was_selected_fails(outcome_record_schema):
    data = _make_outcome()
    del data["was_selected"]
    with pytest.raises(ValidationError):
        validate(instance=data, schema=outcome_record_schema)


def test_outcome_additional_properties_rejected(outcome_record_schema):
    if outcome_record_schema.get("additionalProperties") is not False:
        pytest.skip("additionalProperties not set to false — strict mode not enabled")
    data = _make_outcome(unknown_field="bad")
    with pytest.raises(ValidationError):
        validate(instance=data, schema=outcome_record_schema)


# ── Graph Schema ─────────────────────────────────────────────────────────────


def test_graph_schema_has_system_nodes(graph_schema):
    assert "system_nodes" in graph_schema, (
        "graph-schema.yaml must define system_nodes. Source: engine/handlers.py:handle_outcomes()"
    )


_OUTCOME_NODE = "< the domain's feedbackloop.outcome_node_label >"
_OUTCOME_EDGE = "< the domain's feedbackloop.outcome_edge_type >"


def test_outcome_node_is_the_domains_declared_label(graph_schema):
    """Stage 2 follow-up: the outcome node's label is the domain's declaration, not engine vocabulary."""
    nodes = {n.get("label"): n for n in graph_schema.get("system_nodes", [])}
    assert _OUTCOME_NODE in nodes
    assert "TransactionOutcome" not in nodes, "the outcome label is the domain's, not a system node label"
    assert nodes[_OUTCOME_NODE].get("example") == "TransactionOutcome"


def test_outcome_node_has_required_properties(graph_schema):
    node = next(
        (n for n in graph_schema.get("system_nodes", []) if n.get("label") == _OUTCOME_NODE),
        None,
    )
    assert node is not None, "outcome node not found"
    prop_names = {p["name"] for p in node.get("properties", [])}
    for field in ("outcome_id", "match_id", "candidate_id", "outcome", "created_at"):
        assert field in prop_names, f"outcome node missing property '{field}'"


def test_graph_schema_has_system_edges(graph_schema):
    assert "system_edges" in graph_schema


def test_outcome_edge_is_the_domains_declared_type(graph_schema):
    edges = {e.get("type"): e for e in graph_schema.get("system_edges", [])}
    assert _OUTCOME_EDGE in edges
    assert edges[_OUTCOME_EDGE].get("example") == "RESULTED_IN"
    assert edges[_OUTCOME_EDGE].get("to") == _OUTCOME_NODE


def test_resolved_from_edge_defined(graph_schema):
    edge_types = [e.get("type") for e in graph_schema.get("system_edges", [])]
    assert "RESOLVED_FROM" in edge_types


def test_resolved_from_direction_matches_resolver(graph_schema):
    """RU-102 / CEG-COR-124 (DEC-40 option A): the resolver writes
    ``(duplicate)-[:RESOLVED_FROM]->(canonical)`` (engine/resolution/resolver.py
    ``_create_resolution`` / ``_merge_resolution``); the contract states the same."""
    edge = next(e for e in graph_schema["system_edges"] if e.get("type") == "RESOLVED_FROM")
    assert edge["from"] == "< duplicate entity >"
    assert edge["to"] == "< canonical entity >"
    description = edge["description"].lower()
    assert "no engine match" in description
    assert "transfer" not in description or "not transferred" in description


def test_resolution_docstrings_do_not_claim_relationship_transfer():
    """RU-102: resolution links duplicates to a canonical; it moves no edges."""
    import engine.resolution.resolver as resolver_module
    from engine.handlers import handle_resolve
    from engine.resolution.resolver import EntityResolver

    for doc in (resolver_module.__doc__, EntityResolver.__doc__, handle_resolve.__doc__):
        text = (doc or "").lower()
        assert "transfers relationships" not in text
        assert "transfer edges" not in text
        assert "merge duplicates" not in text


def test_graph_schema_has_domain_abstraction_note(graph_schema):
    assert "domain_schema_abstraction" in graph_schema
