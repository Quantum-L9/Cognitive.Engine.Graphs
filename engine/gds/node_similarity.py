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

1. ``pre_drop``      — drop a stale in-memory projection left by a crashed run;
2. ``clear_staging`` — delete this job's staging relationships left by a crashed run;
3. ``project``       — ``gds.graph.project`` over the job's declared labels/types;
4. ``write``         — ``gds.nodeSimilarity.write`` into the job's **staging**
                       relationship type, with the declared bounds;
5. ``swap``          — one statement (one transaction): delete the edges this job
                       wrote last time, then turn each staging relationship into a
                       ``writeedge`` relationship carrying ``writeproperty`` and the
                       job's ownership marker;
6. ``drop``          — drop the projection (always, in a ``finally``).

The previous result survives until the replacement is fully written: a failed
project or write leaves it in place, and the swap replaces it atomically.
Only relationships carrying this job's ownership marker
(``gds_job`` = the job name) are ever deleted, so another job or
application data that uses the same relationship type is never touched.

Every label, relationship type and property name comes from the job spec and
passes through ``sanitize_label``. Values reach Cypher as ``$parameters``; the
relationship types and the score property in ``clear_staging`` and ``swap``,
which Cypher cannot parameterize, are interpolated only after sanitization.
There is no built-in vocabulary: a job missing ``writeedge``,
``writeproperty`` or a projection fails with a ``ValueError`` naming the job
and the missing field.

GDS API: ``gds.nodeSimilarity.write(graphName, configuration)`` with
``writeRelationshipType``, ``writeProperty``, ``topK``, ``similarityCutoff``,
``degreeCutoff``, ``similarityMetric``; YIELD ``nodesCompared``,
``relationshipsWritten`` (Neo4j GDS manual, node-similarity page).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from engine.config.schema import GDSJobSpec, GDSNodeSimilaritySpec
from engine.utils.security import sanitize_label

NODE_SIMILARITY_ALGORITHM = "nodeSimilarity"
# Packs spell the algorithm two ways; ``node_similarity`` is a documented alias
# of the canonical ``nodeSimilarity`` and dispatches to the same job.
NODE_SIMILARITY_ALIASES: frozenset[str] = frozenset({"node_similarity"})
NODE_SIMILARITY_ALGORITHMS: frozenset[str] = frozenset({NODE_SIMILARITY_ALGORITHM}) | NODE_SIMILARITY_ALIASES

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

# Engine-owned marker on every relationship a nodeSimilarity job writes; its
# value is the job name. Cleanup deletes only relationships carrying this job's
# marker, never another producer's relationships of the same type.
OWNER_PROPERTY = "gds_job"


def staging_edge_for(job_name: str) -> str:
    """The job's private staging relationship type (unique per job, valid identifier)."""
    digest = hashlib.sha256(job_name.encode("utf-8")).hexdigest()[:16]
    return sanitize_label(f"GDS_STAGING_{digest}")


def _clear_staging_cypher(staging_edge: str) -> str:
    """Delete staging relationships a crashed run of this job left behind."""
    stage = sanitize_label(staging_edge)
    return f"""
MATCH ()-[s:{stage}]->()
DELETE s
RETURN count(s) AS staging_deleted
"""


def _swap_cypher(write_edge: str, staging_edge: str, write_property: str) -> str:
    """Replace this job's previous output with the staged result, in one statement."""
    edge = sanitize_label(write_edge)
    stage = sanitize_label(staging_edge)
    prop = sanitize_label(write_property)
    return f"""
CALL {{
  MATCH ()-[old:{edge}]->()
  WHERE old.{OWNER_PROPERTY} = $job_id
  DELETE old
  RETURN count(old) AS relationships_deleted
}}
CALL {{
  MATCH (a)-[s:{stage}]->(b)
  CREATE (a)-[r:{edge}]->(b)
  SET r.{prop} = s.{prop}, r.{OWNER_PROPERTY} = $job_id
  DELETE s
  RETURN count(r) AS relationships_written
}}
RETURN relationships_deleted, relationships_written
"""


@dataclass(frozen=True)
class NodeSimilarityPlan:
    """The parameterized statements one ``nodeSimilarity`` job run executes."""

    graph_name: str
    node_labels: list[str]
    edge_types: list[str]
    write_edge: str
    staging_edge: str
    write_property: str
    config: dict[str, Any]
    clear_staging_cypher: str
    swap_cypher: str
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
    staging_edge = staging_edge_for(job_spec.name)

    config: dict[str, Any] = {
        "writeRelationshipType": staging_edge,
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
        staging_edge=staging_edge,
        write_property=write_property,
        config=config,
        clear_staging_cypher=_clear_staging_cypher(staging_edge),
        swap_cypher=_swap_cypher(write_edge, staging_edge, write_property),
        params={
            "graph_name": graph_name,
            "job_id": job_spec.name,
            "node_labels": node_labels,
            "edge_types": edge_types,
            "config": config,
        },
    )
