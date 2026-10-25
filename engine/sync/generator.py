"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [sync, cypher, merge]
owner: engine-team
status: active
--- /L9_META ---

Sync generator: Spec -> UNWIND MERGE/MATCH SET Cypher.
Generates batch sync queries from domain sync endpoint specs.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.schema import DomainSpec, SyncEndpointSpec, SyncStrategy
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)


class SyncGenerator:
    """Generates batch sync Cypher queries."""

    def __init__(self, domain_spec: DomainSpec) -> None:
        self.domain_spec = domain_spec

    def generate_sync_query(
        self,
        endpoint_spec: SyncEndpointSpec,
        batch_data: list[dict[str, Any]],
    ) -> str:
        """Generate batch sync Cypher query."""
        if endpoint_spec.batchstrategy == SyncStrategy.UNWINDMERGE:
            return self._generate_unwind_merge(endpoint_spec)
        if endpoint_spec.batchstrategy == SyncStrategy.UNWINDMATCHSET:
            return self._generate_unwind_match_set(endpoint_spec)
        raise ValueError(f"Unknown sync strategy: {endpoint_spec.batchstrategy}")

    def _generate_unwind_merge(self, spec: SyncEndpointSpec) -> str:
        if not spec.targetnode:
            raise ValueError(f"Endpoint '{spec.path}': targetnode required for MERGE")
        if not spec.idproperty:
            raise ValueError(f"Endpoint '{spec.path}': idproperty required for MERGE")

        sanitized_target = sanitize_label(spec.targetnode)
        sanitized_id = sanitize_label(spec.idproperty)

        # Stage 2 (CEG Stage 4 proof, P3): a childsync row carries its children
        # as a list of maps under `field`. `SET n += row` copied that list onto
        # the parent and Neo4j rejected the whole statement ("Property values
        # can only be of primitive types or arrays thereof"), so childsync could
        # never execute. Null-override the child fields in a map projection so
        # the parent receives only its own properties.
        from engine.config.settings import settings

        child_fields = [sanitize_label(child.field) for child in spec.childsync or []]
        if child_fields and settings.childsync_foreach_projection:
            null_overrides = ", ".join(f"{field}: null" for field in child_fields)
            set_clause = f"SET n += row{{.*, {null_overrides}}}, n._tenant = $tenant"
        else:
            set_clause = "SET n += row, n._tenant = $tenant"

        cypher_parts = [
            "UNWIND $batch AS row",
            f"MERGE (n:{sanitized_target} {{{sanitized_id}: row.{sanitized_id}}})",
            set_clause,
        ]

        if spec.taxonomyedges:
            for tax_edge in spec.taxonomyedges:
                sanitized_edge_type = sanitize_label(tax_edge.edgetype)
                cypher_parts.extend(
                    [
                        "WITH n, row",
                        f"OPTIONAL MATCH (tax:{sanitize_label(tax_edge.targetlabel)} {{{sanitize_label(tax_edge.targetid)}: row.{sanitize_label(tax_edge.field)}}})",
                        "FOREACH (_ IN CASE WHEN tax IS NOT NULL THEN [1] ELSE [] END |",
                        f"  MERGE (n)-[:{sanitized_edge_type}]->(tax)",
                        ")",
                    ]
                )

        if spec.childsync:
            for child in spec.childsync:
                sanitized_child_node = sanitize_label(child.targetnode)
                sanitized_child_edge = sanitize_label(child.edgetype)
                sanitized_child_field = sanitize_label(child.field)
                sanitized_child_target_id = sanitize_label(child.targetid)
                if not settings.childsync_foreach_projection:
                    # Legacy path (C-21 rollback). Not executable on Neo4j 5:
                    # the parent SET above still carries the child collection.
                    direction = "->" if child.edgedirection == "parenttochild" else "<-"
                    cypher_parts.extend(
                        [
                            "WITH n, row",
                            f"UNWIND coalesce(row.{sanitized_child_field}, []) AS child_row",
                            f"MERGE (c:{sanitized_child_node} {{{sanitized_child_target_id}: child_row.{sanitized_child_target_id}}})",
                            "SET c += child_row, c._tenant = $tenant",
                            f"MERGE (n)-[:{sanitized_child_edge}]{direction}(c)",
                        ]
                    )
                    continue
                # Stage 2 (P3): `(n)-[:T]<-(c)` is not Cypher; the reverse edge
                # is `(n)<-[:T]-(c)`. An undeclared direction is refused rather
                # than silently picking one.
                if child.edgedirection == "parenttochild":
                    edge_pattern = f"(n)-[:{sanitized_child_edge}]->(c)"
                elif child.edgedirection == "childtoparent":
                    edge_pattern = f"(n)<-[:{sanitized_child_edge}]-(c)"
                else:
                    msg = (
                        f"Endpoint '{spec.path}': childsync '{child.field}' edgedirection must be "
                        f"'parenttochild' or 'childtoparent', got {child.edgedirection!r}"
                    )
                    raise ValueError(msg)
                # FOREACH keeps one output row per parent, so a parent with no
                # children (or several childsync blocks) never truncates the
                # statement the way UNWIND over an empty list does.
                cypher_parts.extend(
                    [
                        "WITH n, row",
                        f"FOREACH (child_row IN coalesce(row.{sanitized_child_field}, []) |",
                        f"  MERGE (c:{sanitized_child_node} {{{sanitized_child_target_id}: child_row.{sanitized_child_target_id}}})",
                        "  SET c += child_row, c._tenant = $tenant",
                        f"  MERGE {edge_pattern}",
                        ")",
                    ]
                )

        return "\n".join(cypher_parts)

    def _generate_unwind_match_set(self, spec: SyncEndpointSpec) -> str:
        if not spec.targetnode:
            raise ValueError(f"Endpoint '{spec.path}': targetnode required")
        if not spec.idproperty:
            raise ValueError(f"Endpoint '{spec.path}': idproperty required")

        cypher_parts = [
            "UNWIND $batch AS row",
            f"MATCH (n:{sanitize_label(spec.targetnode)} {{{sanitize_label(spec.idproperty)}: row.{sanitize_label(spec.idproperty)}}})",
        ]

        if spec.fieldsupdated:
            set_clauses = [f"n.{sanitize_label(field)} = row.{sanitize_label(field)}" for field in spec.fieldsupdated]
            set_clauses.append("n._tenant = $tenant")
            set_clauses.append("n.updated_at = datetime()")
            cypher_parts.append("SET " + ",\n    ".join(set_clauses))
        else:
            cypher_parts.append("SET n += row, n._tenant = $tenant")

        return "\n".join(cypher_parts)


# Relationship evidence edges carry this stamp (QM.POC0.CEG.GRAPH_EVIDENCE.002A);
# engine/scoring/graph_evidence.py reads only edges that carry it.
RELATIONSHIP_EVIDENCE_SCHEMA = "ceg.relationship_evidence.v1"
