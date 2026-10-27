"""Unit tests — ParameterResolver: passthrough, derived parameters, empty input.

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


# ── RU-016 / CEG-COR-171: defaults are backfilled before derived parameters ──


def _freight_spec_with_defaulted_derivation():
    from pathlib import Path

    from engine.config.loader import DomainPackLoader
    from engine.config.schema import DerivedParameterSpec, QueryFieldSpec

    domains = Path(__file__).resolve().parents[2] / "domains"
    base = DomainPackLoader(config_path=str(domains)).load_domain("freight-matching")
    qs = base.queryschema.model_copy(
        update={"fields": [*base.queryschema.fields, QueryFieldSpec(name="r1a_budget", type="float", default=100.0)]}
    )
    return base.model_copy(
        update={
            "queryschema": qs,
            "derivedparameters": [
                *base.derivedparameters,
                DerivedParameterSpec(name="r1a_half_budget", expression="r1a_budget / 2.0", type="float"),
            ],
        }
    )


class _ParamCapture:
    def __init__(self) -> None:
        self.parameters: list[dict] = []

    async def execute_query(self, *, cypher: str, parameters: dict, database: str) -> list[dict]:
        self.parameters.append(parameters)
        return []


async def _match_freight(query: dict) -> dict:
    from unittest.mock import MagicMock

    from engine.handlers import handle_match, init_dependencies

    spec = _freight_spec_with_defaulted_derivation()
    driver = _ParamCapture()
    loader = MagicMock()
    loader.load_domain.return_value = spec
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    await handle_match(spec.domain.id, {"query": query, "match_direction": spec.queryschema.matchdirections[0]})
    return driver.parameters[0]


@pytest.mark.finding("CEG-COR-171")
async def test_derived_parameter_sees_declared_default_for_omitted_field():
    params = await _match_freight({})
    assert params["r1a_budget"] == 100.0
    assert params["r1a_half_budget"] == 50.0


@pytest.mark.finding("CEG-COR-171")
async def test_explicit_value_wins_over_declared_default():
    params = await _match_freight({"r1a_budget": 30.0})
    assert params["r1a_budget"] == 30.0
    assert params["r1a_half_budget"] == 15.0


@pytest.mark.finding("CEG-COR-171")
async def test_flag_off_resolves_before_defaults(monkeypatch):
    from engine.config.settings import settings
    from engine.handlers import ValidationError

    monkeypatch.setattr(settings, "query_defaults_before_derived", False)
    with pytest.raises(ValidationError, match="Derived parameter|r1a_budget|Unknown"):
        await _match_freight({})
