"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [causal]
tags: [causal, compiler, cypher]
owner: engine-team
status: active
--- /L9_META ---

Causal edge compiler.
Compiles causal edge declarations from domain spec into parameterized Cypher.
"""

from __future__ import annotations

import logging

from engine.causal.identity import node_id_property
from engine.config.schema import CausalEdgeSpec, DomainSpec
from engine.config.settings import settings
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)


class CausalCompiler:
    """
    Compiles causal edge declarations from domain spec into
    Cypher MERGE patterns with temporal validation.

    Similar to GateCompiler for gates -- takes spec declarations
    and produces parameterized Cypher.
    """

    def __init__(self, domain_spec: DomainSpec) -> None:
        self._spec = domain_spec
        self._causal_spec = domain_spec.causal

    def compile_causal_edge_create(
        self,
        edge_spec: CausalEdgeSpec,
        *,
        source_id_property: str | None = None,
        target_id_property: str | None = None,
    ) -> str:
        """
        Generate Cypher for creating a causal edge with validation.

        All labels are sanitized. Values use parameterized queries.
        Temporal validation is included when edge_spec.temporal_validation is True.

        RU-068 (``causal_edge_declared_identity``): source and target are
        matched by their identity properties — ``source_id_property`` /
        ``target_id_property`` when given (sanitized), else ``outcome_id`` for
        the domain's outcome label and the declared id property otherwise
        (``engine.causal.identity``). All predicates share one ``WHERE``
        (emitted only when non-empty); the threshold is the parameter
        ``$confidence_threshold``; the MERGE key is the relationship type, with
        properties set ``ON CREATE`` / ``ON MATCH`` so repeats do not create
        parallel edges. Parameters: ``source_id``, ``target_id``,
        ``confidence``, ``mechanism`` and, when the threshold is positive,
        ``confidence_threshold``.
        """
        source_label = sanitize_label(edge_spec.source_label)
        target_label = sanitize_label(edge_spec.target_label)
        edge_type = sanitize_label(edge_spec.edge_type)

        if not settings.causal_edge_declared_identity:
            return self._legacy_edge_create(edge_spec)

        source_prop = sanitize_label(source_id_property or node_id_property(self._spec, edge_spec.source_label))
        target_prop = sanitize_label(target_id_property or node_id_property(self._spec, edge_spec.target_label))

        predicates: list[str] = []
        if edge_spec.temporal_validation:
            predicates.append("source.created_at < target.created_at")
        if edge_spec.confidence_threshold > 0.0:
            predicates.append("$confidence >= $confidence_threshold")
        where_clause = f"WHERE {' AND '.join(predicates)}\n" if predicates else ""

        return (
            f"MATCH (source:{source_label} {{{source_prop}: $source_id}})\n"
            f"MATCH (target:{target_label} {{{target_prop}: $target_id}})\n"
            f"{where_clause}"
            f"MERGE (source)-[r:{edge_type}]->(target)\n"
            f"ON CREATE SET r.created_at = datetime(), r.confidence = $confidence, r.mechanism = $mechanism\n"
            f"ON MATCH SET r.confidence = $confidence, r.mechanism = $mechanism\n"
            f"RETURN r"
        )

    @staticmethod
    def _legacy_edge_create(edge_spec: CausalEdgeSpec) -> str:
        """Pre-RU-068 Cypher, kept verbatim for C-21 rollback only."""
        source_label = sanitize_label(edge_spec.source_label)
        target_label = sanitize_label(edge_spec.target_label)
        edge_type = sanitize_label(edge_spec.edge_type)
        temporal_clause = ""
        if edge_spec.temporal_validation:
            temporal_clause = "WHERE source.created_at < target.created_at\n"

        confidence_clause = ""
        if edge_spec.confidence_threshold > 0.0:
            confidence_clause = f"AND $confidence >= {edge_spec.confidence_threshold}\n"

        return (
            f"MATCH (source:{source_label} {{entity_id: $source_id}})\n"
            f"MATCH (target:{target_label} {{entity_id: $target_id}})\n"
            f"{temporal_clause}"
            f"{confidence_clause}"
            f"MERGE (source)-[r:{edge_type} {{\n"
            f"    confidence: $confidence,\n"
            f"    mechanism: $mechanism,\n"
            f"    created_at: datetime()\n"
            f"}}]->(target)\n"
            f"RETURN r"
        )

    def compile_causal_chain_query(
        self,
        root_label: str,
        edge_types: list[str] | None = None,
        max_depth: int | None = None,
    ) -> str:
        """
        Generate Cypher for traversing causal chains from a root node.

        Uses variable-length path patterns to discover causal chains
        up to the configured depth limit.
        """
        safe_root = sanitize_label(root_label)
        depth = int(max_depth or self._causal_spec.chain_depth_limit)

        if edge_types:
            safe_types = [sanitize_label(t) for t in edge_types]
            edge_pattern = "|".join(safe_types)
            rel_pattern = f"[:{edge_pattern}*1..{depth}]"
        # Use all causal edge types from the spec
        elif self._causal_spec.causal_edges:
            safe_types = [sanitize_label(e.edge_type) for e in self._causal_spec.causal_edges]
            edge_pattern = "|".join(safe_types)
            rel_pattern = f"[:{edge_pattern}*1..{depth}]"
        else:
            rel_pattern = f"[*1..{depth}]"

        cypher = (
            f"MATCH path = (root:{safe_root})-{rel_pattern}->(effect)\n"
            f"RETURN root, nodes(path) AS chain_nodes,\n"
            f"       relationships(path) AS chain_edges,\n"
            f"       length(path) AS chain_depth\n"
            f"ORDER BY chain_depth DESC"
        )

        return cypher

    def compile_all_edge_creates(self) -> list[tuple[CausalEdgeSpec, str]]:
        """Compile Cypher for all causal edges declared in the domain spec."""
        compiled: list[tuple[CausalEdgeSpec, str]] = []
        for edge_spec in self._causal_spec.causal_edges:
            cypher = self.compile_causal_edge_create(edge_spec)
            compiled.append((edge_spec, cypher))
        return compiled
