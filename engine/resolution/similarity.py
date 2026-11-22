"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [resolution]
tags: [resolution, similarity, jaccard, structural]
owner: engine-team
status: active
--- /L9_META ---

Multi-signal entity similarity scoring.
Combines property (Jaccard), structural (neighbor overlap),
and behavioral (outcome pattern) signals inspired by OmniSage.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.outcome_vocabulary import declared_outcome_node_label
from engine.config.schema import DomainSpec, SemanticRegistrySpec
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

_FALLBACK_ID_PROPERTY = "entity_id"


def declared_id_property(domain_spec: DomainSpec | None, label: str) -> str:
    """The property that identifies a node of ``label``, as the domain declares it.

    Same rule the match path applies to candidates (CEG-COR-002 / PO-04): the
    sync endpoint's ``idproperty`` for this label (the key the engine MERGEs
    on), else the node's first ``required`` property, else ``entity_id``.
    ``spec_declared_candidate_id`` off, or no domain spec, keeps ``entity_id``.
    """
    from engine.config.settings import settings

    if domain_spec is None or not settings.spec_declared_candidate_id:
        return _FALLBACK_ID_PROPERTY
    if domain_spec.sync:
        for endpoint in domain_spec.sync.endpoints:
            if endpoint.targetnode == label and endpoint.idproperty:
                return endpoint.idproperty
    for node in domain_spec.ontology.nodes:
        if node.label == label:
            for prop in node.properties:
                if prop.required:
                    return prop.name
    return _FALLBACK_ID_PROPERTY


