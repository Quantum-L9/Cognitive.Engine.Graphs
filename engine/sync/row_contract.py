"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [sync, validation, contract, domain-spec]
owner: engine-team
status: active
--- /L9_META ---

Typed sync contract, derived entirely from the tenant's domain spec.

The spec declares every property a node sync endpoint writes, including the
structured shapes a row carries on the wire: ``list`` (with ``items``) and
``map`` (with ``properties``). Neo4j stores primitives and arrays of
primitives only, so:

* ``list<primitive>`` is a storable node property;
* ``map`` and ``list<map>`` are wire-only shapes. A ``list<map>`` is legal on a
  node only as the ``field`` of a ``childsync`` on a sync endpoint targeting
  that node; nothing consumes a bare ``map`` or a nested wire-only shape, so
  declaring one is an admission defect.

``stored_shape_defects`` / ``endpoint_shape_defects`` are the admission half:
the spec declares every field its sync endpoints read, in a shape a sync can
write (``engine/config/loader.py::admission_defects``). Domain-neutral: no
label, field or vocabulary is known here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from engine.config.schema import (
    SHAPE_TYPES,
    ChildSyncSpec,
    DomainSpec,
    EdgeSpec,
    NodeSpec,
    PropertySpec,
    PropertyType,
    SyncEndpointSpec,
    TaxonomyEdgeSpec,
)


def _node(domain_spec: DomainSpec, label: str | None) -> NodeSpec | None:
    for node in domain_spec.ontology.nodes:
        if node.label == label:
            return node
    return None


def _properties(node: NodeSpec | None) -> dict[str, PropertySpec]:
    return {prop.name: prop for prop in node.properties} if node is not None else {}


def _is_scalar(prop: PropertySpec | None) -> bool:
    return prop is not None and prop.type not in SHAPE_TYPES


def _is_scalar_or_scalar_list(prop: PropertySpec | None) -> bool:
    if prop is None:
        return False
    if prop.type is PropertyType.LIST:
        return prop.items is not None and prop.items.type is not PropertyType.MAP
    return prop.type is not PropertyType.MAP


def _is_list_of_map(prop: PropertySpec | None) -> bool:
    return (
        prop is not None
        and prop.type is PropertyType.LIST
        and prop.items is not None
        and prop.items.type is PropertyType.MAP
    )


def _wire_only_members(properties: list[PropertySpec], path: str) -> list[str]:
    """Wire-only shapes nested inside a consumed ``list<map>`` item: nothing consumes them."""
    return [f"{path}.{prop.name} ({prop.declared_type})" for prop in properties if prop.wire_only]


def _found(prop: PropertySpec | None) -> str:
    """How a defect names what the spec declares for a field."""
    return prop.declared_type if prop is not None else "undeclared"


def _item_properties(prop: PropertySpec | None) -> list[PropertySpec]:
    """The members of a ``list<map>`` item (empty for anything else)."""
    if prop is None or prop.items is None:
        return []
    return prop.items.properties or []


# ── stored shapes ─────────────────────────────────────────────────────────────


def _consumed_fields(domain_spec: DomainSpec) -> set[tuple[str, str]]:
    """``(targetnode, field)`` of every childsync on a node sync endpoint."""
    endpoints = domain_spec.sync.endpoints if domain_spec.sync else []
    return {
        (endpoint.targetnode, child.field)
        for endpoint in endpoints
        if endpoint.targetnode and not endpoint.targetedge
        for child in endpoint.childsync or []
    }


def _node_property_defects(label: str, prop: PropertySpec, consumed: set[tuple[str, str]]) -> list[str]:
    """A wire-only node property is legal only as a consumed ``list<map>`` with storable members."""
    if not prop.wire_only:
        return []
    if (label, prop.name) not in consumed or prop.type is PropertyType.MAP:
        defect = (
            f"ontology node {label} property {prop.name!r}: declared {prop.declared_type}, a wire-only "
            "shape Neo4j cannot store, and no childsync on a sync endpoint targeting "
            f"{label} consumes it (only list<map> is consumable)"
        )
        return [defect]
    return [
        f"ontology node {label} property {prop.name!r}: item member {member} is a wire-only shape nothing consumes"
        for member in _wire_only_members(_item_properties(prop), f"{prop.name}[]")
    ]


def _edge_property_defects(edge: EdgeSpec) -> list[str]:
    """A relationship stores no wire-only shape: nothing consumes one."""
    return [
        f"ontology edge {edge.type} property {prop.name!r}: declared {prop.declared_type}, a wire-only shape "
        "Neo4j cannot store on a relationship"
        for prop in edge.properties
        if prop.wire_only
    ]


def stored_shape_defects(domain_spec: DomainSpec) -> list[str]:
    """Declared ``map`` / ``list<map>`` properties that would be stored, because nothing consumes them.

    A node property declared ``list<map>`` is consumed when it is the ``field``
    of a ``childsync`` on a sync endpoint whose ``targetnode`` is that node; a
    ``map`` (and any wire-only shape on an edge, or nested inside a consumed
    item) has no consumer. Unconditional: no Neo4j version can store either.
    """
    consumed = _consumed_fields(domain_spec)
    defects = [
        defect
        for node in domain_spec.ontology.nodes
        for prop in node.properties
        for defect in _node_property_defects(node.label, prop, consumed)
    ]
    for edge in domain_spec.ontology.edges:
        defects.extend(_edge_property_defects(edge))
    return defects


