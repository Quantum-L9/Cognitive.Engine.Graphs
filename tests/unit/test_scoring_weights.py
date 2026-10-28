"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, convergence, dimension-weight]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for convergence loop: ScoringAssembler consuming DimensionWeight nodes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.schema import (
    ComputationType,
    DomainMetadata,
    DomainSpec,
    FeedbackLoopSpec,
    ScoringDimensionSpec,
    ScoringSpec,
    SignalWeightsSpec,
)
from engine.scoring.assembler import ScoringAssembler


def _make_scoring_spec(dimensions: list[dict]) -> MagicMock:
    """Create a mock DomainSpec with scoring + feedback config."""
    spec = MagicMock(spec=DomainSpec)
    spec.scoring = MagicMock(spec=ScoringSpec)

    mock_dims = []
    for dim_dict in dimensions:
        dim = MagicMock(spec=ScoringDimensionSpec)
        dim.name = dim_dict.get("name", "test_dim")
        dim.candidateprop = dim_dict.get("candidateprop")
        dim.queryprop = dim_dict.get("queryprop")
        dim.computation = ComputationType(dim_dict.get("computation", "candidateproperty"))
        dim.weightkey = dim_dict.get("weightkey", "w")
        dim.defaultweight = dim_dict.get("defaultweight", 1.0)
        dim.matchdirections = dim_dict.get("matchdirections")
        dim.minvalue = dim_dict.get("minvalue")
        dim.maxvalue = dim_dict.get("maxvalue")
        dim.defaultwhennull = dim_dict.get("defaultwhennull", 0.0)
        dim.expression = dim_dict.get("expression")
        dim.alias = dim_dict.get("alias")
        dim.decayconstant = dim_dict.get("decayconstant")
        dim.bias = dim_dict.get("bias")
        mock_dims.append(dim)

    spec.scoring.dimensions = mock_dims

    # Mock feedback loop spec
    fl = MagicMock(spec=FeedbackLoopSpec)
    fl.enabled = True
    sw = MagicMock(spec=SignalWeightsSpec)
    sw.enabled = True
    sw.penalty_threshold = 0.5
    sw.penalty_factor = 0.3
    fl.signal_weights = sw
    spec.feedbackloop = fl

    domain_meta = MagicMock(spec=DomainMetadata)
    domain_meta.id = "test_domain"
    spec.domain = domain_meta

    return spec


def _mock_driver() -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    return driver


@pytest.mark.unit
class TestScoringAssemblerLearnedWeights:
    """Test that ScoringAssembler consumes DimensionWeight nodes."""

    @pytest.mark.asyncio
    async def test_load_learned_weights(self) -> None:
        """load_learned_weights queries and stores DimensionWeight data."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "geo",
                    "candidateprop": "lat",
                    "computation": "candidateproperty",
                    "weightkey": "wgeo",
                    "defaultweight": 0.3,
                },
            ]
        )
        driver = _mock_driver()
        driver.execute_query.return_value = [
            {"name": "geo", "weight": 1.5},
        ]

        assembler = ScoringAssembler(spec, graph_driver=driver)
        weights = await assembler.load_learned_weights()

        assert weights == {"geo": 1.5}
        driver.execute_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_learned_weights_applied_to_scoring(self) -> None:
        """Learned weights multiply spec-defined weights in scoring clause."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
                {
                    "name": "score2",
                    "candidateprop": "prop2",
                    "computation": "candidateproperty",
                    "weightkey": "w2",
                    "defaultweight": 0.5,
                },
            ]
        )
        driver = _mock_driver()
        driver.execute_query.return_value = [
            {"name": "score1", "weight": 2.0},  # Will multiply 0.5 * 2.0 = 1.0
        ]

        assembler = ScoringAssembler(spec, graph_driver=driver)
        await assembler.load_learned_weights()

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        # score1 should have weight 0.5 * 2.0 = 1.0
        assert "(1.0 * score1)" in cypher
        # score2 should have default weight 0.5 (no learned weight)
        assert "(0.5 * score2)" in cypher

    def test_no_learned_weights_uses_defaults(self) -> None:
        """Without learned weights, spec defaults are used."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.4,
                },
            ]
        )

        assembler = ScoringAssembler(spec)
        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})

        assert "(0.4 * score1)" in cypher

    @pytest.mark.asyncio
    async def test_no_driver_returns_empty_weights(self) -> None:
        """load_learned_weights returns empty if no driver provided."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
            ]
        )

        assembler = ScoringAssembler(spec)
        weights = await assembler.load_learned_weights()

        assert weights == {}

    @pytest.mark.asyncio
    async def test_feedback_disabled_returns_empty(self) -> None:
        """load_learned_weights returns empty when feedback loop disabled."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
            ]
        )
        spec.feedbackloop.enabled = False
        driver = _mock_driver()

        assembler = ScoringAssembler(spec, graph_driver=driver)
        weights = await assembler.load_learned_weights()

        assert weights == {}
        driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_weight_override_plus_learned(self) -> None:
        """Caller weight override is multiplied by learned weight."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
            ]
        )
        driver = _mock_driver()
        driver.execute_query.return_value = [
            {"name": "score1", "weight": 1.5},
        ]

        assembler = ScoringAssembler(spec, graph_driver=driver)
        await assembler.load_learned_weights()

        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={"w1": 0.8})

        # 0.8 (override) * 1.5 (learned) = 1.2
        expected_weight = 0.8 * 1.5
        assert f"({expected_weight} * score1)" in cypher

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_weights(self) -> None:
        """Empty DB returns no learned weights."""
        spec = _make_scoring_spec(
            [
                {
                    "name": "score1",
                    "candidateprop": "prop1",
                    "computation": "candidateproperty",
                    "weightkey": "w1",
                    "defaultweight": 0.5,
                },
            ]
        )
        driver = _mock_driver()
        driver.execute_query.return_value = []

        assembler = ScoringAssembler(spec, graph_driver=driver)
        weights = await assembler.load_learned_weights()

        assert weights == {}


