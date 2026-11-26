"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [health]
tags: [health, audit, temporal, sync, read-only]
owner: engine-team
status: active
--- /L9_META ---

Read-only detector: declared temporal properties stored as STRING (D1).

Before native temporal storage (ceg-scoring-audit L1, PR #563) a declared
``datetime`` / ``date`` / ``duration`` was written as the ISO string the row carried, which
every temporal comparison in the compiled match query rejects. This module
counts, per tenant database, how many stored values of each declared temporal
property (node and relationship, scalar and ``list<temporal>``) are still
strings. It converts nothing: the operator decision (D1) is that historical
data is converted only under a separate approval, and only if this finds any.

Every statement runs through ``GraphDriver.execute_read`` (a managed READ
transaction: the server refuses any write, and the tenant database is never
provisioned). Labels and property names come from the tenant's own spec and
pass ``sanitize_label``; nothing is interpolated from a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.config.schema import DomainSpec, PropertySpec, PropertyType
from engine.utils.security import sanitize_label

TEMPORAL_TYPES = frozenset({PropertyType.DATETIME, PropertyType.DATE, PropertyType.DURATION})


@dataclass(frozen=True)
class TemporalProperty:
    """One declared temporal property: where it is stored and how it is declared."""

    owner: str  # "node" | "relationship"
    label: str  # node label or relationship type
    name: str
    declared: str  # the declared type, e.g. "datetime" or "list<date>"
    is_list: bool


@dataclass(frozen=True)
class AuditQuery:
    """One read-only statement: every declared temporal of one label or relationship type."""

    owner: str
    label: str
    cypher: str
    properties: tuple[TemporalProperty, ...]


def _temporal(prop: PropertySpec) -> tuple[bool, bool]:
    """(is a declared temporal, is a list of one)."""
    if prop.type in TEMPORAL_TYPES:
        return True, False
    if prop.type is PropertyType.LIST and prop.items is not None and prop.items.type in TEMPORAL_TYPES:
        return True, True
    return False, False


def declared_temporal_properties(domain_spec: DomainSpec) -> list[TemporalProperty]:
    """Every storable temporal the spec declares, in declaration order."""
    found: list[TemporalProperty] = []
    owners: list[tuple[str, str, list[PropertySpec]]] = [
        ("node", node.label, node.properties) for node in domain_spec.ontology.nodes
    ]
    owners.extend(("relationship", edge.type, edge.properties) for edge in domain_spec.ontology.edges)
    for owner, label, properties in owners:
        for prop in properties:
            temporal, is_list = _temporal(prop)
            if temporal:
                found.append(TemporalProperty(owner, label, prop.name, prop.declared_type, is_list))
    return found


def _string_count(variable: str, prop: TemporalProperty, alias: str) -> str:
    """``count`` of entities whose stored value (or any list element) is a STRING."""
    ref = f"{variable}.{sanitize_label(prop.name)}"
    if prop.is_list:
        test = f"{ref} IS :: LIST<ANY> NOT NULL AND any(element IN {ref} WHERE element IS :: STRING NOT NULL)"
    else:
        test = f"{ref} IS :: STRING NOT NULL"
    return f"count(CASE WHEN {test} THEN 1 END) AS {alias}"


def build_audit_queries(domain_spec: DomainSpec) -> list[AuditQuery]:
    """One aggregate read per label / relationship type that declares a temporal."""
    grouped: dict[tuple[str, str], list[TemporalProperty]] = {}
    for prop in declared_temporal_properties(domain_spec):
        grouped.setdefault((prop.owner, prop.label), []).append(prop)
    queries: list[AuditQuery] = []
    for (owner, label), props in grouped.items():
        # Label / type from the tenant's own spec, through sanitize_label (CYPHER_SAFETY).
        safe = sanitize_label(label)
        pattern = "MATCH (e:" + safe + ")" if owner == "node" else "MATCH ()-[e:" + safe + "]->()"
        counts = ", ".join(_string_count("e", prop, f"c{index}") for index, prop in enumerate(props))
        queries.append(AuditQuery(owner, label, f"{pattern} RETURN {counts}", tuple(props)))
    return queries


def summarise(domain_spec: DomainSpec, results: list[tuple[AuditQuery, list[dict[str, Any]]]]) -> dict[str, Any]:
    """The audit report: one finding per declared temporal, with its string-valued count."""
    findings: list[dict[str, Any]] = []
    for query, rows in results:
        row = rows[0] if rows else {}
        for index, prop in enumerate(query.properties):
            findings.append(
                {
                    "owner": prop.owner,
                    "label": prop.label,
                    "property": prop.name,
                    "declared": prop.declared,
                    "string_valued": int(row.get(f"c{index}") or 0),
                }
            )
    total = sum(finding["string_valued"] for finding in findings)
    return {
        "domain": domain_spec.domain.id,
        "database": domain_spec.domain.id,
        "checked": len(findings),
        "string_valued_total": total,
        "conversion_required": total > 0,
        "findings": findings,
    }


async def audit_temporal_storage(graph_driver: Any, domain_spec: DomainSpec) -> dict[str, Any]:
    """Count string-valued declared temporals in the tenant's database. Read-only; writes nothing."""
    results: list[tuple[AuditQuery, list[dict[str, Any]]]] = []
    for query in build_audit_queries(domain_spec):
        rows = await graph_driver.execute_read(query.cypher, {}, database=domain_spec.domain.id)
        results.append((query, rows))
    return summarise(domain_spec, results)
