"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [gds]
tags: [gds, scheduler, node-similarity]
owner: engine-team
status: active
--- /L9_META ---

engine/gds/node_similarity.py
GDS ``nodeSimilarity`` job plan (CEG-GDS-SIM).

A domain pack declares a GDS job with ``algorithm: nodeSimilarity``; the
scheduler builds a :class:`NodeSimilarityPlan` from it and runs the plan's
statements in order against the tenant database:

1. ``pre_drop``  — drop a stale in-memory projection left by a crashed run;
2. ``project``   — ``gds.graph.project`` over the job's declared labels/types;
3. ``clear``     — delete the job's previous ``writeedge`` relationships
                   between projected nodes, so a re-run replaces rather than
                   duplicates (``gds.nodeSimilarity.write`` always CREATEs);
4. ``write``     — ``gds.nodeSimilarity.write`` with the declared bounds;
5. ``drop``      — drop the projection (always, in a ``finally``).

Every label, relationship type and property name comes from the job spec and
passes through ``sanitize_label``; all of them reach Cypher as ``$parameters``
except the relationship type in ``clear``, which Cypher cannot parameterize
and is interpolated only after sanitization. There is no built-in vocabulary:
a job missing ``writeedge``, ``writeproperty`` or a projection fails with a
``ValueError`` naming the job and the missing field.

GDS API: ``gds.nodeSimilarity.write(graphName, configuration)`` with
``writeRelationshipType``, ``writeProperty``, ``topK``, ``similarityCutoff``,
``degreeCutoff``, ``similarityMetric``; YIELD ``nodesCompared``,
``relationshipsWritten`` (Neo4j GDS manual, node-similarity page).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.config.schema import GDSJobSpec, GDSNodeSimilaritySpec
from engine.utils.security import sanitize_label

NODE_SIMILARITY_ALGORITHM = "nodeSimilarity"

PRE_DROP_CYPHER = """
CALL gds.graph.exists($graph_name) YIELD exists
WITH exists WHERE exists
CALL gds.graph.drop($graph_name) YIELD graphName
RETURN graphName
"""

PROJECT_CYPHER = """
CALL gds.graph.project($graph_name, $node_labels, $edge_types)
YIELD graphName, nodeCount, relationshipCount
RETURN graphName, nodeCount, relationshipCount
"""

WRITE_CYPHER = """
CALL gds.nodeSimilarity.write($graph_name, $config)
YIELD nodesCompared, relationshipsWritten
RETURN nodesCompared, relationshipsWritten
"""

DROP_CYPHER = "CALL gds.graph.drop($graph_name) YIELD graphName RETURN graphName"


def _clear_cypher(write_edge: str) -> str:
    """Delete the job's prior output: ``write_edge`` relationships between projected nodes."""
    edge = sanitize_label(write_edge)  # the one identifier Cypher cannot take as a $parameter
    return f"""
MATCH (a)-[r:{edge}]->(b)
WHERE any(l IN labels(a) WHERE l IN $node_labels)
  AND any(l IN labels(b) WHERE l IN $node_labels)
DELETE r
RETURN count(r) AS relationships_deleted
"""


@dataclass(frozen=True)
class NodeSimilarityPlan:
    """The parameterized statements one ``nodeSimilarity`` job run executes."""

    graph_name: str
    node_labels: list[str]
    edge_types: list[str]
    write_edge: str
    write_property: str
    config: dict[str, Any]
    clear_cypher: str
    params: dict[str, Any] = field(default_factory=dict)

    pre_drop_cypher: str = PRE_DROP_CYPHER
    project_cypher: str = PROJECT_CYPHER
    write_cypher: str = WRITE_CYPHER
    drop_cypher: str = DROP_CYPHER


def _required(job_spec: GDSJobSpec, field_name: str, value: str | None) -> str:
    if value is None or not str(value).strip():
        msg = (
            f"Job {job_spec.name}: algorithm {job_spec.algorithm!r} requires {field_name!r}; "
            "the domain spec must declare it (no built-in default)"
        )
        raise ValueError(msg)
    return sanitize_label(str(value))


def build_node_similarity_plan(job_spec: GDSJobSpec) -> NodeSimilarityPlan:
    """Build the run plan for a ``nodeSimilarity`` job, or raise ``ValueError``."""
    node_labels = [sanitize_label(label) for label in job_spec.projection.nodelabels]
    edge_types = [sanitize_label(edge) for edge in job_spec.projection.edgetypes]
    if not node_labels or not edge_types:
        msg = (
            f"Job {job_spec.name}: algorithm {job_spec.algorithm!r} requires a projection "
            "with at least one node label and one relationship type"
        )
        raise ValueError(msg)

    write_edge = _required(job_spec, "writeedge", job_spec.writeedge)
    write_property = _required(job_spec, "writeproperty", job_spec.writeproperty)
    bounds = job_spec.nodesimilarity or GDSNodeSimilaritySpec()

    config: dict[str, Any] = {
        "writeRelationshipType": write_edge,
        "writeProperty": write_property,
        "topK": bounds.topk,
        "degreeCutoff": bounds.degreecutoff,
        "similarityMetric": bounds.similaritymetric,
    }
    if bounds.similaritycutoff is not None:
        config["similarityCutoff"] = bounds.similaritycutoff

    graph_name = f"{sanitize_label(job_spec.name)}_graph"
    return NodeSimilarityPlan(
        graph_name=graph_name,
        node_labels=node_labels,
        edge_types=edge_types,
        write_edge=write_edge,
        write_property=write_property,
        config=config,
        clear_cypher=_clear_cypher(write_edge),
        params={
            "graph_name": graph_name,
            "node_labels": node_labels,
            "edge_types": edge_types,
            "config": config,
        },
    )
