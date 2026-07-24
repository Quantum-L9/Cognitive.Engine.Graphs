"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [matching, traversal]
status: active
--- /L9_META ---

Unit tests — ParameterResolver: passthrough, derived parameters, empty input.

Real API (engine/traversal/resolver.py):
    ParameterResolver(domain_spec).resolve_parameters(query_data: dict) -> dict
Returns the query data with derived parameters added. It does NOT drop None
values — null handling is delegated to gate null semantics in Cypher.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader()
    try:
        return loader.load_domain("plasticos")
    except Exception:
        pytest.skip("plasticos domain spec not loadable with current schema")


def test_none_values_preserved_for_null_semantics(plasticos_spec):
    """None values pass through — gates handle nulls via nullbehavior."""
    from engine.traversal.resolver import ParameterResolver

    resolver = ParameterResolver(plasticos_spec)
    result = resolver.resolve_parameters({"some_param": None, "valid_param": "value"})
    assert result.get("valid_param") == "value"
    assert "some_param" in result
    assert result["some_param"] is None


def test_string_passthrough(plasticos_spec):
    """String params pass through unchanged."""
    from engine.traversal.resolver import ParameterResolver

    resolver = ParameterResolver(plasticos_spec)
    result = resolver.resolve_parameters({"tag": "HDPE"})
    assert result["tag"] == "HDPE"


def test_empty_params_returns_dict(plasticos_spec):
    """Empty input returns a dict (plus any derivable parameters)."""
    from engine.traversal.resolver import ParameterResolver

    resolver = ParameterResolver(plasticos_spec)
    result = resolver.resolve_parameters({})
    assert isinstance(result, dict)


def test_input_not_mutated(plasticos_spec):
    """resolve_parameters copies the input rather than mutating it."""
    from engine.traversal.resolver import ParameterResolver

    resolver = ParameterResolver(plasticos_spec)
    original = {"polymer_type": "PET"}
    snapshot = dict(original)
    resolver.resolve_parameters(original)
    assert original == snapshot
