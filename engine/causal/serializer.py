"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [causal]
tags: [causal, serializer, bfs, explanation]
owner: engine-team
status: active
--- /L9_META ---

BFS-based causal subgraph serialization for match explanations.

Reference: Jiang et al. (2023) "ReasoningLM: Enabling Structural Subgraph
Reasoning in Pre-trained Language Models", Section 4.2.1.
Converts a subgraph into a flat sequence preserving structural information.
"""

from __future__ import annotations

from typing import Any

import structlog

from engine.causal.identity import FALLBACK_ID_PROPERTY, node_id_property
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = structlog.get_logger(__name__)


class CausalSubgraphSerializer:
    """
    Serializes the causal neighborhood around a node using BFS.

    Produces a human-readable explanation of why a candidate was selected,
    preserving structural information from the causal subgraph.
    """

    def __init__(self, graph_driver: GraphDriver, domain_spec: DomainSpec) -> None:
        self._driver = graph_driver
        self._spec = domain_spec
        self._db = domain_spec.domain.id

    async def serialize_neighborhood(
        self,
        node_id: str,
        node_label: str,
        max_depth: int = 2,
        max_nodes: int = 20,
        id_property: str = "entity_id",
    ) -> dict[str, Any]:
        """
        BFS from node_id, collecting edges and properties.

        ``id_property`` is the property that identifies the start node and
        names the nodes on each path — the domain's declared id (sync
        ``idproperty`` / first required property), not a fixed ``entity_id``.

        Returns:
        - serialized: human-readable path string
        - nodes_visited: count of unique nodes
        - edges_traversed: list of edge types encountered
        - depth_reached: actual BFS depth
        """
        safe_label = sanitize_label(node_label)
        safe_id = sanitize_label(id_property)

        # Build causal edge pattern from domain spec
        causal_spec = self._spec.causal
        if settings.causal_serializer_variable_depth:
            # RU-074 / CEG-COR-121: `[r:T]` is length 1, so depth_reached never
            # exceeded 1. Cypher does not accept a parameter as a var-length
            # bound, so the bound is a validated integer literal in
            # [1, chain_depth_limit]; `$max_depth` carries the same value.
            hops = max(1, min(int(max_depth), int(causal_spec.chain_depth_limit)))
            max_depth = hops
            if causal_spec.causal_edges:
                safe_types = [sanitize_label(e.edge_type) for e in causal_spec.causal_edges]
                rel_pattern = f"[:{'|'.join(safe_types)}*1..{hops}]"
            else:
                rel_pattern = f"[*1..{hops}]"
        elif causal_spec.causal_edges:
            safe_types = [sanitize_label(e.edge_type) for e in causal_spec.causal_edges]
            edge_filter = "|".join(safe_types)
            rel_pattern = f"[r:{edge_filter}]"
        else:
            rel_pattern = "[r]"

        path_node_id = self._path_node_id_expression(safe_label, safe_id)
        cypher = f"""
        MATCH (start:{safe_label})
        WHERE start.{safe_id} = $node_id
        CALL {{
            WITH start
            MATCH path = (start)-{rel_pattern}->(neighbor)
            WITH path, length(path) AS depth
            WHERE depth <= $max_depth
            RETURN path, depth
            ORDER BY depth ASC
            LIMIT $max_nodes
        }}
        WITH path, depth,
             [n IN nodes(path) | {{id: {path_node_id}, label: labels(n)[0], name: coalesce(n.name, {path_node_id})}}] AS path_nodes,
             [r IN relationships(path) | {{type: type(r), confidence: coalesce(r.confidence, 0.0)}}] AS path_rels
        RETURN path_nodes, path_rels, depth
        ORDER BY depth ASC
        """

        try:
            results = await self._driver.execute_query(
                cypher,
                parameters={
                    "node_id": node_id,
                    "max_depth": max_depth,
                    "max_nodes": max_nodes,
                },
                database=self._db,
            )
        except Exception as exc:
            # C09: a failed query is distinguishable from an empty neighborhood
            # (which logs nothing) by this event and its error class; the
            # returned explanation keeps the locked match shape.
            logger.warning(
                "causal_serialization_failed",
                node_id=node_id,
                node_label=node_label,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return {
                "serialized": "",
                "nodes_visited": 0,
                "edges_traversed": [],
                "depth_reached": 0,
            }

        if not results:
            return {
                "serialized": "",
                "nodes_visited": 0,
                "edges_traversed": [],
                "depth_reached": 0,
            }

        # Collect unique nodes and edges, build serialized string
        visited_nodes: set[str] = set()
        edge_types: list[str] = []
        path_strings: list[str] = []
        max_depth_seen = 0

        for record in results:
            path_nodes = record.get("path_nodes", [])
            path_rels = record.get("path_rels", [])
            depth = record.get("depth", 0)
            max_depth_seen = max(max_depth_seen, depth)

            # Build path string: Node[id] -[REL_TYPE]-> Node[id]
            parts: list[str] = []
            for i, node in enumerate(path_nodes):
                node_str = f"{node.get('label', '?')}[{node.get('name', '?')}]"
                visited_nodes.add(str(node.get("id", "")))
                parts.append(node_str)
                if i < len(path_rels):
                    rel = path_rels[i]
                    rel_type = rel.get("type", "?")
                    edge_types.append(rel_type)
                    parts.append(f"-[{rel_type}]->")

            path_str = " ".join(parts)
            if path_str and path_str not in path_strings:
                path_strings.append(path_str)

        serialized = "; ".join(path_strings)

        return {
            "serialized": serialized,
            "nodes_visited": len(visited_nodes),
            "edges_traversed": list(dict.fromkeys(edge_types)),
            "depth_reached": max_depth_seen,
        }

    def _path_node_id_expression(self, start_label: str, start_id_property: str) -> str:
        """Cypher for a path node's identity, by the DECLARED id of its label (F09).

        A causal path can cross labels whose identity properties differ (e.g. a
        depot's ``depot_id`` and an outcome's ``outcome_id``); reading every node
        by the start node's property gave the other labels a NULL id, so their
        nodes collapsed into one in ``nodes_visited``. With
        ``spec_declared_candidate_id`` on, each label the spec declares (ontology
        nodes, causal-edge endpoints, the outcome label) is read by
        ``engine.causal.identity.node_id_property`` — the start label by the
        caller's ``id_property`` — falling back to ``entity_id`` for a node that
        lacks the declared property; undeclared labels keep the start property.
        Labels and properties are sanitized (C-9). Flag off: the start property.
        """
        from engine.config.outcome_vocabulary import declared_outcome_node_label

        fallback = f"n.{sanitize_label(start_id_property)}"
        if not settings.spec_declared_candidate_id:
            return fallback
        labels: list[str] = [node.label for node in self._spec.ontology.nodes]
        for edge in self._spec.causal.causal_edges:
            labels += [edge.source_label, edge.target_label]
        outcome_label = declared_outcome_node_label(self._spec)
        if outcome_label is not None:
            labels.append(outcome_label)
        branches: list[str] = []
        seen: set[str] = set()
        for raw_label in labels:
            label = sanitize_label(raw_label)
            if label in seen:
                continue
            seen.add(label)
            prop = sanitize_label(
                start_id_property if label == start_label else node_id_property(self._spec, raw_label)
            )
            if prop == FALLBACK_ID_PROPERTY:
                branches.append(f"WHEN n:{label} THEN n.{prop}")
            else:
                branches.append(f"WHEN n:{label} THEN coalesce(n.{prop}, n.{sanitize_label(FALLBACK_ID_PROPERTY)})")
        if not branches:
            return fallback
        return f"CASE {' '.join(branches)} ELSE {fallback} END"
