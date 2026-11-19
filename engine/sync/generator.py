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
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from engine.config.schema import (
    ChildSyncSpec,
    DomainSpec,
    EdgeDirection,
    ManagedByType,
    ObservedAssociationSourceSpec,
    SyncEndpointSpec,
    SyncStrategy,
)
from engine.config.tenant_mapping import declared_ref_namespace
from engine.sync.row_contract import ChildProjection, SyncRowContract
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

    def row_contract(self, endpoint_spec: SyncEndpointSpec) -> SyncRowContract:
        """The endpoint's typed row contract; ``ValueError`` names every undeclared shape."""
        return SyncRowContract.from_spec(self.domain_spec, endpoint_spec)

    def project_batch(
        self, endpoint_spec: SyncEndpointSpec, batch_data: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """The ``$batch`` the declared-projection Cypher reads, and the undeclared keys it dropped.

        Each row becomes ``{"node": {...}, "children": {<field>: [...]}}`` holding
        only properties the spec declares writable (``SyncRowContract.project``).
        Used with ``sync_declared_projection_enabled``.
        """
        return self.row_contract(endpoint_spec).project(batch_data)

    def _generate_declared_merge(self, spec: SyncEndpointSpec) -> str:
        """UNWINDMERGE over ``project_batch`` rows: every identifier and shape from the spec.

        The parent receives ``row.node`` (declared, storable, present keys), so
        a key the spec does not declare — or a wire-only ``list<map>`` — never
        reaches ``SET``. A ``list<scalar>`` taxonomy field links one taxonomy
        node per element; a scalar field one. Each childsync child receives only
        the properties its item map and its node both declare; a
        ``managedby: static`` child is written only when the MERGE creates it.
        """
        contract = self.row_contract(spec)
        target = sanitize_label(contract.target_label)
        id_property = sanitize_label(contract.idproperty)
        cypher_parts = [
            "UNWIND $batch AS row",
            f"MERGE (n:{target} {{{id_property}: row.node.{id_property}}})",
            "SET n += row.node, n._tenant = $tenant",
        ]
        for tax_edge in spec.taxonomyedges or []:
            edge_type = sanitize_label(tax_edge.edgetype)
            tax_label = sanitize_label(tax_edge.targetlabel)
            tax_id = sanitize_label(tax_edge.targetid)
            tax_field = sanitize_label(tax_edge.field)
            if tax_edge.field in contract.taxonomy_lists:
                # One edge per element; an element naming no taxonomy node links nothing.
                cypher_parts.extend(
                    [
                        "WITH n, row",
                        f"OPTIONAL MATCH (tax:{tax_label}) WHERE tax.{tax_id} IN coalesce(row.node.{tax_field}, [])",
                        "WITH n, row, collect(tax) AS taxonomy_nodes",
                        f"FOREACH (tax IN taxonomy_nodes | MERGE (n)-[:{edge_type}]->(tax))",
                    ]
                )
            else:
                cypher_parts.extend(
                    [
                        "WITH n, row",
                        f"OPTIONAL MATCH (tax:{tax_label} {{{tax_id}: row.node.{tax_field}}})",
                        "FOREACH (_ IN CASE WHEN tax IS NOT NULL THEN [1] ELSE [] END |",
                        f"  MERGE (n)-[:{edge_type}]->(tax)",
                        ")",
                    ]
                )
        for child, projection in zip(spec.childsync or [], contract.children, strict=True):
            cypher_parts.extend(self._declared_child_clauses(spec.path, child, projection))
        return "\n".join(cypher_parts)

    def _declared_child_clauses(
        self, endpoint_path: str, child: ChildSyncSpec, projection: ChildProjection
    ) -> list[str]:
        """One childsync block. A declared line identity MERGEs the relationship by that key."""
        from engine.config.settings import settings

        child_label = sanitize_label(child.targetnode)
        child_edge = sanitize_label(child.edgetype)
        child_field = sanitize_label(projection.field)
        child_id = sanitize_label(projection.targetid)
        if child.edgedirection == "parenttochild":
            plain = f"(n)-[:{child_edge}]->(c)"
            keyed_open = f"(n)-[rel:{child_edge} {{"
            keyed_close = "}]->(c)"
        elif child.edgedirection == "childtoparent":
            plain = f"(n)<-[:{child_edge}]-(c)"
            keyed_open = f"(n)<-[rel:{child_edge} {{"
            keyed_close = "}]-(c)"
        else:
            msg = (
                f"Endpoint '{endpoint_path}': childsync '{child.field}' edgedirection must be "
                f"'parenttochild' or 'childtoparent', got {child.edgedirection!r}"
            )
            raise ValueError(msg)
        identity = child.edgeidentity
        use_lines = settings.buyer_line_identity_enabled and identity is not None and bool(projection.edge_keys)
        write = "ON CREATE SET" if projection.static else "SET"
        merge_node = f"  MERGE (c:{child_label} {{{child_id}: child_row.{child_id}}})"
        header = [
            "WITH n, row",
            f"FOREACH (child_row IN coalesce(row.children.{child_field}, []) |",
            merge_node,
        ]
        if not use_lines or identity is None:
            return [
                *header,
                f"  {write} c += child_row, c._tenant = $tenant",
                f"  MERGE {plain}",
                ")",
            ]
        names = [sanitize_label(name) for name in (child.edgeproperties or []) if name in projection.edge_keys]
        identity_name = sanitize_label(identity)
        # The child node's own id is not an edge override. Nulling it deletes the
        # identity the MERGE just bound and the next sync creates a second node.
        node_names = [name for name in names if name != sanitize_label(projection.targetid)]
        overrides = ", ".join(f"{name}: null" for name in node_names)
        rel_set = ", ".join(f"rel.{name} = child_row.{name}" for name in names)
        keyed = f"{keyed_open}{identity_name}: child_row.{identity_name}{keyed_close}"
        absent = f"child_row.{identity_name} IS NULL OR child_row.{identity_name} = ''"
        lines_expr = f"coalesce(row.children.{child_field}, [])"
        keyed_lines = f"[line IN {lines_expr} WHERE line.{identity_name} IS NOT NULL AND line.{identity_name} <> '']"
        if child.edgedirection == "parenttochild":
            stale_match = f"(n)-[stale:{child_edge}]->(stale_target)"
        else:
            stale_match = f"(n)<-[stale:{child_edge}]-(stale_target)"
        supplied = f"row.children.{child_field} IS NOT NULL"
        return [
            "WITH n, row",
            f"WITH n, row, {keyed_lines} AS keyed_lines",
            f"OPTIONAL MATCH {stale_match}",
            f"WHERE {supplied}",
            f"  AND stale.{identity_name} IS NOT NULL",
            (
                f"  AND NOT any(line IN keyed_lines WHERE line.{identity_name} = stale.{identity_name} "
                f"AND stale_target.{child_id} = line.{child_id})"
            ),
            "DELETE stale",
            "WITH DISTINCT n, row",
            f"FOREACH (child_row IN {lines_expr} |",
            merge_node,
            f"  {write} c += child_row{{.*, {overrides}}}, c._tenant = $tenant",
            f"  FOREACH (missing IN CASE WHEN {absent} THEN [1] ELSE [] END |",
            f"    MERGE {plain}",
            "  )",
            f"  FOREACH (present IN CASE WHEN {absent} THEN [] ELSE [1] END |",
            f"    MERGE {keyed}",
            f"    SET {rel_set}, rel._tenant = $tenant",
            "  )",
            ")",
        ]

    def _generate_unwind_merge(self, spec: SyncEndpointSpec) -> str:
        if not spec.targetnode:
            raise ValueError(f"Endpoint '{spec.path}': targetnode required for MERGE")
        if not spec.idproperty:
            raise ValueError(f"Endpoint '{spec.path}': idproperty required for MERGE")

        from engine.config.settings import settings

        if settings.sync_declared_projection_enabled:
            return self._generate_declared_merge(spec)

        sanitized_target = sanitize_label(spec.targetnode)
        sanitized_id = sanitize_label(spec.idproperty)

        # Stage 2 (CEG Stage 4 proof, P3): a childsync row carries its children
        # as a list of maps under `field`. `SET n += row` copied that list onto
        # the parent and Neo4j rejected the whole statement ("Property values
        # can only be of primitive types or arrays thereof"), so childsync could
        # never execute. Null-override the child fields in a map projection so
        # the parent receives only its own properties.
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

        from engine.config.settings import settings

        # Declared projection: rows are `project_batch` rows; read `row.node`.
        row_source = "row"
        if settings.sync_declared_projection_enabled:
            self.row_contract(spec)  # refuses an endpoint whose fields are not declared
            row_source = "row.node"

        cypher_parts = [
            "UNWIND $batch AS row",
            f"MATCH (n:{sanitize_label(spec.targetnode)} {{{sanitize_label(spec.idproperty)}: {row_source}.{sanitize_label(spec.idproperty)}}})",
        ]

        if spec.fieldsupdated:
            set_clauses = [
                f"n.{sanitize_label(field)} = {row_source}.{sanitize_label(field)}" for field in spec.fieldsupdated
            ]
            set_clauses.append("n._tenant = $tenant")
            set_clauses.append("n.updated_at = datetime()")
            cypher_parts.append("SET " + ",\n    ".join(set_clauses))
        else:
            cypher_parts.append(f"SET n += {row_source}, n._tenant = $tenant")

        # RU-060 / CEG-COR-169: MATCH drops rows with no existing node; report how
        # many rows actually matched (and were updated) so the handler need not
        # assume the whole batch landed.
        if settings.sync_count_from_write_result:
            cypher_parts.append("RETURN count(n) AS synced_count")

        return "\n".join(cypher_parts)


# ── Relationship evidence sync (QM.POC0.CEG.GRAPH_EVIDENCE.002A) ──────────────
#
# A sync endpoint that declares `targetedge` + `fromnode` + `tonode` (fields the
# schema always had, which no generator read) materialises one typed, evidenced
# relationship per batch row between two nodes that already exist. Everything
# structural comes from the tenant's own spec: the edge must be declared in
# `ontology.edges` with exactly that type, source label, target label,
# `direction: DIRECTED` and `managedby: sync`. A row names only the endpoints'
# declared ids and the evidence; it can never choose a label, a relationship
# type, a database or a tenant.
#
# Identity rule: one edge per evidence assertion, keyed by
# (relationship type, `evidence_ref`) within the tenant. Replaying the same row
# is a no-op; a row that reuses an `evidence_ref` with different endpoints or
# evidence is a conflict and is refused — stored provenance is never
# overwritten (`ON CREATE SET` only).

RELATIONSHIP_EVIDENCE_SCHEMA = "ceg.relationship_evidence.v1"
MAX_RELATIONSHIP_BATCH = 500
MAX_EVIDENCE_REF_LENGTH = 256
MAX_SOURCE_REF_LENGTH = 512

ASSERTION_STATUSES = frozenset({"observed", "reported", "inferred"})
POLARITIES = frozenset({"positive", "negative"})
_REQUIRED_ROW_KEYS = frozenset(
    {"from_id", "to_id", "evidence_ref", "source_ref", "confidence", "assertion_status", "polarity"}
)
# ``observed_association`` is provenance CEG checks against its own tenant spec
# before any write (``observed_association_defects``, D-9); it is never stored.
OBSERVED_ASSOCIATION_KEY = "observed_association"
_OPTIONAL_ROW_KEYS = frozenset({"source_revision", "expires_at", OBSERVED_ASSOCIATION_KEY})
# Properties the engine stamps on an evidence edge; never row-supplied.
RELATIONSHIP_INTERNAL_PROPERTIES = frozenset({"_tenant", "_evidence_schema", "_subject_ref", "_object_ref"})


@dataclass(frozen=True)
class RelationshipDeclaration:
    """A relationship sync endpoint resolved against the tenant's ontology."""

    edge_type: str
    from_label: str
    from_id_property: str
    to_label: str
    to_id_property: str
    # Declared graph-ref namespaces of the endpoint labels (D-13); None when undeclared.
    from_ref_namespace: str | None = None
    to_ref_namespace: str | None = None


def _declared_id_property(domain_spec: DomainSpec, label: str, path: str) -> str:
    """The stable id property the tenant declares for ``label``; never a default.

    The node sync endpoint's `idproperty` (the key node sync MERGEs on), else the
    node's first required property. Disagreeing node endpoints, or none, refuse.
    """
    declared = {
        endpoint.idproperty
        for endpoint in (domain_spec.sync.endpoints if domain_spec.sync else [])
        if endpoint.targetnode == label and endpoint.idproperty
    }
    if len(declared) > 1:
        msg = f"Endpoint '{path}': node {label!r} has conflicting sync idproperty declarations {sorted(declared)}"
        raise ValueError(msg)
    if declared:
        return declared.pop()
    for node in domain_spec.ontology.nodes:
        if node.label == label:
            for prop in node.properties:
                if prop.required:
                    return prop.name
    msg = f"Endpoint '{path}': node {label!r} declares no stable id (no sync idproperty, no required property)"
    raise ValueError(msg)


def _row_text(row: dict[str, Any], key: str, index: int, max_length: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        msg = f"relationship row {index}: {key} must be a non-empty string of at most {max_length} characters"
        raise ValueError(msg)
    return value


def _row_node_id(row: dict[str, Any], key: str, index: int) -> str | int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, str | int) or (isinstance(value, str) and not value.strip()):
        msg = f"relationship row {index}: {key} must be a non-empty string or an integer node id"
        raise ValueError(msg)
    return value


def _row_confidence(row: dict[str, Any], index: int) -> float:
    value = row.get("confidence")
    not_a_confidence = f"relationship row {index}: confidence must be an explicit number in [0, 1]"
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(not_a_confidence)
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(not_a_confidence) from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError(not_a_confidence)
    return confidence


def _row_expiry(row: dict[str, Any], index: int) -> str | None:
    value = row.get("expires_at")
    if value is None:
        return None
    invalid = f"relationship row {index}: expires_at must be an ISO-8601 timestamp with a UTC offset"
    if not isinstance(value, str):
        raise ValueError(invalid)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(invalid) from exc
    if parsed.tzinfo is None:
        raise ValueError(invalid)
    return value


def normalize_relationship_rows(batch: list[Any]) -> list[dict[str, Any]]:
    """Validate relationship rows and return them in write shape, deduplicated.

    Each result is ``{from_id, to_id, evidence_ref, props}`` where ``props`` is
    exactly what is stored on the edge. Unknown keys, missing evidence, a
    confidence outside [0, 1], an unknown assertion status or polarity, and two
    rows that reuse one ``evidence_ref`` with different content are refused.
    """
    if not 1 <= len(batch) <= MAX_RELATIONSHIP_BATCH:
        msg = f"relationship batch must hold 1..{MAX_RELATIONSHIP_BATCH} rows, got {len(batch)}"
        raise ValueError(msg)
    by_evidence: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(batch):
        if not isinstance(row, dict):
            msg = f"relationship row {index}: must be an object"
            raise ValueError(msg)
        unknown = sorted(set(row) - _REQUIRED_ROW_KEYS - _OPTIONAL_ROW_KEYS)
        if unknown:
            msg = f"relationship row {index}: unsupported keys {unknown}; labels, types and tenancy are spec-owned"
            raise ValueError(msg)
        missing = sorted(_REQUIRED_ROW_KEYS - set(row))
        if missing:
            msg = f"relationship row {index}: missing {missing}"
            raise ValueError(msg)
        status = row["assertion_status"]
        if status not in ASSERTION_STATUSES:
            msg = f"relationship row {index}: assertion_status must be one of {sorted(ASSERTION_STATUSES)}"
            raise ValueError(msg)
        polarity = row["polarity"]
        if polarity not in POLARITIES:
            msg = f"relationship row {index}: polarity must be one of {sorted(POLARITIES)}"
            raise ValueError(msg)
        evidence_ref = _row_text(row, "evidence_ref", index, MAX_EVIDENCE_REF_LENGTH)
        props: dict[str, Any] = {
            "evidence_ref": evidence_ref,
            "source_ref": _row_text(row, "source_ref", index, MAX_SOURCE_REF_LENGTH),
            "confidence": _row_confidence(row, index),
            "assertion_status": status,
            "polarity": polarity,
        }
        if row.get("source_revision") is not None:
            props["source_revision"] = _row_text(row, "source_revision", index, MAX_SOURCE_REF_LENGTH)
        expiry = _row_expiry(row, index)
        if expiry is not None:
            props["expires_at"] = expiry
        normalized = {
            "from_id": _row_node_id(row, "from_id", index),
            "to_id": _row_node_id(row, "to_id", index),
            "evidence_ref": evidence_ref,
            "props": props,
        }
        seen = by_evidence.get(evidence_ref)
        if seen is not None and seen != normalized:
            msg = f"relationship row {index}: evidence_ref {evidence_ref!r} repeated with different content"
            raise ValueError(msg)
        by_evidence[evidence_ref] = normalized
    return list(by_evidence.values())


def resolve_relationship_declaration(domain_spec: DomainSpec, spec: SyncEndpointSpec) -> RelationshipDeclaration:
    """Resolve a `targetedge` endpoint against the tenant's declared ontology, or refuse."""
    if not (spec.targetedge and spec.fromnode and spec.tonode):
        msg = f"Endpoint '{spec.path}': a relationship endpoint declares targetedge, fromnode and tonode"
        raise ValueError(msg)
    if spec.targetnode:
        msg = f"Endpoint '{spec.path}': declares both targetnode and targetedge; one endpoint syncs one kind"
        raise ValueError(msg)
    if spec.batchstrategy != SyncStrategy.UNWINDMERGE:
        msg = f"Endpoint '{spec.path}': relationship sync requires batchstrategy UNWINDMERGE"
        raise ValueError(msg)
    matches = [
        edge
        for edge in domain_spec.ontology.edges
        if edge.type == spec.targetedge and edge.from_ == spec.fromnode and edge.to == spec.tonode
    ]
    if len(matches) != 1:
        msg = (
            f"Endpoint '{spec.path}': ontology must declare exactly one edge "
            f"({spec.fromnode})-[:{spec.targetedge}]->({spec.tonode}), found {len(matches)}"
        )
        raise ValueError(msg)
    edge = matches[0]
    if edge.direction != EdgeDirection.DIRECTED or edge.managedby != ManagedByType.SYNC:
        msg = f"Endpoint '{spec.path}': edge {spec.targetedge!r} must be declared DIRECTED and managedby sync"
        raise ValueError(msg)
    return RelationshipDeclaration(
        edge_type=sanitize_label(spec.targetedge),
        from_label=sanitize_label(spec.fromnode),
        from_id_property=sanitize_label(_declared_id_property(domain_spec, spec.fromnode, spec.path)),
        to_label=sanitize_label(spec.tonode),
        to_id_property=sanitize_label(_declared_id_property(domain_spec, spec.tonode, spec.path)),
        from_ref_namespace=declared_ref_namespace(domain_spec, spec.fromnode),
        to_ref_namespace=declared_ref_namespace(domain_spec, spec.tonode),
    )


# Same bound as a scoring entity ref (engine/scoring/evidence_priority.py).
MAX_GRAPH_REF_LENGTH = 256


def _ref_defect(value: Any, namespace: str) -> str | None:
    """Why ``value`` is not a graph ref ``<namespace>:<id>``, else None."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_GRAPH_REF_LENGTH
        or not value.startswith(f"{namespace}:")
        or not value[len(namespace) + 1 :]
        or any(char.isspace() for char in value)
    ):
        return f"{value!r} is not a graph ref {namespace}:<id> (max {MAX_GRAPH_REF_LENGTH} chars, no whitespace)"
    return None


def ref_namespace_defects(domain_spec: DomainSpec, spec: SyncEndpointSpec, batch: list[Any]) -> list[str]:
    """Rows whose node ids fall outside the tenant's declared ref namespaces (D-13).

    A node endpoint with a ``refnamespace`` writes only ids in it; a relationship
    endpoint's ``from_id``/``to_id`` must be in its labels' namespaces. Labels
    without a declaration are unconstrained. Non-mapping rows are left to the
    row validators that already refuse them.
    """
    checks: list[tuple[str, str]] = []
    if spec.targetnode and spec.refnamespace and spec.idproperty:
        checks.append((spec.idproperty, spec.refnamespace))
    if spec.targetedge and spec.fromnode and spec.tonode:
        for key, label in (("from_id", spec.fromnode), ("to_id", spec.tonode)):
            namespace = declared_ref_namespace(domain_spec, label)
            if namespace:
                checks.append((key, namespace))
    defects = []
    for index, row in enumerate(batch):
        if not isinstance(row, dict):
            continue
        for key, namespace in checks:
            defect = _ref_defect(row.get(key), namespace)
            if defect is not None:
                defects.append(f"row {index}: {key} {defect}")
    return defects


def _row_association(value: Any) -> tuple[str, str, str, str, int] | None:
    """The row's ``observed_association`` as an association key, or None when malformed."""
    if not isinstance(value, dict):
        return None
    try:
        return ObservedAssociationSourceSpec.model_validate(value).key
    except ValueError:
        return None


def observed_association_defects(spec: SyncEndpointSpec, batch: list[Any], *, require: bool) -> list[str]:
    """Observed relationship rows whose claimed provider association CEG's own spec does not declare (D-9).

    The tenant spec held by CEG is the authority for which provider associations a
    relationship endpoint accepts (``observedfrom``); a producer's mirror of it is
    not. A row that carries ``observed_association`` must name exactly one entry
    of this endpoint's ``observedfrom``, with ``assertion_status: observed``. With
    ``require``, an observed row on an endpoint that declares ``observedfrom``
    must carry the key. Endpoints without ``observedfrom`` accept no key and are
    otherwise unchanged. Non-mapping rows are left to the row validators.
    """
    if not spec.targetedge:
        return []
    declared = {source.key for source in spec.observedfrom or []}
    defects = []
    for index, row in enumerate(batch):
        if not isinstance(row, dict):
            continue
        observed = row.get("assertion_status") == "observed"
        if OBSERVED_ASSOCIATION_KEY not in row:
            if require and observed and declared:
                defects.append(
                    f"row {index}: {OBSERVED_ASSOCIATION_KEY} is required on an observed row for an "
                    "endpoint that declares observedfrom"
                )
            continue
        if not observed:
            defects.append(f"row {index}: {OBSERVED_ASSOCIATION_KEY} is accepted only with assertion_status observed")
            continue
        key = _row_association(row[OBSERVED_ASSOCIATION_KEY])
        if key is None:
            defects.append(
                f"row {index}: {OBSERVED_ASSOCIATION_KEY} must be an object with exactly provider, "
                "fromobjecttype, toobjecttype, associationcategory and associationtypeid"
            )
        elif key not in declared:
            defects.append(
                f"row {index}: {OBSERVED_ASSOCIATION_KEY} {key} is not declared by this endpoint's observedfrom"
            )
    return defects


def relationship_precheck_query(declaration: RelationshipDeclaration) -> str:
    """Per row: how many tenant / any-tenant endpoints match, and edges already holding its evidence_ref."""
    edge_type = sanitize_label(declaration.edge_type)
    from_label = sanitize_label(declaration.from_label)
    from_id = sanitize_label(declaration.from_id_property)
    to_label = sanitize_label(declaration.to_label)
    to_id = sanitize_label(declaration.to_id_property)
    return "\n".join(
        [
            "UNWIND $batch AS row",
            f"OPTIONAL MATCH (a:{from_label} {{{from_id}: row.from_id}})",
            "WITH row, collect(a) AS sources",
            f"OPTIONAL MATCH (b:{to_label} {{{to_id}: row.to_id}})",
            "WITH row, sources, collect(b) AS targets",
            f"OPTIONAL MATCH (x:{from_label})-[r:{edge_type} {{evidence_ref: row.evidence_ref}}]->(y:{to_label})",
            "WHERE r._tenant = $tenant",
            "WITH row, sources, targets, collect(",
            f"  CASE WHEN r IS NULL THEN null ELSE {{from_id: x.{from_id},",
            f"  to_id: y.{to_id}, props: properties(r)}} END) AS existing",
            "RETURN row.evidence_ref AS evidence_ref,",
            "  size([s IN sources WHERE s._tenant = $tenant]) AS from_count, size(sources) AS from_any,",
            "  size([t IN targets WHERE t._tenant = $tenant]) AS to_count, size(targets) AS to_any,",
            "  existing",
        ]
    )


def relationship_merge_query(declaration: RelationshipDeclaration) -> str:
    """Create each evidence edge once; an existing edge is returned untouched."""
    edge_type = sanitize_label(declaration.edge_type)
    from_label = sanitize_label(declaration.from_label)
    from_id = sanitize_label(declaration.from_id_property)
    to_label = sanitize_label(declaration.to_label)
    to_id = sanitize_label(declaration.to_id_property)
    return "\n".join(
        [
            "UNWIND $batch AS row",
            f"MATCH (a:{from_label} {{{from_id}: row.from_id}}) WHERE a._tenant = $tenant",
            f"MATCH (b:{to_label} {{{to_id}: row.to_id}}) WHERE b._tenant = $tenant",
            f"MERGE (a)-[r:{edge_type} {{evidence_ref: row.evidence_ref}}]->(b)",
            "ON CREATE SET r += row.props, r._tenant = $tenant, r._evidence_schema = $schema,",
            f"  r._subject_ref = toString(a.{from_id}), r._object_ref = toString(b.{to_id})",
            "RETURN row.evidence_ref AS evidence_ref, properties(r) AS stored",
        ]
    )


def relationship_uniqueness_constraint_query(declaration: RelationshipDeclaration) -> str:
    """Schema statement enforcing one edge per ``(_tenant, evidence_ref)`` for the declared type.

    A transaction alone cannot stop two concurrent writers recording one
    ``evidence_ref`` on different endpoint pairs: each MERGE matches only its own
    pair. The relationship property uniqueness constraint makes the database
    refuse the second commit. Idempotent (``IF NOT EXISTS``); it is a schema
    statement and must run in its own transaction, never inside a data write.
    """
    edge_type = sanitize_label(declaration.edge_type)
    # The name is built only from the sanitised type, so it is a plain identifier.
    return (
        f"CREATE CONSTRAINT ceg_relationship_evidence_{edge_type} IF NOT EXISTS "
        f"FOR ()-[r:{edge_type}]-() REQUIRE (r._tenant, r.evidence_ref) IS UNIQUE"
    )


def stored_evidence(stored: dict[str, Any]) -> dict[str, Any]:
    """The row-supplied part of a stored evidence edge's properties."""
    return {key: value for key, value in stored.items() if key not in RELATIONSHIP_INTERNAL_PROPERTIES}