# ── RU-009 / CEG-COR-018: validate the effective (merged) request weights ──


class _NoRowsDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_query(self, *, cypher: str, parameters: dict, database: str) -> list[dict]:
        self.calls += 1
        return []


async def _orbital_match(weights: dict) -> _NoRowsDriver:
    from engine.handlers import handle_match, init_dependencies
    from tests.fixtures.domain_contracts import synthetic_loader

    driver = _NoRowsDriver()
    loader = MagicMock()
    loader.load_domain.return_value = synthetic_loader().load_domain("orbital-logistics")
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    await handle_match("orbital-logistics", {"query": {}, "match_direction": "window_to_depot", "weights": weights})
    return driver


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-018")
class TestEffectiveRequestWeightValidation:
    """orbital-logistics: w_reliability default 0.6, w_ports default 0.3."""

    async def test_partial_override_exceeding_ceiling_is_rejected(self) -> None:
        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match=r"Weights sum to 1\.3000"):
            await _orbital_match({"w_reliability": 1.0})

    async def test_full_override_summing_to_one_is_accepted(self) -> None:
        driver = await _orbital_match({"w_reliability": 0.7, "w_ports": 0.3})
        assert driver.calls == 1

    async def test_lowering_override_is_accepted(self) -> None:
        driver = await _orbital_match({"w_reliability": 0.1})
        assert driver.calls == 1

    async def test_unknown_weight_key_is_rejected(self) -> None:
        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match="w_typo"):
            await _orbital_match({"w_typo": 0.1})

    async def test_per_key_range_check_unchanged(self) -> None:
        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match="outside allowed range"):
            await _orbital_match({"w_ports": 1.5})

    async def test_non_numeric_weight_is_a_validation_error(self) -> None:
        """A non-numeric override is reported as the caller's error, not a ValueError."""
        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match="'w_reliability' is not a valid number"):
            await _orbital_match({"w_reliability": "abc"})

    async def test_flag_off_validates_supplied_keys_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "match_effective_weight_validation", False)
        driver = await _orbital_match({"w_reliability": 1.0})
        assert driver.calls == 1  # the pre-fix acceptance

    def test_merged_weights_are_the_scoring_coefficients(self) -> None:
        from tests.fixtures.domain_contracts import synthetic_loader

        assembler = ScoringAssembler(synthetic_loader().load_domain("orbital-logistics"))
        merged = assembler.merged_request_weights("window_to_depot", {"w_reliability": 1.0})
        assert merged == {"depot_reliability": 1.0, "port_capacity": 0.3}
        clause, _ = assembler.assemble_scoring_clause("window_to_depot", {"w_reliability": 1.0})
        assert "(1.0 * depot_reliability)" in clause
        assert "(0.3 * port_capacity)" in clause
