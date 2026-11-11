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

from engine.config.schema import (
    SHAPE_TYPES,
    DomainSpec,
    NodeSpec,
    PropertySpec,
    PropertyType,
    SyncEndpointSpec,
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


def stored_shape_defects(domain_spec: DomainSpec) -> list[str]:
    """Declared ``map`` / ``list<map>`` properties that would be stored, because nothing consumes them.

    A node property declared ``list<map>`` is consumed when it is the ``field``
    of a ``childsync`` on a sync endpoint whose ``targetnode`` is that node; a
    ``map`` (and any wire-only shape on an edge, or nested inside a consumed
    item) has no consumer. Unconditional: no Neo4j version can store either.
    """
    consumed: set[tuple[str, str]] = set()
    for endpoint in domain_spec.sync.endpoints if domain_spec.sync else []:
        if endpoint.targetnode and not endpoint.targetedge:
            consumed.update((endpoint.targetnode, child.field) for child in endpoint.childsync or [])
    defects: list[str] = []
    for node in domain_spec.ontology.nodes:
        for prop in node.properties:
            if not prop.wire_only:
                continue
            if (node.label, prop.name) not in consumed or prop.type is PropertyType.MAP:
                defects.append(
                    f"ontology node {node.label} property {prop.name!r}: declared {prop.declared_type}, a wire-only "
                    "shape Neo4j cannot store, and no childsync on a sync endpoint targeting "
                    f"{node.label} consumes it (only list<map> is consumable)"
                )
            elif prop.items is not None:
                defects.extend(
                    f"ontology node {node.label} property {prop.name!r}: item member {member} is a wire-only "
                    "shape nothing consumes"
                    for member in _wire_only_members(prop.items.properties or [], f"{prop.name}[]")
                )
    for edge in domain_spec.ontology.edges:
        defects.extend(
            f"ontology edge {edge.type} property {prop.name!r}: declared {prop.declared_type}, a wire-only shape "
            "Neo4j cannot store on a relationship"
            for prop in edge.properties
            if prop.wire_only
        )
    return defects


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
    props = _properties(target)
    defects: list[str] = []
    if not _is_scalar(props.get(endpoint.idproperty or "")):
        defects.append(
            f"{where}: idproperty {endpoint.idproperty!r} must be a scalar property declared on {target.label}"
        )
    for child in endpoint.childsync or []:
        declared = props.get(child.field)
        if not _is_list_of_map(declared):
            found = declared.declared_type if declared is not None else "undeclared"
            defects.append(
                f"{where}: childsync field {child.field!r} must be declared on {target.label} as list<map>, "
                f"found {found}"
            )
            continue
        item = {prop.name: prop for prop in (declared.items.properties if declared and declared.items else None) or []}
        key = item.get(child.targetid)
        if key is None or not key.required or not _is_scalar(key):
            defects.append(
                f"{where}: childsync targetid {child.targetid!r} must be a required scalar property of "
                f"{child.field}'s items"
            )
        child_node = _node(domain_spec, child.targetnode)
        if child_node is None:
            defects.append(f"{where}: childsync targetnode {child.targetnode!r} is not declared in ontology.nodes")
        elif not _is_scalar(_properties(child_node).get(child.targetid)):
            defects.append(
                f"{where}: childsync targetid {child.targetid!r} must be a scalar property declared on "
                f"{child_node.label}"
            )
    for edge in endpoint.taxonomyedges or []:
        declared = props.get(edge.field)
        if not _is_scalar_or_scalar_list(declared):
            found = declared.declared_type if declared is not None else "undeclared"
            defects.append(
                f"{where}: taxonomy field {edge.field!r} must be declared on {target.label} as a scalar or "
                f"list<scalar>, found {found}"
            )
        taxonomy = _node(domain_spec, edge.targetlabel)
        if taxonomy is None:
            defects.append(f"{where}: taxonomy targetlabel {edge.targetlabel!r} is not declared in ontology.nodes")
        elif not _is_scalar(_properties(taxonomy).get(edge.targetid)):
            defects.append(
                f"{where}: taxonomy targetid {edge.targetid!r} must be a scalar property declared on {taxonomy.label}"
            )
    for name in endpoint.fieldsupdated or []:
        declared = props.get(name)
        if declared is None or declared.wire_only:
            found = declared.declared_type if declared is not None else "undeclared"
            defects.append(
                f"{where}: fieldsupdated {name!r} must be a storable property declared on {target.label}, found {found}"
            )
    return defects
