"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, counterfactual, scenario-generation]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for R6 counterfactual scenario generation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.causal.counterfactual import (
    _INTERVENTION_ADD_DIMENSION,
    CounterfactualGenerator,
)
from engine.config.schema import CounterfactualSpec


def _make_spec(
    max_scenarios: int = 3,
    min_confidence: float = 0.3,
    pool_size: int = 10,
) -> CounterfactualSpec:
    return CounterfactualSpec(
        enabled=True,
        max_scenarios_per_outcome=max_scenarios,
        min_confidence=min_confidence,
        comparison_pool_size=pool_size,
    )


def _mock_driver() -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    return driver


@pytest.mark.unit
class TestCounterfactualGenerator:
    """Test counterfactual scenario generation."""

    @pytest.mark.asyncio
    async def test_generate_missing_dimension_scenario(self) -> None:
        """Generates scenario when positive outcomes have dimensions the negative lacks."""
        spec = _make_spec()
        driver = _mock_driver()

        # Outcome fingerprint (failure): missing "geodecay"
        driver.execute_query.side_effect = [
            # get_outcome_fingerprint
            [
                {
                    "active_dimensions": ["communitymatch", "pricealignment"],
                    "dimension_weights": {"communitymatch": 0.3, "pricealignment": 0.25},
                    "gates_passed": ["density_range"],
                    "match_direction": "forward",
                    "candidate_count": 10,
                    "outcome": "failure",
                }
            ],
            # find_similar_positives: 3 winners all have "geodecay"
            [
                {
                    "active_dimensions": ["geodecay", "communitymatch", "pricealignment"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": "pos_1",
                },
                {
                    "active_dimensions": ["geodecay", "communitymatch"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": "pos_2",
                },
                {
                    "active_dimensions": ["geodecay", "pricealignment"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": "pos_3",
                },
            ],
            # store_scenarios
            [],
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("out_fail_001")

        assert len(scenarios) >= 1
        geo_scenario = next((s for s in scenarios if s["key_difference"] == "geodecay"), None)
        assert geo_scenario is not None
        assert geo_scenario["intervention_type"] == _INTERVENTION_ADD_DIMENSION
        assert geo_scenario["confidence"] == 1.0  # 3/3

    @pytest.mark.asyncio
    async def test_confidence_filtering(self) -> None:
        """Scenarios below min_confidence are excluded."""
        spec = _make_spec(min_confidence=0.5)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [
                {
                    "active_dimensions": ["communitymatch"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "match_direction": "forward",
                    "candidate_count": 5,
                    "outcome": "failure",
                }
            ],
            # 10 positives, only 3 have "geodecay" → 3/10 = 0.3 < 0.5
            [
                {
                    "active_dimensions": ["geodecay", "communitymatch"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": f"pos_{i}",
                }
                if i < 3
                else {
                    "active_dimensions": ["communitymatch", "pricealignment"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": f"pos_{i}",
                }
                for i in range(10)
            ],
            [],
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("out_fail_002")

        # pricealignment is in 7/10 = 0.7 >= 0.5, should appear
        # geodecay is in 3/10 = 0.3 < 0.5, should NOT appear
        geo_scenarios = [s for s in scenarios if s["key_difference"] == "geodecay"]
        price_scenarios = [s for s in scenarios if s["key_difference"] == "pricealignment"]
        assert len(geo_scenarios) == 0
        assert len(price_scenarios) >= 1

    @pytest.mark.asyncio
    async def test_max_scenarios_limit(self) -> None:
        """Number of scenarios is capped at max_scenarios_per_outcome."""
        spec = _make_spec(max_scenarios=2)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [
                {
                    "active_dimensions": [],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "match_direction": "forward",
                    "candidate_count": 5,
                    "outcome": "failure",
                }
            ],
            # Positives with many different dimensions
            [
                {
                    "active_dimensions": ["dim_a", "dim_b", "dim_c", "dim_d"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": f"pos_{i}",
                }
                for i in range(5)
            ],
            [],
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("out_fail_003")

        assert len(scenarios) <= 2

    @pytest.mark.asyncio
    async def test_no_fingerprint_returns_empty(self) -> None:
        """Returns empty when outcome has no fingerprint."""
        spec = _make_spec()
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [],  # No outcome found
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("nonexistent")

        assert scenarios == []

    @pytest.mark.asyncio
    async def test_no_positives_returns_empty(self) -> None:
        """Returns empty when no positive outcomes found for comparison."""
        spec = _make_spec()
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [
                {
                    "active_dimensions": ["geo"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "match_direction": "forward",
                    "candidate_count": 5,
                    "outcome": "failure",
                }
            ],
            [],  # No positives
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("out_fail_004")

        assert scenarios == []

    @pytest.mark.asyncio
    async def test_scenario_has_required_fields(self) -> None:
        """Each scenario has all required fields."""
        spec = _make_spec()
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [
                {
                    "active_dimensions": ["communitymatch"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "match_direction": "forward",
                    "candidate_count": 5,
                    "outcome": "failure",
                }
            ],
            [
                {
                    "active_dimensions": ["geodecay", "communitymatch"],
                    "dimension_weights": {},
                    "gates_passed": [],
                    "outcome_id": "pos_1",
                },
            ],
            [],
        ]

        gen = CounterfactualGenerator(spec, driver, "test_domain", outcome_label="DockingResult")
        scenarios = await gen.generate_for_outcome("out_fail_005")

        for scenario in scenarios:
            assert "scenario_id" in scenario
            assert "actual_outcome" in scenario
            assert "counterfactual_outcome" in scenario
            assert "intervention_type" in scenario
            assert "confidence" in scenario
            assert "key_difference" in scenario
            assert scenario["actual_outcome"] == "out_fail_005"
            assert scenario["counterfactual_outcome"] == "success"


# ── RU-005 / CEG-COR-056: outcome map fields stored in one decodable encoding ──

_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


def _fingerprint_rows(actual_weights: object, positive_weights: object) -> list[list[dict]]:
    return [
        [
            {
                "active_dimensions": ["geo", "price"],
                "dimension_weights": actual_weights,
                "gates_passed": [],
                "match_direction": "forward",
                "candidate_count": 3,
                "outcome": "failure",
            }
        ],
        [
            {
                "active_dimensions": ["geo", "price"],
                "dimension_weights": positive_weights,
                "gates_passed": [],
                "outcome_id": "pos_1",
            }
        ],
        [],
    ]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-056")
class TestStoredDimensionWeights:
    async def test_json_string_weights_produce_weight_scenarios(self) -> None:
        from engine.models.outcomes import encode_outcome_map

        driver = _mock_driver()
        driver.execute_query.side_effect = _fingerprint_rows(
            encode_outcome_map({"geo": 0.1, "price": 0.3}),
            encode_outcome_map({"geo": 0.5, "price": 0.3}),
        )
        scenarios = await CounterfactualGenerator(
            _make_spec(), driver, "d", outcome_label="DockingResult"
        ).generate_for_outcome("out_1")
        assert [s["key_difference"] for s in scenarios] == ["geo_weight_increase"]

    async def test_legacy_repr_string_is_tolerated_without_weight_scenarios(self) -> None:
        driver = _mock_driver()
        driver.execute_query.side_effect = _fingerprint_rows(
            str({"geo": 0.1, "price": 0.3}),
            str({"geo": 0.5, "price": 0.3}),
        )
        scenarios = await CounterfactualGenerator(
            _make_spec(), driver, "d", outcome_label="DockingResult"
        ).generate_for_outcome("out_1")
        assert scenarios == []

    async def test_dict_valued_weights_still_accepted(self) -> None:
        driver = _mock_driver()
        driver.execute_query.side_effect = _fingerprint_rows({"geo": 0.1}, {"geo": 0.5})
        scenarios = await CounterfactualGenerator(
            _make_spec(), driver, "d", outcome_label="DockingResult"
        ).generate_for_outcome("out_1")
        assert [s["key_difference"] for s in scenarios] == ["geo_weight_increase"]

    def test_codec_round_trip_and_tolerance(self) -> None:
        import json

        from engine.models.outcomes import decode_outcome_map, encode_outcome_map

        encoded = encode_outcome_map({"b": 0.2, "a": 0.5})
        assert encoded == '{"a": 0.5, "b": 0.2}'
        assert json.loads(encoded) == {"a": 0.5, "b": 0.2}
        assert decode_outcome_map(encoded) == {"a": 0.5, "b": 0.2}
        assert decode_outcome_map({"a": 1}) == {"a": 1}
        assert decode_outcome_map("{'a': 0.5}") == {}  # legacy repr: never eval'd
        assert decode_outcome_map("[1, 2]") == {}
        assert decode_outcome_map(None) == {}
        assert encode_outcome_map(None) == "{}"

    async def test_handle_outcomes_writes_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        from engine.config.loader import DomainPackLoader
        from engine.config.settings import settings
        from engine.handlers import handle_outcomes, init_dependencies

        spec = DomainPackLoader(config_path=str(_DOMAINS)).load_domain("plasticos")
        driver = _mock_driver()
        driver.execute_query.return_value = [{"outcome_id": "x"}]
        loader = MagicMock()
        loader.load_domain.return_value = spec
        payload = {
            "match_id": "m",
            "candidate_id": "c",
            "outcome": "success",
            "fingerprint": {"dimension_weights": {"geo_proximity": 0.25, "credit_score": 0.15}},
        }

        init_dependencies(driver, loader)
        await handle_outcomes("plasticos", dict(payload))
        written = driver.execute_query.call_args.kwargs["parameters"]["dimension_weights"]
        assert json.loads(written) == {"geo_proximity": 0.25, "credit_score": 0.15}

        monkeypatch.setattr(settings, "outcome_map_json_encoding", False)
        await handle_outcomes("plasticos", dict(payload))
        legacy = driver.execute_query.call_args.kwargs["parameters"]["dimension_weights"]
        assert legacy == str({"geo_proximity": 0.25, "credit_score": 0.15})


# ── RU-075 / CEG-COR-122: one scenario per (intervention, key_difference); spec label ──


@pytest.mark.unit
class TestCounterfactualDedupeAndLabel:
    def _actual(self) -> dict:
        return {"active_dimensions": ["price"], "dimension_weights": {"price": 0.1}, "match_direction": "d1"}

    def _positives(self) -> list[dict]:
        return [
            {"active_dimensions": ["price"], "dimension_weights": {"price": 0.5}, "outcome_id": "p1"},
            {"active_dimensions": ["price"], "dimension_weights": {"price": 0.6}, "outcome_id": "p2"},
        ]

    def test_two_positives_with_higher_weight_yield_one_scenario(self) -> None:
        generator = CounterfactualGenerator(_make_spec(), _mock_driver(), "test_db", outcome_label="DockingResult")
        scenarios = generator._diff_fingerprints(self._actual(), self._positives(), "out_1")
        keys = [(s["intervention_type"], s["key_difference"]) for s in scenarios]
        assert keys == [("change_weight", "price_weight_increase")]

    def test_flag_off_restores_duplicates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "counterfactual_dedupe_and_spec_label", False)
        generator = CounterfactualGenerator(_make_spec(), _mock_driver(), "test_db", outcome_label="DockingResult")
        scenarios = generator._diff_fingerprints(self._actual(), self._positives(), "out_1")
        assert len(scenarios) == 2

    @pytest.mark.asyncio
    async def test_queries_use_the_spec_outcome_label(self) -> None:
        driver = _mock_driver()
        driver.execute_query.side_effect = [
            [{"active_dimensions": ["price"], "dimension_weights": {}, "match_direction": "d1"}],
            [],
        ]
        generator = CounterfactualGenerator(_make_spec(), driver, "test_db", outcome_label="Outcome")
        await generator.generate_for_outcome("out_1")
        cyphers = [call.kwargs["cypher"] for call in driver.execute_query.call_args_list]
        assert all("(o:Outcome" in c for c in cyphers)
        assert not any("TransactionOutcome" in c for c in cyphers)

    def test_outcome_label_has_no_default(self) -> None:
        """Stage 2 follow-up (item 6): the caller passes the domain's label; there is no borrowed default."""
        import inspect

        parameter = inspect.signature(CounterfactualGenerator).parameters["outcome_label"]
        assert parameter.default is inspect.Parameter.empty
        spec, driver = _make_spec(), _mock_driver()
        with pytest.raises(TypeError):
            CounterfactualGenerator(spec, driver, "test_db")  # type: ignore[call-arg]

    def test_unsafe_label_is_refused(self) -> None:
        with pytest.raises(ValueError):
            CounterfactualGenerator(_make_spec(), _mock_driver(), "test_db", outcome_label="X) DETACH DELETE (n")