class SimilarityScorer:
    """Multi-signal entity similarity scorer.

    Three similarity signals combined:
    1. Property similarity (entity-feature): Jaccard for categorical properties
    2. Structural similarity (entity-entity): shared neighbor count
    3. Behavioral similarity (user-entity): shared outcome patterns over the
       domain's declared ``feedbackloop.outcome_node_label``

    Every node is addressed by the label's declared id property
    (:func:`declared_id_property`), never by a fixed ``entity_id``.

    Final score = alpha * property_sim + beta * structural_sim + gamma * behavioral_sim
    """

    def __init__(
        self,
        registry_spec: SemanticRegistrySpec,
        graph_driver: GraphDriver,
        domain_id: str,
        domain_spec: DomainSpec | None = None,
    ) -> None:
        self._spec = registry_spec
        self._graph_driver = graph_driver
        self._db = domain_id
        # RU-103 / PO-04: the domain spec declares each label's id property.
        # None: resolved on first use from the engine's domain loader (the
        # tenant id is the domain id), else ``entity_id``.
        self._domain_spec = domain_spec

    async def compute_similarity(
        self,
        entity_a_id: str,
        entity_b_id: str,
        entity_label: str,
    ) -> float:
        """Compute weighted multi-signal similarity between two entities.

        Args:
            entity_a_id: First entity ID.
            entity_b_id: Second entity ID.
            entity_label: The Neo4j label of both entities.

        Returns:
            Weighted similarity score in [0, 1].
        """
        label = sanitize_label(entity_label)

        property_sim = await self._property_similarity(entity_a_id, entity_b_id, label)
        structural_sim = await self._structural_similarity(entity_a_id, entity_b_id, label)
        behavioral_sim = await self._behavioral_similarity(entity_a_id, entity_b_id, label)

        score = (
            self._spec.property_weight * property_sim
            + self._spec.structural_weight * structural_sim
            + self._spec.behavioral_weight * behavioral_sim
        )
        return min(1.0, max(0.0, score))

    async def find_candidates(
        self,
        entity_id: str,
        entity_label: str,
        threshold: float | None = None,
        limit: int | None = None,
        exclude_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        """Find entities similar to the given entity.

        Uses property comparison as initial candidate filter,
        then computes full multi-signal similarity.

        Args:
            entity_id: The entity to find matches for.
            entity_label: Neo4j label of the entity.
            threshold: Minimum similarity score (defaults to spec threshold).
            limit: Max candidates to return (defaults to spec max_candidates).
            exclude_resolved: Leave out nodes that already carry an outgoing
                RESOLVED_FROM before the capped read (the resolver's
                eligibility rule), so they never take a candidate slot.

        Returns:
            List of dicts with entity_id and similarity score.
        """
        effective_threshold = threshold if threshold is not None else self._spec.similarity_threshold
        effective_limit = limit if limit is not None else self._spec.max_candidates
        label = sanitize_label(entity_label)

        # Phase 1: find candidate pairs via property overlap
        candidate_ids = await self._find_property_candidates(
            entity_id, label, effective_limit, exclude_resolved=exclude_resolved
        )

        # Phase 2: compute full similarity for each candidate
        results: list[dict[str, Any]] = []
        for cid in candidate_ids:
            if cid == entity_id:
                continue
            sim = await self.compute_similarity(entity_id, cid, entity_label)
            if sim >= effective_threshold:
                results.append({"entity_id": cid, "similarity": round(sim, 4)})

        # Ties break on the candidate id so a capped result never depends on
        # graph insertion order (R14).
        results.sort(key=lambda x: (-x["similarity"], str(x["entity_id"])))
        return results[:effective_limit]

    async def _property_similarity(
        self,
        entity_a_id: str,
        entity_b_id: str,
        label: str,
    ) -> float:
        """Jaccard similarity over comparison properties."""
        # Validated again here so this helper is safe on its own, not only via its callers (C-009).
        label = sanitize_label(label)
        comp_props = self._spec.comparison_properties
        if not comp_props:
            return 0.0

        safe_props = [sanitize_label(p) for p in comp_props]
        id_prop = sanitize_label(self.id_property(label))
        return_clauses = ", ".join(f"a.{p} AS a_{p}, b.{p} AS b_{p}" for p in safe_props)

        cypher = f"""
        MATCH (a:{label} {{{id_prop}: $a_id}})
        MATCH (b:{label} {{{id_prop}: $b_id}})
        RETURN {return_clauses}
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"a_id": entity_a_id, "b_id": entity_b_id},
            database=self._db,
        )
        if not results:
            return 0.0

        row = results[0]
        matches = 0
        total = 0
        for p in safe_props:
            val_a = row.get(f"a_{p}")
            val_b = row.get(f"b_{p}")
            if val_a is not None and val_b is not None:
                total += 1
                if val_a == val_b:
                    matches += 1

        return matches / total if total > 0 else 0.0

    async def _structural_similarity(
        self,
        entity_a_id: str,
        entity_b_id: str,
        label: str,
    ) -> float:
        """Shared neighbor overlap (Jaccard on neighbor sets)."""
        # Validated again here so this helper is safe on its own, not only via its callers (C-009).
        label = sanitize_label(label)
        id_prop = sanitize_label(self.id_property(label))
        cypher = f"""
        MATCH (a:{label} {{{id_prop}: $a_id}})--(neighbor_a)
        WITH a, collect(DISTINCT id(neighbor_a)) AS neighbors_a
        MATCH (b:{label} {{{id_prop}: $b_id}})--(neighbor_b)
        WITH neighbors_a, collect(DISTINCT id(neighbor_b)) AS neighbors_b
        WITH neighbors_a, neighbors_b,
             [x IN neighbors_a WHERE x IN neighbors_b] AS shared
        RETURN size(shared) AS shared_count,
               size(neighbors_a) + size(neighbors_b) - size(shared) AS union_count
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"a_id": entity_a_id, "b_id": entity_b_id},
            database=self._db,
        )
        if not results:
            return 0.0
        row = results[0]
        shared = row.get("shared_count", 0)
        union = row.get("union_count", 0)
        return shared / union if union > 0 else 0.0

    def _resolved_domain_spec(self) -> DomainSpec | None:
        if self._domain_spec is None:
            from engine.state import get_state

            state = get_state()
            if state.is_initialized:
                self._domain_spec = state.domain_loader.load_domain(self._db)
        return self._domain_spec

    def id_property(self, label: str) -> str:
        """The sanitized id property of ``label`` (F01): declared, else ``entity_id``."""
        return sanitize_label(declared_id_property(self._resolved_domain_spec(), sanitize_label(label)))

    async def _behavioral_similarity(
        self,
        entity_a_id: str,
        entity_b_id: str,
        label: str | None = None,
    ) -> float:
        """Shared outcome pattern similarity.

        RU-103 / CEG-COR-177: both entities are matched with ``label`` and the
        label's declared id property, so a same-id node of another label never
        contributes outcomes. The outcome nodes are the domain's declared
        ``feedbackloop.outcome_node_label`` (F02), never a fixed label. A
        domain that declares none (or no domain spec) has no observable
        behavioral evidence: the signal is 0.0 and no outcome is guessed (R15).
        ``resolution_behavioral_label_scoped`` off (or no label) restores the
        label-less ``entity_id`` / ``TransactionOutcome`` match.
        """
        from engine.config.settings import settings

        if label is not None and settings.resolution_behavioral_label_scoped:
            safe_label = sanitize_label(label)
            id_prop = sanitize_label(self.id_property(safe_label))
            declared = declared_outcome_node_label(self._resolved_domain_spec())
            if declared is None:
                logger.debug("Behavioral similarity unavailable for %s: no declared outcome node label", self._db)
                return 0.0
            outcome_label = sanitize_label(declared)
            cypher = f"""
        OPTIONAL MATCH (a:{safe_label} {{{id_prop}: $a_id}})-[:RESULTED_IN]->(oa:{outcome_label})
        WITH collect(DISTINCT oa.match_id) AS matches_a
        OPTIONAL MATCH (b:{safe_label} {{{id_prop}: $b_id}})-[:RESULTED_IN]->(ob:{outcome_label})
        WITH matches_a, collect(DISTINCT ob.match_id) AS matches_b
        WITH matches_a, matches_b,
             [x IN matches_a WHERE x IN matches_b] AS shared
        RETURN size(shared) AS shared_count,
               size(matches_a) + size(matches_b) - size(shared) AS union_count
        """
            return await self._shared_outcome_ratio(cypher, entity_a_id, entity_b_id)
        cypher = """
        OPTIONAL MATCH (a {entity_id: $a_id})-[:RESULTED_IN]->(oa:TransactionOutcome)
        WITH collect(DISTINCT oa.match_id) AS matches_a
        OPTIONAL MATCH (b {entity_id: $b_id})-[:RESULTED_IN]->(ob:TransactionOutcome)
        WITH matches_a, collect(DISTINCT ob.match_id) AS matches_b
        WITH matches_a, matches_b,
             [x IN matches_a WHERE x IN matches_b] AS shared
        RETURN size(shared) AS shared_count,
               size(matches_a) + size(matches_b) - size(shared) AS union_count
        """
        return await self._shared_outcome_ratio(cypher, entity_a_id, entity_b_id)

    async def _shared_outcome_ratio(self, cypher: str, entity_a_id: str, entity_b_id: str) -> float:
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"a_id": entity_a_id, "b_id": entity_b_id},
            database=self._db,
        )
        if not results:
            return 0.0
        row = results[0]
        shared = row.get("shared_count", 0)
        union = row.get("union_count", 0)
        return shared / union if union > 0 else 0.0

    async def _find_property_candidates(
        self,
        entity_id: str,
        label: str,
        limit: int,
        exclude_resolved: bool = False,
    ) -> list[Any]:
        """Find candidate entity IDs that share at least one comparison property value.

        F03: any declared comparison property qualifies a candidate, not only
        the first, so a candidate the full scorer would accept is not dropped
        by the prefilter. The read stays capped at ``limit``; candidates
        sharing more comparison values come first, ties by id, so the capped
        subset is deterministic (R14). Nodes without the declared id are never
        candidates. ``exclude_resolved`` drops already-resolved nodes before the
        cap. ``resolution_prefilter_recall_enabled`` off restores the previous
        first-property prefilter.
        """
        from engine.config.settings import settings

        # Validated again here so this helper is safe on its own, not only via its callers (C-009).
        label = sanitize_label(label)
        id_prop = sanitize_label(self.id_property(label))
        comp_props = self._spec.comparison_properties
        if not settings.resolution_prefilter_recall_enabled:
            return await self._find_first_property_candidates(entity_id, label, id_prop, limit)
        unresolved = " AND NOT EXISTS { (candidate)-[:RESOLVED_FROM]->() }" if exclude_resolved else ""
        if not comp_props:
            # Fallback: return all entities of the same label
            cypher = f"""
            MATCH (candidate:{label})
            WHERE candidate.{id_prop} IS NOT NULL AND candidate.{id_prop} <> $entity_id{unresolved}
            RETURN candidate.{id_prop} AS entity_id
            ORDER BY entity_id
            LIMIT $limit
            """
            results = await self._graph_driver.execute_query(
                cypher=cypher,
                parameters={"entity_id": entity_id, "limit": limit},
                database=self._db,
            )
            return [r["entity_id"] for r in results if r.get("entity_id") is not None]

        safe_props = [sanitize_label(p) for p in comp_props]
        shared_terms = [f"(source.{p} IS NOT NULL AND candidate.{p} = source.{p})" for p in safe_props]
        shared_count = " + ".join(f"CASE WHEN {term} THEN 1 ELSE 0 END" for term in shared_terms)
        cypher = f"""
        MATCH (source:{label} {{{id_prop}: $entity_id}})
        MATCH (candidate:{label})
        WHERE candidate.{id_prop} IS NOT NULL
              AND candidate.{id_prop} <> $entity_id{unresolved}
              AND ({" OR ".join(shared_terms)})
        WITH candidate.{id_prop} AS entity_id, {shared_count} AS shared
        RETURN entity_id
        ORDER BY shared DESC, entity_id ASC
        LIMIT $limit
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"entity_id": entity_id, "limit": limit},
            database=self._db,
        )
        return [r["entity_id"] for r in results if r.get("entity_id") is not None]

    async def _find_first_property_candidates(self, entity_id: Any, label: str, id_prop: str, limit: int) -> list[Any]:
        """The previous prefilter (``resolution_prefilter_recall_enabled`` off): first comparison property only."""
        label = sanitize_label(label)
        id_prop = sanitize_label(id_prop)
        comp_props = self._spec.comparison_properties
        if not comp_props:
            cypher = f"""
            MATCH (e:{label})
            WHERE e.{id_prop} <> $entity_id
            RETURN e.{id_prop} AS entity_id
            LIMIT $limit
            """
        else:
            safe_prop = sanitize_label(comp_props[0])
            cypher = f"""
            MATCH (source:{label} {{{id_prop}: $entity_id}})
            MATCH (candidate:{label})
            WHERE candidate.{id_prop} <> $entity_id
                  AND candidate.{safe_prop} = source.{safe_prop}
            RETURN candidate.{id_prop} AS entity_id
            LIMIT $limit
            """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"entity_id": entity_id, "limit": limit},
            database=self._db,
        )
        return [r["entity_id"] for r in results if r.get("entity_id")]
