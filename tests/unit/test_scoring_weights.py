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
    async def test_learned_weights_applied_to_scoring(self, monkeypatch) -> None:
        """Learned weights multiply spec-defined weights in scoring clause.

        RU-023 / CEG-COR-083: the original expectation (0.5 * 2.0 = 1.0 next to
        0.5, sum 1.5) breached the C-22 ceiling. With renormalisation on the
        multiplier still sets the ratio (score1 = 2 x score2) and the sum stays
        at the pre-learning 1.0; the multiply-only result is asserted with
        learned_weight_renormalization off.
        """
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

        coeffs = _coefficients(cypher)
        assert coeffs["score1"] == pytest.approx(2.0 * coeffs["score2"])
        assert coeffs["score1"] + coeffs["score2"] == pytest.approx(1.0)

        from engine.config.settings import settings

        monkeypatch.setattr(settings, "learned_weight_renormalization", False)
        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={})
        # Legacy multiply-only: score1 0.5 * 2.0 = 1.0, score2 default 0.5
        assert "(1.0 * score1)" in cypher
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
    async def test_weight_override_plus_learned(self, monkeypatch) -> None:
        """Caller weight override is multiplied by learned weight.

        RU-023 / CEG-COR-083: 0.8 * 1.5 = 1.2 breaches the C-22 ceiling. With
        renormalisation on, the lone non-penalised dimension keeps its
        pre-learning (override) weight 0.8; multiply-only is asserted with
        learned_weight_renormalization off.
        """
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
        assert _coefficients(cypher)["score1"] == pytest.approx(0.8)

        from engine.config.settings import settings

        monkeypatch.setattr(settings, "learned_weight_renormalization", False)
        cypher, _ = assembler.assemble_scoring_clause(match_direction="any", weights={"w1": 0.8})
        # Legacy: 0.8 (override) * 1.5 (learned) = 1.2
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


# ── RU-023 / CEG-COR-083: renormalised learned weights; 0.0 is a learned weight (RG-030) ──

_COEFF = _re.compile(r"\((-?[0-9.eE+-]+) \* (\w+)\)")


def _coefficients(clause: str) -> dict[str, float]:
    score_line = clause.splitlines()[-1]
    return {alias: float(value) for value, alias in _COEFF.findall(score_line)}


@pytest.fixture
def plasticos(plasticos_domain_spec):
    """The checked-in plasticos design pack (conftest ``plasticos_domain_spec``, DESIGN_PACKS_DIR)."""
    return plasticos_domain_spec


_SUPPLY = "supply_opportunity_to_buyer_facility"


@pytest.mark.unit
class TestLearnedWeightRenormalization:
    @pytest.mark.asyncio
    async def test_learned_max_weight_keeps_pre_learning_sum(self, plasticos) -> None:
        """learned=3.0 (plasticos max_weight) on every supply dimension keeps the C-22 sum."""
        supply_dims = [d for d in plasticos.scoring.dimensions if _SUPPLY in (d.matchdirections or [])]
        pre_sum = sum(d.defaultweight for d in supply_dims)
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, plasticos, {d.name: 3.0 for d in supply_dims})

        assembler = ScoringAssembler(plasticos, graph_driver=store)
        loaded = await assembler.load_learned_weights()
        assert set(loaded) == {d.name for d in supply_dims}
        clause, _ = assembler.assemble_scoring_clause(_SUPPLY, {})

        coeffs = _coefficients(clause)
        positive = sum(v for v in coeffs.values() if v > 0)
        assert positive == pytest.approx(pre_sum)
        assert positive <= 1.0 + 1e-9
        # Equal multipliers leave every coefficient at its spec weight.
        for d in supply_dims:
            assert coeffs[d.name] == pytest.approx(d.defaultweight)

    @pytest.mark.asyncio
    async def test_unequal_multipliers_redistribute_within_the_sum(self, plasticos) -> None:
        supply_dims = [d for d in plasticos.scoring.dimensions if _SUPPLY in (d.matchdirections or [])]
        pre_sum = sum(d.defaultweight for d in supply_dims)
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, plasticos, {"geo_proximity": 3.0})

        assembler = ScoringAssembler(plasticos, graph_driver=store)
        await assembler.load_learned_weights()
        coeffs = _coefficients(assembler.assemble_scoring_clause(_SUPPLY, {})[0])

        assert sum(coeffs.values()) == pytest.approx(pre_sum)
        geo_share = coeffs["geo_proximity"] / sum(coeffs.values())
        assert geo_share > 0.25  # boosted relative to its 0.25 spec share

    @pytest.mark.asyncio
    async def test_zero_and_small_learned_weights_both_load(self) -> None:
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, spec, {"geo_proximity": 0.0, "credit_score": 0.2})

        weights = await ScoringAssembler(spec, graph_driver=store).load_learned_weights()

        assert weights == {"geo_proximity": 0.0, "credit_score": 0.2}

    @pytest.mark.asyncio
    async def test_learned_zero_takes_penalty_branch(self) -> None:
        """A loaded 0.0 is below penalty_threshold (0.5) -> -penalty_factor, per current design (DEC-09 A)."""
        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, spec, {"geo_proximity": 0.0})

        assembler = ScoringAssembler(spec, graph_driver=store)
        await assembler.load_learned_weights()
        coeffs = _coefficients(assembler.assemble_scoring_clause("any", {})[0])

        assert coeffs["geo_proximity"] == pytest.approx(-0.3)
        assert coeffs["credit_score"] == pytest.approx(0.15)  # no learned multiplier -> no rescale

    @pytest.mark.asyncio
    async def test_flag_off_restores_multiply_only_and_truthiness_filter(self, monkeypatch, plasticos) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "learned_weight_renormalization", False)
        supply_dims = [d for d in plasticos.scoring.dimensions if _SUPPLY in (d.matchdirections or [])]
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, plasticos, {d.name: 3.0 for d in supply_dims})
        assembler = ScoringAssembler(plasticos, graph_driver=store)
        await assembler.load_learned_weights()
        coeffs = _coefficients(assembler.assemble_scoring_clause(_SUPPLY, {})[0])
        assert sum(coeffs.values()) == pytest.approx(3.0 * sum(d.defaultweight for d in supply_dims))

        spec = _plasticos_like_spec()
        store = _PropertyLevelDimensionWeightStore()
        await _write_weights(store, spec, {"geo_proximity": 0.0, "credit_score": 0.2})
        assert await ScoringAssembler(spec, graph_driver=store).load_learned_weights() == {"credit_score": 0.2}
