"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [resolution]
tags: [resolution, entity-resolver, deduplication]
owner: engine-team
status: active
--- /L9_META ---

Entity resolver: finds duplicates above the similarity threshold, picks a
canonical entity and links each duplicate to it with a
``(duplicate)-[:RESOLVED_FROM]->(canonical)`` edge.

Resolution is linkage only (RU-102 / CEG-COR-124, DEC-40 option A): no
relationships are moved from a duplicate to its canonical, no node is deleted,
and no engine match or scoring query consults RESOLVED_FROM, so a resolved
duplicate remains a match candidate. The resolver itself reads the edge only
to skip already-resolved entities.

Every read and write addresses nodes by the label's declared id property
(``SimilarityScorer.id_property`` / ``declared_id_property``; F01), the same key
the scorer uses, so a domain keyed by anything other than ``entity_id`` resolves
on its own identity.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from engine.config.schema import DomainSpec, SemanticRegistrySpec
from engine.graph.driver import GraphDriver
from engine.resolution.similarity import SimilarityScorer
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)


def _merge_integrity() -> bool:
    """RU-101 / CEG-COR-059..061 flag: threshold threading, canonical-relative
    verification, idempotent RESOLVED_FROM edges and batch skip by entity id."""
    from engine.config.settings import settings

    return settings.entity_resolution_merge_integrity


def _validate_threshold(threshold: Any) -> float | None:
    """A similarity-threshold override is a number in [0, 1], or None."""
    if threshold is None:
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not math.isfinite(threshold):
        msg = f"threshold must be a number in [0, 1], got {threshold!r}"
        raise ValueError(msg)
    if not 0.0 <= threshold <= 1.0:
        msg = f"threshold must be a number in [0, 1], got {threshold!r}"
        raise ValueError(msg)
    return threshold


