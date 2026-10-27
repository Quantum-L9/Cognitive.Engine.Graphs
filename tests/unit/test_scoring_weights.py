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

import re as _re
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


# ── RU-022 / CEG-COR-030: reader keys match the writer's DimensionWeight (RG-028) ──


class _PropertyLevelDimensionWeightStore:
    """Fake driver that stores what SignalWeightCalculator._store_weights writes
    and answers a DimensionWeight reader by evaluating the reader's own predicates.

    Not a blanket ``return_value``: a reader that filters on properties the
    writer never sets gets zero rows, exactly as Neo4j would return.
    """

    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.reader_cypher: list[str] = []
        self.execute_query = AsyncMock(side_effect=self._execute)

    async def _execute(self, cypher: str, parameters: dict | None = None, database: str | None = None, **_kw):
        parameters = parameters or {}
        if "MERGE (dw:DimensionWeight" in cypher:
            key = {"dimension": parameters["dimension"], "tenant": parameters["tenant"]}
            node = next((n for n in self.nodes if all(n.get(k) == v for k, v in key.items())), None)
            if node is None:
                node = dict(key)
                self.nodes.append(node)
            node.update(weight=parameters["weight"], confidence=parameters["confidence"])
            node["ci_width"] = parameters["ci_width"]
            return [{"dimension": parameters["dimension"]}]
        self.reader_cypher.append(cypher)
        rows = list(self.nodes)
        map_pattern = _re.search(r"\(dw:DimensionWeight\s*\{([^}]*)\}\)", cypher)
        if map_pattern:
            for prop, param in _re.findall(r"(\w+)\s*:\s*\$(\w+)", map_pattern.group(1)):
                rows = [r for r in rows if r.get(prop) == parameters.get(param)]
        for prop, param in _re.findall(r"dw\.(\w+)\s+IN\s+\$(\w+)", cypher):
            rows = [r for r in rows if r.get(prop) in parameters.get(param, [])]
        for prop, param in _re.findall(r"dw\.(\w+)\s*=\s*\$(\w+)", cypher):
            rows = [r for r in rows if r.get(prop) == parameters.get(param)]
        returns = _re.findall(r"dw\.(\w+)\s+AS\s+(\w+)", cypher)
        return [{alias: r.get(prop) for prop, alias in returns} for r in rows]


def _plasticos_like_spec(domain_id: str = "plasticos") -> MagicMock:
    spec = _make_scoring_spec(
        [
            {"name": "geo_proximity", "weightkey": "w_geo", "defaultweight": 0.25},
            {"name": "credit_score", "weightkey": "w_credit", "defaultweight": 0.15},
        ]
    )
    spec.domain.id = domain_id
    return spec


async def _write_weights(store: _PropertyLevelDimensionWeightStore, spec, details: dict[str, float]) -> None:
    from engine.feedback.signal_weights import SignalWeightCalculator

    calc = SignalWeightCalculator.__new__(SignalWeightCalculator)
    calc._driver = store
    calc._spec = spec
    calc._db = spec.domain.id
    await calc._store_weights({name: {"weight": w, "confidence": 0.9, "ci_width": 0.1} for name, w in details.items()})


@pytest.mark.unit
class TestLearnedWeightsTenantKeyed:
    @pytest.mark.asyncio
    async def test_reader_returns_what_the_writer_stored(self) -> None:
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, spec, {"geo_proximity": 0.2})
        assert store.nodes[0]["dimension"] == "geo_proximity"
        assert store.nodes[0]["tenant"] == "plasticos"

        weights = await ScoringAssembler(spec, graph_driver=store).load_learned_weights()

        assert weights == {"geo_proximity": 0.2}

    @pytest.mark.asyncio
    async def test_reader_filters_on_dimension_and_tenant_only(self) -> None:
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await ScoringAssembler(spec, graph_driver=store).load_learned_weights()

        cypher = store.reader_cypher[-1]
        assert "{tenant: $tenant}" in cypher
        assert "dw.dimension IN $names" in cypher
        assert "dimension_name" not in cypher
        assert "domain_id" not in cypher
        _, kwargs = store.execute_query.call_args
        assert kwargs["parameters"] == {"names": ["geo_proximity", "credit_score"], "tenant": "plasticos"}
        assert kwargs["database"] == "plasticos"

    @pytest.mark.asyncio
    async def test_reader_is_tenant_scoped(self) -> None:
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, _plasticos_like_spec("other_tenant"), {"geo_proximity": 0.2})

        assert await ScoringAssembler(spec, graph_driver=store).load_learned_weights() == {}

    @pytest.mark.asyncio
    async def test_flag_off_restores_legacy_reader(self, monkeypatch) -> None:
        """C-21: learned_weights_tenant_keyed=False emits the legacy (inert) reader."""
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "learned_weights_tenant_keyed", False)
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, spec, {"geo_proximity": 0.2})

        weights = await ScoringAssembler(spec, graph_driver=store).load_learned_weights()

        assert weights == {}
        assert "dw.dimension_name IN $names AND dw.domain_id = $domain_id" in store.reader_cypher[-1]
