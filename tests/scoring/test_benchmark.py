"""
Score quality benchmark suite (W5-03).

Asserts the Cypher the scoring assembler emits for the benchmark spec (no
Neo4j). The former Python re-implementation of the scoring math was removed
(CEG scoring audit L2): it tested itself, not the engine, and bounded scores
at 1.5 although the engine clamps to [0, 1].
"""

from __future__ import annotations


import pytest

from engine.config.schema import (
    ComputationType,
    DomainMetadata,
    DomainSpec,
    EdgeCategory,
    EdgeDirection,
    EdgeSpec,
    ManagedByType,
    MatchEntitiesSpec,
    MatchEntitySpec,
    NodeSpec,
    OntologySpec,
    PropertySpec,
    PropertyType,
    QueryFieldSpec,
    QuerySchemaSpec,
    ScoringDimensionSpec,
    ScoringSource,
    ScoringSpec,
)
from engine.scoring.assembler import ScoringAssembler
from tests.fixtures.benchmark_data import BAD_PAIRS, GOOD_PAIRS

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_benchmark_spec() -> DomainSpec:
    """Build a DomainSpec with scoring dimensions matching benchmark data."""
    nodes = [
        NodeSpec(
            label="Facility",
            candidate=True,
            properties=[
                PropertySpec(name="lat", type=PropertyType.FLOAT),
                PropertySpec(name="lon", type=PropertyType.FLOAT),
                PropertySpec(name="rate", type=PropertyType.FLOAT),
                PropertySpec(name="community_id", type=PropertyType.INT),
                PropertySpec(name="density", type=PropertyType.FLOAT),
                PropertySpec(name="active", type=PropertyType.BOOL),
            ],
        ),
        NodeSpec(label="Query", queryentity=True),
    ]
    edges = [
        EdgeSpec(
            type="PROCESSES",
            **{"from": "Facility"},
            to="Query",
            direction=EdgeDirection.DIRECTED,
            category=EdgeCategory.CAPABILITY,
            managedby=ManagedByType.SYNC,
        ),
    ]
    scoring = ScoringSpec(
        dimensions=[
            ScoringDimensionSpec(
                name="geo_proximity",
                source=ScoringSource.COMPUTED,
                computation=ComputationType.GEODECAY,
                candidateprop="lat",
                queryprop="lat",
                candidateprop_lon="lon",
                decayconstant=50000.0,
                weightkey="w_geo",
                defaultweight=0.30,
            ),
            ScoringDimensionSpec(
                name="structural",
                source=ScoringSource.CANDIDATEPROPERTY,
                computation=ComputationType.CANDIDATEPROPERTY,
                candidateprop="rate",
                weightkey="w_structural",
                defaultweight=0.30,
            ),
            ScoringDimensionSpec(
                name="community",
                source=ScoringSource.COMPUTED,
                computation=ComputationType.COMMUNITYMATCH,
                candidateprop="community_id",
                queryprop="community_id",
                bias=1.5,
                weightkey="w_community",
                defaultweight=0.20,
            ),
            ScoringDimensionSpec(
                name="density_score",
                source=ScoringSource.CANDIDATEPROPERTY,
                computation=ComputationType.LOGNORMALIZED,
                candidateprop="density",
                maxvalue=100.0,
                weightkey="w_density",
                defaultweight=0.20,
            ),
        ]
    )

    return DomainSpec(
        domain=DomainMetadata(id="benchmark", name="Benchmark", version="1.0.0"),
        ontology=OntologySpec(nodes=nodes, edges=edges),
        matchentities=MatchEntitiesSpec(
            candidate=[MatchEntitySpec(label="Facility", matchdirection="buyer_to_seller")],
            queryentity=[MatchEntitySpec(label="Query", matchdirection="buyer_to_seller")],
        ),
        queryschema=QuerySchemaSpec(
            matchdirections=["buyer_to_seller"],
            fields=[
                QueryFieldSpec(name="lat", type=PropertyType.FLOAT),
                QueryFieldSpec(name="lon", type=PropertyType.FLOAT),
                QueryFieldSpec(name="community_id", type=PropertyType.INT),
            ],
        ),
        gates=[],
        scoring=scoring,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScoreBenchmark:
    """Score quality benchmark using synthetic data."""

    @pytest.fixture
    def spec(self):
        return _build_benchmark_spec()

    def test_assembler_produces_clause(self, spec):
        """Assembler generates a non-empty scoring clause."""
        assembler = ScoringAssembler(spec)
        clause, _ = assembler.assemble_scoring_clause("buyer_to_seller", {})
        assert "WITH" in clause
        assert "score" in clause

    def test_every_dimension_and_the_composite_are_clamped(self, spec):
        """W1-02 + D5: each dimension and the weighted total are bounded to [0, 1] in the emitted Cypher.

        The declared communitymatch bias (1.5) exceeds 1.0; the dimension clamp
        cuts it, so no candidate's score can exceed 1.0. The former test-local
        re-implementation of the scoring math allowed scores up to 1.5.
        """
        assembler = ScoringAssembler(spec)
        clause, _ = assembler.assemble_scoring_clause("buyer_to_seller", {})
        dims_line, score_line = clause.splitlines()
        for name in ("geo_proximity", "structural", "community", "density_score"):
            assert f" END AS {name}" in dims_line
        assert dims_line.count("WHEN isNaN(") == 4
        weighted = "(0.3 * geo_proximity) + (0.3 * structural) + (0.2 * community) + (0.2 * density_score)"
        assert score_line.endswith(f"{ScoringAssembler._clamp_expression(weighted)} AS score")

    def test_benchmark_pairs_reference_only_declared_properties(self, spec):
        """The synthetic pairs exercise the declared candidate properties and query fields."""
        declared = {p.name for n in spec.ontology.nodes for p in n.properties}
        fields = {f.name for f in spec.queryschema.fields}
        for pair in GOOD_PAIRS + BAD_PAIRS:
            assert set(pair.candidate_props) <= declared, pair.label
            assert set(pair.query_params) <= fields, pair.label

    def test_clamp_expression(self):
        """ScoringAssembler._clamp_expression wraps correctly."""
        result = ScoringAssembler._clamp_expression("candidate.rate")
        assert "CASE" in result
        assert "0.0" in result
        assert "1.0" in result

    def test_assembler_active_dimensions(self, spec):
        """Assembler tracks which dimensions are active."""
        assembler = ScoringAssembler(spec)
        assembler.assemble_scoring_clause("buyer_to_seller", {})
        active = assembler.last_active_dimension_names
        assert len(active) == 4
        assert "geo_proximity" in active
        assert "structural" in active
