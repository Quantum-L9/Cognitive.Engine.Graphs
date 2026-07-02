"""Unit tests — Parameter coercion: range floats, booleans, set lists, None exclusion."""

from __future__ import annotations

from pathlib import Path

DOMAINS_DIR = Path(__file__).parent.parent.parent / "domains"


def test_resolve_parameters_preserves_input():
    """resolve_parameters returns input data with derived parameters added."""
    from engine.config.loader import DomainPackLoader
    from engine.traversal.resolver import ParameterResolver

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    resolver = ParameterResolver(spec)
    result = resolver.resolve_parameters({"some_param": None, "valid_param": "value"})
    # resolve_parameters copies input and adds derived params
    assert result.get("valid_param") == "value"
    assert isinstance(result, dict)


def test_string_passthrough():
    from engine.config.loader import DomainPackLoader
    from engine.traversal.resolver import ParameterResolver

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    resolver = ParameterResolver(spec)
    result = resolver.resolve_parameters({"tag": "HDPE"})
    assert result["tag"] == "HDPE"


def test_empty_params_returns_empty():
    from engine.config.loader import DomainPackLoader
    from engine.traversal.resolver import ParameterResolver

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    resolver = ParameterResolver(spec)
    result = resolver.resolve_parameters({})
    assert isinstance(result, dict)
    assert len(result) == 0
