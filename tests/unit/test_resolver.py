"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, resolver, entity-resolution]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for R8 entity resolution / semantic registry.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.schema import SemanticRegistrySpec
from engine.resolution.resolver import EntityResolver
from engine.resolution.similarity import SimilarityScorer


def _make_spec(
    *,
    threshold: float = 0.85,
    property_weight: float = 0.5,
    structural_weight: float = 0.3,
    behavioral_weight: float = 0.2,
    comparison_properties: list[str] | None = None,
    entity_labels: list[str] | None = None,
    max_candidates: int = 20,
) -> SemanticRegistrySpec:
    return SemanticRegistrySpec(
        enabled=True,
        entity_labels=entity_labels or ["Supplier"],
        similarity_threshold=threshold,
        property_weight=property_weight,
        structural_weight=structural_weight,
        behavioral_weight=behavioral_weight,
        comparison_properties=comparison_properties or ["name", "region"],
        max_candidates=max_candidates,
    )


def _mock_driver() -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    return driver


@pytest.mark.unit
class TestSimilarityScorer:
    """Test multi-signal similarity scoring."""

    @pytest.mark.asyncio
    async def test_property_similarity_identical(self) -> None:
        """Identical properties yield similarity of 1.0."""
        spec = _make_spec(property_weight=1.0, structural_weight=0.0, behavioral_weight=0.0)
        driver = _mock_driver()

        # Property query returns matching values
        driver.execute_query.side_effect = [
            [{"a_name": "Acme", "b_name": "Acme", "a_region": "US", "b_region": "US"}],
            [{"shared_count": 0, "union_count": 0}],  # structural
            [{"shared_count": 0, "union_count": 0}],  # behavioral
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        assert sim == 1.0

    @pytest.mark.asyncio
    async def test_property_similarity_none(self) -> None:
        """No matching properties yield similarity of 0.0."""
        spec = _make_spec(property_weight=1.0, structural_weight=0.0, behavioral_weight=0.0)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [{"a_name": "Acme", "b_name": "Globe", "a_region": "US", "b_region": "EU"}],
            [{"shared_count": 0, "union_count": 0}],
            [{"shared_count": 0, "union_count": 0}],
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        assert sim == 0.0

    @pytest.mark.asyncio
    async def test_structural_similarity(self) -> None:
        """Shared neighbors increase structural similarity."""
        spec = _make_spec(property_weight=0.0, structural_weight=1.0, behavioral_weight=0.0)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [{"a_name": "A", "b_name": "B", "a_region": "US", "b_region": "EU"}],  # property
            [{"shared_count": 5, "union_count": 10}],  # structural: 5/10 = 0.5
            [{"shared_count": 0, "union_count": 0}],  # behavioral
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        assert abs(sim - 0.5) < 0.01

    @pytest.mark.asyncio
    async def test_behavioral_similarity(self) -> None:
        """Shared transaction patterns increase behavioral similarity."""
        spec = _make_spec(property_weight=0.0, structural_weight=0.0, behavioral_weight=1.0)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [{"a_name": "A", "b_name": "B", "a_region": "US", "b_region": "EU"}],
            [{"shared_count": 0, "union_count": 0}],
            [{"shared_count": 3, "union_count": 6}],  # behavioral: 3/6 = 0.5
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        assert abs(sim - 0.5) < 0.01

    @pytest.mark.asyncio
    async def test_combined_score_weighted(self) -> None:
        """Final score correctly weights three signals."""
        spec = _make_spec(property_weight=0.5, structural_weight=0.3, behavioral_weight=0.2)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [{"a_name": "Acme", "b_name": "Acme", "a_region": "US", "b_region": "EU"}],  # 1/2 = 0.5
            [{"shared_count": 4, "union_count": 8}],  # 4/8 = 0.5
            [{"shared_count": 2, "union_count": 10}],  # 2/10 = 0.2
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        # 0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.2 = 0.25 + 0.15 + 0.04 = 0.44
        assert abs(sim - 0.44) < 0.01

    @pytest.mark.asyncio
    async def test_score_clamped_to_0_1(self) -> None:
        """Score is clamped between 0.0 and 1.0."""
        spec = _make_spec(property_weight=1.0, structural_weight=0.0, behavioral_weight=0.0)
        driver = _mock_driver()

        driver.execute_query.side_effect = [
            [{"a_name": "A", "b_name": "A", "a_region": "US", "b_region": "US"}],
            [{"shared_count": 0, "union_count": 0}],
            [{"shared_count": 0, "union_count": 0}],
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        sim = await scorer.compute_similarity("e1", "e2", "Supplier")

        assert 0.0 <= sim <= 1.0

    @pytest.mark.asyncio
    async def test_find_candidates_filters_by_threshold(self) -> None:
        """find_candidates only returns entities above threshold."""
        spec = _make_spec(threshold=0.6)
        driver = _mock_driver()

        # find_property_candidates returns 2 candidates
        driver.execute_query.side_effect = [
            [{"entity_id": "e2"}, {"entity_id": "e3"}],  # property candidates
            # e2 similarity computation (high)
            [{"a_name": "Acme", "b_name": "Acme", "a_region": "US", "b_region": "US"}],
            [{"shared_count": 5, "union_count": 5}],
            [{"shared_count": 2, "union_count": 4}],
            # e3 similarity computation (low)
            [{"a_name": "Acme", "b_name": "Globe", "a_region": "US", "b_region": "EU"}],
            [{"shared_count": 0, "union_count": 5}],
            [{"shared_count": 0, "union_count": 4}],
        ]

        scorer = SimilarityScorer(spec, driver, "test_domain")
        candidates = await scorer.find_candidates("e1", "Supplier")

        # e2 should pass (high similarity), e3 should not (low similarity)
        assert len(candidates) >= 1
        assert all(c["similarity"] >= 0.6 for c in candidates)


@pytest.mark.unit
class TestEntityResolver:
    """Test entity resolver."""

    @pytest.mark.asyncio
    async def test_resolve_entity_no_duplicates(self) -> None:
        """When no duplicates found, returns original entity as canonical."""
        spec = _make_spec()
        driver = _mock_driver()

        # find_candidates returns empty
        driver.execute_query.side_effect = [
            [],  # find_property_candidates returns nothing
        ]

        resolver = EntityResolver(spec, driver, "test_domain")
        result = await resolver.resolve_entity("e1", "Supplier")

        assert result["canonical_id"] == "e1"
        assert result["merged_count"] == 0
        assert result["resolution_ids"] == []

    @pytest.mark.asyncio
    async def test_resolve_entity_with_duplicate(self) -> None:
        """Duplicate found → creates RESOLVED_FROM edge to canonical."""
        spec = _make_spec(threshold=0.8)
        driver = _mock_driver()

        # find_candidates → find_property_candidates
        # Then compute_similarity for each
        driver.execute_query.side_effect = [
            # find_property_candidates
            [{"entity_id": "e2"}],
            # compute_similarity for e2 (all high)
            [{"a_name": "Acme", "b_name": "Acme", "a_region": "US", "b_region": "US"}],
            [{"shared_count": 8, "union_count": 10}],
            [{"shared_count": 3, "union_count": 4}],
            # RU-101: already-resolved lookup over e1 + candidates (none resolved)
            [],
            # find_canonical: e1 has more connections
            [{"entity_id": "e1"}],
            # create_resolution (RU-101: MERGE returns the edge's id and whether it is new)
            [{"resolution_id": "res_000000000001", "created": True}],
        ]

        resolver = EntityResolver(spec, driver, "test_domain")
        result = await resolver.resolve_entity("e1", "Supplier")

        assert result["canonical_id"] == "e1"
        assert result["merged_count"] == 1
        assert len(result["resolution_ids"]) == 1

    @pytest.mark.asyncio
    async def test_resolve_batch_summary(self) -> None:
        """Batch resolution returns summary stats."""
        spec = _make_spec(threshold=0.5)
        driver = _mock_driver()

        # First call: list all entities
        # Second+: resolve each
        driver.execute_query.side_effect = [
            # list entities
            [{"entity_id": "e1"}, {"entity_id": "e2"}],
            # resolve e1: find_property_candidates returns nothing
            [],
            # resolve e2: find_property_candidates returns nothing
            [],
        ]

        resolver = EntityResolver(spec, driver, "test_domain")
        result = await resolver.resolve_batch("Supplier")

        assert result["total_entities"] == 2
        assert result["total_merged"] == 0
        assert result["resolution_groups"] == 0


# ── RU-101 / CEG-COR-059..061: merge integrity ─────────────────────────────


class _FakeGraph:
    """In-memory emulation of the resolver's Cypher (RESOLVED_FROM bookkeeping).

    Dispatches on the shape of each statement the resolver sends: the batch
    entity listing, the already-resolved lookup, canonical selection, and the
    RESOLVED_FROM write (CREATE, or MERGE + ON CREATE SET).
    """

    def __init__(self, entities: dict[str, int]) -> None:
        # entity_id -> number of non-RESOLVED_FROM relationships
        self.other_degree = dict(entities)
        self.edges: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.writes: list[tuple[str, str]] = []

    def _has_outgoing(self, eid: str) -> bool:
        return any(key[0] == eid for key in self.edges)

    def _rf_degree(self, eid: str) -> int:
        return sum(1 for key in self.edges if eid in key[:2])

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        assert database == "test_domain"
        if "RESOLVED_FROM" in cypher and ("CREATE (source)" in cypher or "MERGE (source)" in cypher):
            key: tuple[Any, ...] = (parameters["source_id"], parameters["canonical_id"])
            self.writes.append(key)
            if "MERGE (source)" in cypher and key in self.edges:
                existing = self.edges[key]["resolution_id"]
                return [{"resolution_id": existing, "created": existing == parameters["resolution_id"]}]
            if "MERGE (source)" not in cypher:  # legacy CREATE: a duplicate edge is a new edge
                key = (*key, len(self.edges))
            self.edges[key] = {"resolution_id": parameters["resolution_id"], "confidence": parameters["similarity"]}
            return [{"resolution_id": parameters["resolution_id"], "created": True}]
        if "AS resolved_id" in cypher:
            return [{"resolved_id": eid} for eid in parameters["entity_ids"] if self._has_outgoing(eid)]
        if "ORDER BY degree DESC" in cypher:
            ids = list(parameters["entity_ids"])
            if "NOT EXISTS" in cypher:
                ids = [eid for eid in ids if not self._has_outgoing(eid)]
            exclude_rf = "type(r) <> 'RESOLVED_FROM'" in cypher
            degree = {eid: self.other_degree[eid] + (0 if exclude_rf else self._rf_degree(eid)) for eid in ids}
            if "entity_id ASC" in cypher:
                ranked = sorted(ids, key=lambda eid: (-degree[eid], eid))
            else:  # unspecified order among ties: emulate as reverse input order
                position = {eid: idx for idx, eid in enumerate(ids)}
                ranked = sorted(ids, key=lambda eid: (-degree[eid], -position[eid]))
            return [{"entity_id": ranked[0]}] if ranked else []
        if "RETURN e.entity_id AS entity_id" in cypher:
            return [{"entity_id": eid} for eid in self.other_degree if not self._has_outgoing(eid)]
        raise AssertionError(f"unexpected Cypher: {cypher}")

    def resolved_from(self) -> list[tuple[str, str, float]]:
        return [(key[0], key[1], props["confidence"]) for key, props in self.edges.items()]


class _FakeScorer:
    """Similarity from a fixed matrix; find_candidates honours the threshold."""

    def __init__(self, graph: _FakeGraph, sims: dict[frozenset[str], float], default_threshold: float) -> None:
        self._graph = graph
        self._sims = sims
        self._default = default_threshold
        self.find_thresholds: list[float | None] = []

    async def compute_similarity(self, a: str, b: str, entity_label: str) -> float:
        return self._sims.get(frozenset((a, b)), 0.0)

    async def find_candidates(
        self, entity_id: str, entity_label: str, threshold: float | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self.find_thresholds.append(threshold)
        effective = threshold if threshold is not None else self._default
        out = [
            {"entity_id": other, "similarity": round(self._sims.get(frozenset((entity_id, other)), 0.0), 4)}
            for other in self._graph.other_degree
            if other != entity_id and self._sims.get(frozenset((entity_id, other)), 0.0) >= effective
        ]
        out.sort(key=lambda c: -c["similarity"])
        return out


def _resolver(
    entities: dict[str, int], sims: dict[tuple[str, str], float], threshold: float = 0.6
) -> tuple[EntityResolver, _FakeGraph, _FakeScorer]:
    graph = _FakeGraph(entities)
    resolver = EntityResolver(_make_spec(threshold=threshold), graph, "test_domain")  # type: ignore[arg-type]
    scorer = _FakeScorer(graph, {frozenset(k): v for k, v in sims.items()}, threshold)
    resolver._scorer = scorer  # type: ignore[assignment]
    return resolver, graph, scorer


_IDENTICAL = {("e1", "e2"): 1.0, ("e1", "e3"): 1.0, ("e2", "e3"): 1.0}


@pytest.mark.unit
class TestMergeIntegrity:
    @pytest.mark.asyncio
    async def test_batch_of_three_identical_entities_creates_two_edges(self) -> None:
        resolver, graph, _ = _resolver({"e1": 0, "e2": 0, "e3": 0}, _IDENTICAL)
        result = await resolver.resolve_batch("Supplier")
        assert sorted((s, c) for s, c, _ in graph.resolved_from()) == [("e2", "e1"), ("e3", "e1")]
        assert result["total_merged"] == 2
        assert result["resolution_groups"] == 1
        assert result["total_entities"] == 3

    @pytest.mark.asyncio
    async def test_rerunning_the_batch_creates_nothing(self) -> None:
        resolver, graph, _ = _resolver({"e1": 0, "e2": 0, "e3": 0}, _IDENTICAL)
        await resolver.resolve_batch("Supplier")
        edges_before = graph.resolved_from()
        again = await resolver.resolve_batch("Supplier")
        assert graph.resolved_from() == edges_before
        assert again["total_merged"] == 0
        assert again["resolution_groups"] == 0

    @pytest.mark.asyncio
    async def test_degree_tie_never_forms_a_cycle(self) -> None:
        resolver, graph, _ = _resolver({"e1": 2, "e2": 2}, {("e1", "e2"): 0.9})
        await resolver.resolve_entity("e1", "Supplier")
        await resolver.resolve_entity("e2", "Supplier")
        edges = {(s, c) for s, c, _ in graph.resolved_from()}
        assert edges == {("e2", "e1")}  # deterministic tie-break: entity_id ASC
        assert not any((c, s) in edges for s, c in edges)

    @pytest.mark.asyncio
    async def test_batch_threshold_override_is_honoured(self) -> None:
        sims = {("e1", "e2"): 0.75, ("e1", "e3"): 0.75, ("e2", "e3"): 0.75}
        resolver, graph, scorer = _resolver({"e1": 0, "e2": 0, "e3": 0}, sims)
        result = await resolver.resolve_batch("Supplier", threshold=0.99)
        assert graph.resolved_from() == []
        assert result["total_merged"] == 0
        assert scorer.find_thresholds
        assert all(t == 0.99 for t in scorer.find_thresholds)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["x", 1.5, -0.1, True, float("nan")])
    async def test_invalid_batch_threshold_raises(self, bad: object) -> None:
        resolver, graph, _ = _resolver({"e1": 0, "e2": 0}, {("e1", "e2"): 1.0})
        with pytest.raises(ValueError, match="threshold"):
            await resolver.resolve_batch("Supplier", threshold=bad)  # type: ignore[arg-type]
        assert graph.writes == []

    @pytest.mark.asyncio
    async def test_members_are_verified_against_the_canonical(self) -> None:
        """Scenario 3: e2 (higher degree) becomes canonical for e1's group.

        e3 is similar to e1 (0.9) but only 0.33 to the canonical, so it is not
        merged; e1 -> e2 carries the measured 0.67, not a default 1.0.
        """
        sims = {("e1", "e2"): 0.67, ("e1", "e3"): 0.9, ("e2", "e3"): 0.33}
        resolver, graph, _ = _resolver({"e1": 0, "e2": 5, "e3": 0}, sims, threshold=0.6)
        result = await resolver.resolve_entity("e1", "Supplier")
        assert graph.resolved_from() == [("e1", "e2", 0.67)]
        assert result["canonical_id"] == "e2"
        assert result["merged_count"] == 1
        assert result["merged_entity_ids"] == ["e1"]
        assert len(result["resolution_ids"]) == 1

    @pytest.mark.asyncio
    async def test_already_resolved_candidates_are_excluded(self) -> None:
        resolver, graph, _ = _resolver({"e1": 0, "e2": 0, "e3": 9}, _IDENTICAL)
        await resolver.resolve_entity("e2", "Supplier")  # e2 -> e3, e1 -> e3
        assert {(s, c) for s, c, _ in graph.resolved_from()} == {("e1", "e3"), ("e2", "e3")}
        result = await resolver.resolve_entity("e1", "Supplier")  # e1 already resolved
        assert result["merged_count"] == 0
        assert len(graph.resolved_from()) == 2

    @pytest.mark.asyncio
    async def test_flag_off_restores_legacy_merge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "entity_resolution_merge_integrity", False)
        resolver, graph, scorer = _resolver({"e1": 0, "e2": 0, "e3": 0}, _IDENTICAL)
        result = await resolver.resolve_batch("Supplier", threshold=0.99)
        # Legacy: threshold ignored, CREATE per call, resolution ids in the skip set.
        assert result["total_merged"] == 6
        assert len(graph.resolved_from()) == 6
        assert all(t is None for t in scorer.find_thresholds)

    def test_flag_default_on(self) -> None:
        from engine.config.settings import Settings

        assert Settings().entity_resolution_merge_integrity is True