# ── node sync endpoints ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _EndpointScope:
    """One node sync endpoint resolved against the ontology: what each field check reads."""

    domain_spec: DomainSpec
    where: str
    target: NodeSpec
    props: dict[str, PropertySpec]


def _scalar_key_defect(node: NodeSpec | None, key: str, *, role: str, where: str) -> str | None:
    """``<role>`` must name a node the ontology declares, with ``key`` a scalar of it."""
    if node is None:
        return None
    if _is_scalar(_properties(node).get(key)):
        return None
    return f"{where}: {role} targetid {key!r} must be a scalar property declared on {node.label}"


def _childsync_defects(scope: _EndpointScope, child: ChildSyncSpec) -> list[str]:
    """Field declared ``list<map>``; ``targetid`` a required scalar of the item and of the child node."""
    declared = scope.props.get(child.field)
    if not _is_list_of_map(declared):
        defect = (
            f"{scope.where}: childsync field {child.field!r} must be declared on {scope.target.label} as "
            f"list<map>, found {_found(declared)}"
        )
        return [defect]
    defects: list[str] = []
    key = {prop.name: prop for prop in _item_properties(declared)}.get(child.targetid)
    if key is None or not key.required or not _is_scalar(key):
        defects.append(
            f"{scope.where}: childsync targetid {child.targetid!r} must be a required scalar property of "
            f"{child.field}'s items"
        )
    child_node = _node(scope.domain_spec, child.targetnode)
    if child_node is None:
        defects.append(f"{scope.where}: childsync targetnode {child.targetnode!r} is not declared in ontology.nodes")
    node_defect = _scalar_key_defect(child_node, child.targetid, role="childsync", where=scope.where)
    return defects + ([node_defect] if node_defect else [])


def _taxonomy_defects(scope: _EndpointScope, edge: TaxonomyEdgeSpec) -> list[str]:
    """Field declared a scalar or ``list<scalar>``; ``targetid`` a scalar of ``targetlabel``."""
    defects: list[str] = []
    declared = scope.props.get(edge.field)
    if not _is_scalar_or_scalar_list(declared):
        defects.append(
            f"{scope.where}: taxonomy field {edge.field!r} must be declared on {scope.target.label} as a "
            f"scalar or list<scalar>, found {_found(declared)}"
        )
    taxonomy = _node(scope.domain_spec, edge.targetlabel)
    if taxonomy is None:
        defects.append(f"{scope.where}: taxonomy targetlabel {edge.targetlabel!r} is not declared in ontology.nodes")
    node_defect = _scalar_key_defect(taxonomy, edge.targetid, role="taxonomy", where=scope.where)
    return defects + ([node_defect] if node_defect else [])


def _fieldsupdated_defects(scope: _EndpointScope, name: str) -> list[str]:
    """A PATCH field is a declared, storable property of ``targetnode``."""
    declared = scope.props.get(name)
    if declared is not None and not declared.wire_only:
        return []
    defect = (
        f"{scope.where}: fieldsupdated {name!r} must be a storable property declared on {scope.target.label}, "
        f"found {_found(declared)}"
    )
    return [defect]


# Per-field dispatch, in report order: which endpoint list each check reads.
_ENDPOINT_FIELD_CHECKS: tuple[
    tuple[Callable[[SyncEndpointSpec], Sequence[Any] | None], Callable[[_EndpointScope, Any], list[str]]], ...
] = (
    (lambda endpoint: endpoint.childsync, _childsync_defects),
    (lambda endpoint: endpoint.taxonomyedges, _taxonomy_defects),
    (lambda endpoint: endpoint.fieldsupdated, _fieldsupdated_defects),
)


def endpoint_shape_defects(domain_spec: DomainSpec, endpoint: SyncEndpointSpec) -> list[str]:
    """Fields a node sync endpoint reads that its ``targetnode`` does not declare in a writable shape.

    * ``targetnode`` is an ontology node and ``idproperty`` a scalar it declares;
    * each ``childsync.field`` is declared ``list<map>`` on ``targetnode``, its
      ``targetid`` is a required scalar of the item map and a scalar property of
      the child node;
    * each ``taxonomyedges.field`` is declared a scalar or ``list<scalar>`` on
      ``targetnode`` and ``targetid`` a scalar of ``targetlabel``;
    * each ``fieldsupdated`` entry is a declared, storable ``targetnode`` property.

    Relationship endpoints (``targetedge``) have their own contract
    (``resolve_relationship_declaration``) and return no defects here.
    """
    if endpoint.targetedge or not endpoint.targetnode:
        return []
    where = f"sync {endpoint.path}"
    target = _node(domain_spec, endpoint.targetnode)
    if target is None:
        return [f"{where}: targetnode {endpoint.targetnode!r} is not declared in ontology.nodes"]
    scope = _EndpointScope(domain_spec, where, target, _properties(target))
    defects: list[str] = []
    if not _is_scalar(scope.props.get(endpoint.idproperty or "")):
        defects.append(
            f"{where}: idproperty {endpoint.idproperty!r} must be a scalar property declared on {target.label}"
        )
    for entries, check in _ENDPOINT_FIELD_CHECKS:
        for entry in entries(endpoint) or []:
            defects.extend(check(scope, entry))
    return defects