class EntityResolver:
    """Resolves duplicate entities into canonical records.

    Process:
    1. For a given entity, find candidates above similarity threshold
    2. Pick canonical entity (highest degree node)
    3. Create RESOLVED_FROM edges from each duplicate to the canonical

    ``merged_count`` / ``total_merged`` count the RESOLVED_FROM links created;
    the duplicates' own relationships are left in place.
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
        self._scorer = SimilarityScorer(registry_spec, graph_driver, domain_id, domain_spec=domain_spec)

    async def resolve_entity(
        self,
        entity_id: str | int,
        entity_label: str,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """Find and merge duplicate entities for a given entity.

        Args:
            entity_id: The entity to resolve.
            entity_label: Neo4j label of the entity.
            threshold: Optional override for the spec's similarity threshold
                (RU-101; ignored while ``entity_resolution_merge_integrity`` is off).

        Returns:
            Resolution metadata: canonical_id, merged_count, resolution_ids
            (and, under RU-101, merged_entity_ids).
        """
        if _merge_integrity():
            return await self._resolve_entity_verified(entity_id, entity_label, _validate_threshold(threshold))
        candidates = await self._scorer.find_candidates(
            entity_id=entity_id,
            entity_label=entity_label,
        )

        if not candidates:
            return {
                "canonical_id": entity_id,
                "merged_count": 0,
                "resolution_ids": [],
            }

        # Determine canonical: entity with highest degree
        all_ids = [entity_id, *[c["entity_id"] for c in candidates]]
        canonical_id = await self._find_canonical(all_ids, entity_label)

        # Merge non-canonical entities into canonical
        merge_ids = [eid for eid in all_ids if eid != canonical_id]
        resolution_ids: list[str] = []

        for merge_id in merge_ids:
            rid = await self._create_resolution(
                source_id=merge_id,
                canonical_id=canonical_id,
                entity_label=entity_label,
                similarity=next(
                    (c["similarity"] for c in candidates if c["entity_id"] == merge_id),
                    1.0,
                ),
            )
            resolution_ids.append(rid)

        logger.info(
            "Resolved entity %s: canonical=%s, merged=%d",
            entity_id,
            canonical_id,
            len(merge_ids),
        )

        return {
            "canonical_id": canonical_id,
            "merged_count": len(merge_ids),
            "resolution_ids": resolution_ids,
        }

    async def resolve_batch(
        self,
        entity_label: str,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """Resolve all entities of a given label.

        Args:
            entity_label: Neo4j label to resolve.
            threshold: Optional override for similarity threshold.

        Returns:
            Summary stats: total_entities, total_merged, resolution_groups.
        """
        if _merge_integrity():
            return await self._resolve_batch_verified(entity_label, _validate_threshold(threshold))
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))

        cypher = f"""
        MATCH (e:{label})
        WHERE NOT EXISTS((e)-[:RESOLVED_FROM]->())
        RETURN e.{id_prop} AS entity_id
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={},
            database=self._db,
        )

        entity_ids = [r["entity_id"] for r in results if r.get("entity_id") is not None]
        resolved_set: set[str] = set()
        total_merged = 0
        resolution_groups = 0

        for eid in entity_ids:
            if eid in resolved_set:
                continue

            result = await self.resolve_entity(
                entity_id=eid,
                entity_label=entity_label,
            )
            merged = result.get("merged_count", 0)
            if merged > 0:
                total_merged += merged
                resolution_groups += 1
                # Mark merged entities as resolved
                resolved_set.update(result.get("resolution_ids", []))

        return {
            "total_entities": len(entity_ids),
            "total_merged": total_merged,
            "resolution_groups": resolution_groups,
        }

    # ------------------------------------------------------------------
    # RU-101: merge-integrity path
    # ------------------------------------------------------------------

    async def _resolve_entity_verified(
        self,
        entity_id: str | int,
        entity_label: str,
        threshold: float | None,
    ) -> dict[str, Any]:
        """Resolve one entity so every RESOLVED_FROM edge is justified.

        - candidates are found at the effective threshold (override or spec);
        - entities that already carry an outgoing RESOLVED_FROM are neither
          candidates nor canonical; an already-resolved subject is a no-op;
        - the canonical is the highest-degree remaining entity (RESOLVED_FROM
          not counted), ties broken by entity_id ascending;
        - each other member is merged only when its unrounded similarity *to
          the canonical* meets the threshold, and the edge stores that measured
          similarity rounded to 4 places (the subject's similarity to the
          canonical is the candidate score already measured);
        - every member is scored before any edge is written, so one merge never
          shifts the structural similarity of the members after it;
        - the edge is MERGEd, so it exists at most once per (source, canonical).
        """
        effective = threshold if threshold is not None else self._spec.similarity_threshold
        no_op: dict[str, Any] = {
            "canonical_id": entity_id,
            "merged_count": 0,
            "resolution_ids": [],
            "merged_entity_ids": [],
        }
        candidates = await self._scorer.find_candidates(
            entity_id=entity_id,
            entity_label=entity_label,
            threshold=effective,
            exclude_resolved=True,
        )
        if not candidates:
            return no_op

        resolved = await self._already_resolved([entity_id, *[c["entity_id"] for c in candidates]], entity_label)
        if entity_id in resolved:
            return no_op
        measured = {c["entity_id"]: c["similarity"] for c in candidates if c["entity_id"] not in resolved}
        if not measured:
            return no_op

        all_ids = [entity_id, *measured]
        canonical_id = await self._find_canonical(all_ids, entity_label)

        # Score every member against the canonical before writing any edge: a
        # RESOLVED_FROM edge changes the canonical's neighbor set, so a write
        # here would bias the structural similarity of the members after it.
        # The threshold is compared with the unrounded score; only the stored
        # confidence is rounded. A pair involving the subject was already
        # measured, unrounded, against ``effective`` by find_candidates.
        accepted: list[tuple[str, float]] = []
        for member in all_ids:
            if member == canonical_id:
                continue
            if canonical_id == entity_id:
                accepted.append((member, measured[member]))
                continue
            if member == entity_id:
                accepted.append((member, measured[canonical_id]))
                continue
            score = await self._scorer.compute_similarity(member, canonical_id, entity_label)
            if score < effective:
                logger.info(
                    "Not merging %s into %s: similarity %.6f below threshold %.6f",
                    member,
                    canonical_id,
                    score,
                    effective,
                )
                continue
            accepted.append((member, round(score, 4)))

        resolution_ids: list[str] = []
        merged_entity_ids: list[str] = []
        created_count = 0
        for member, similarity in accepted:
            linked = await self._merge_resolution(member, canonical_id, entity_label, similarity)
            if linked is None:
                continue
            rid, created = linked
            resolution_ids.append(rid)
            merged_entity_ids.append(member)
            created_count += int(created)

        logger.info(
            "Resolved entity %s: canonical=%s, merged=%d (new edges=%d)",
            entity_id,
            canonical_id,
            len(merged_entity_ids),
            created_count,
        )
        return {
            "canonical_id": canonical_id,
            "merged_count": created_count,
            "resolution_ids": resolution_ids,
            "merged_entity_ids": merged_entity_ids,
        }

    async def _resolve_batch_verified(self, entity_label: str, threshold: float | None) -> dict[str, Any]:
        """Batch resolution that skips by entity id and counts only new edges.

        Entities are visited in id order, so the same graph resolves the same
        way whatever order its nodes were created in.
        """
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))
        cypher = f"""
        MATCH (e:{label})
        WHERE e.{id_prop} IS NOT NULL AND NOT EXISTS {{ (e)-[:RESOLVED_FROM]->() }}
        RETURN e.{id_prop} AS entity_id
        ORDER BY entity_id
        """
        results = await self._graph_driver.execute_query(cypher=cypher, parameters={}, database=self._db)

        entity_ids = [r["entity_id"] for r in results if r.get("entity_id") is not None]
        resolved_set: set[str] = set()
        total_merged = 0
        resolution_groups = 0
        for eid in entity_ids:
            if eid in resolved_set:
                continue
            result = await self.resolve_entity(entity_id=eid, entity_label=entity_label, threshold=threshold)
            merged_ids = result.get("merged_entity_ids", [])
            if merged_ids:
                resolved_set.update(merged_ids)
                resolved_set.add(result["canonical_id"])
            created = result.get("merged_count", 0)
            if created > 0:
                total_merged += created
                resolution_groups += 1

        return {
            "total_entities": len(entity_ids),
            "total_merged": total_merged,
            "resolution_groups": resolution_groups,
        }

    async def _already_resolved(self, entity_ids: list[Any], entity_label: str) -> set[Any]:
        """Ids among ``entity_ids`` that already carry an outgoing RESOLVED_FROM.

        Ids keep the type the graph stores them with, so an integer declared id
        compares equal to the candidate ids it is checked against.
        """
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))
        cypher = f"""
        UNWIND $entity_ids AS eid
        MATCH (e:{label} {{{id_prop}: eid}})
        WHERE EXISTS {{ (e)-[:RESOLVED_FROM]->() }}
        RETURN DISTINCT e.{id_prop} AS resolved_id
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"entity_ids": entity_ids},
            database=self._db,
        )
        return {r["resolved_id"] for r in results if r.get("resolved_id") is not None}

    async def _merge_resolution(
        self,
        source_id: str | int,
        canonical_id: str | int,
        entity_label: str,
        similarity: float,
    ) -> tuple[str, bool] | None:
        """MERGE the RESOLVED_FROM edge; return (resolution_id, created) or None if not written.

        Eligibility is re-checked inside the write, after both endpoints are
        write-locked (in elementId order, so two writers never deadlock on a
        pair): the source must carry no RESOLVED_FROM to any other node and the
        canonical none at all. A concurrent resolution that already linked the
        source elsewhere therefore wins, and this call writes nothing (None),
        so a source never gets two canonicals. The lock writes a per-call token to
        ``ceg:resolution-lock`` (a real write, so it takes the node lock) and then
        writes the prior value back (null removes the key), so whatever either
        node stored under that key is unchanged when the transaction commits.
        """
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))
        resolution_id = f"res_{uuid.uuid4().hex[:12]}"
        cypher = f"""
        MATCH (source:{label} {{{id_prop}: $source_id}})
        MATCH (canonical:{label} {{{id_prop}: $canonical_id}})
        WHERE source <> canonical
        UNWIND CASE WHEN elementId(source) < elementId(canonical)
                    THEN [source, canonical] ELSE [canonical, source] END AS n
        WITH source, canonical, n, n.`ceg:resolution-lock` AS prior
        SET n.`ceg:resolution-lock` = $lock_token
        SET n.`ceg:resolution-lock` = prior
        WITH DISTINCT source, canonical
        WHERE NOT EXISTS {{ (canonical)-[:RESOLVED_FROM]->() }}
          AND NOT EXISTS {{ (source)-[:RESOLVED_FROM]->(other) WHERE other <> canonical }}
        MERGE (source)-[rel:RESOLVED_FROM]->(canonical)
        ON CREATE SET rel.resolution_id = $resolution_id,
                      rel.confidence = $similarity,
                      rel.signal = $signal,
                      rel.domain_id = $domain_id,
                      rel.created_at = datetime()
        RETURN rel.resolution_id AS resolution_id,
               rel.resolution_id = $resolution_id AS created
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={
                "source_id": source_id,
                "canonical_id": canonical_id,
                "resolution_id": resolution_id,
                "lock_token": resolution_id,
                "similarity": similarity,
                "signal": "semantic_registry",
                "domain_id": self._db,
            },
            database=self._db,
        )
        if not results:
            return None
        row = results[0]
        return str(row.get("resolution_id") or resolution_id), bool(row.get("created"))

    async def _find_canonical(
        self,
        entity_ids: list[Any],
        entity_label: str,
    ) -> Any:
        """Find the entity with the highest degree (most connections).

        RU-101: already-resolved entities are excluded, RESOLVED_FROM edges do
        not count toward degree, and ties break on entity_id ascending. The id
        is returned with its stored type (an integer declared id stays an int).
        """
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))
        if _merge_integrity():
            cypher = f"""
        UNWIND $entity_ids AS eid
        MATCH (e:{label} {{{id_prop}: eid}})
        WHERE NOT EXISTS {{ (e)-[:RESOLVED_FROM]->() }}
        OPTIONAL MATCH (e)-[r]-()
        WHERE type(r) <> 'RESOLVED_FROM'
        WITH e.{id_prop} AS entity_id, count(r) AS degree
        ORDER BY degree DESC, entity_id ASC
        LIMIT 1
        RETURN entity_id
        """
            results = await self._graph_driver.execute_query(
                cypher=cypher,
                parameters={"entity_ids": entity_ids},
                database=self._db,
            )
            if results:
                return results[0]["entity_id"]
            return entity_ids[0]
        cypher = f"""
        UNWIND $entity_ids AS eid
        MATCH (e:{label} {{{id_prop}: eid}})
        OPTIONAL MATCH (e)-[r]-()
        WITH e.{id_prop} AS entity_id, count(r) AS degree
        ORDER BY degree DESC
        LIMIT 1
        RETURN entity_id
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"entity_ids": entity_ids},
            database=self._db,
        )
        if results:
            return results[0]["entity_id"]
        return entity_ids[0]

    async def _create_resolution(
        self,
        source_id: str,
        canonical_id: str,
        entity_label: str,
        similarity: float,
    ) -> str:
        """Create a RESOLVED_FROM edge between source and canonical."""
        label = sanitize_label(entity_label)
        id_prop = sanitize_label(self._scorer.id_property(label))
        resolution_id = f"res_{uuid.uuid4().hex[:12]}"

        cypher = f"""
        MATCH (source:{label} {{{id_prop}: $source_id}})
        MATCH (canonical:{label} {{{id_prop}: $canonical_id}})
        CREATE (source)-[:RESOLVED_FROM {{
            resolution_id: $resolution_id,
            confidence: $similarity,
            signal: 'semantic_registry',
            domain_id: $domain_id,
            created_at: datetime()
        }}]->(canonical)
        RETURN $resolution_id AS resolution_id
        """
        await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={
                "source_id": source_id,
                "canonical_id": canonical_id,
                "resolution_id": resolution_id,
                "similarity": similarity,
                "domain_id": self._db,
            },
            database=self._db,
        )
        return resolution_id
