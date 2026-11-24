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

Typed sync row contract, derived entirely from the tenant's domain spec.

The spec declares every property a node sync endpoint writes, including the
structured shapes a row carries on the wire: ``list`` (with ``items``) and
``map`` (with ``properties``). Neo4j stores primitives and arrays of
primitives only, so:

* ``list<primitive>`` is a storable node property;
* ``map`` and ``list<map>`` are wire-only shapes. A ``list<map>`` is legal on a
  node only as the ``field`` of a ``childsync`` on a sync endpoint targeting
  that node; nothing consumes a bare ``map`` or a nested wire-only shape, so
  declaring one is an admission defect.

This module owns three things, all domain-neutral:

* ``stored_shape_defects`` / ``endpoint_shape_defects`` — admission: the spec
  declares every field its sync endpoints read, in a shape the generator can
  write (``engine/config/loader.py::admission_defects``).
* ``SyncRowContract.defects`` — ingress: every row of a batch against the
  declared types, before compliance logging or any Cypher (``handle_sync``).
* ``SyncRowContract.project`` — the explicit projection the generator writes:
  ``{"node": <declared storable properties present in the row>,
  "children": {<childsync field>: [<item with only writable properties>]}}``.

Values are never coerced: a JSON string is not parsed into a list, ``"high"``
is not a float. Defects name the row, the field path, the declared type and the
actual JSON type, never the value (a value may be PII).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.config.schema import (
    SHAPE_TYPES,
    ChildSyncSpec,
    DomainSpec,
    EdgeSpec,
    ItemSpec,
    ManagedByType,
    NodeSpec,
    PropertySpec,
    PropertyType,
    SyncEndpointSpec,
    TaxonomyEdgeSpec,
    render_declared_type,
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


# ── value provenance (#351 revision; naming agreed with EIE #419) ─────────────
#
# A node row field ``F`` is provenance-governed when its node also declares
# ``F_provenance: enum[observed|enriched]``. A childsync ``list<map>`` item is
# governed as a whole when its item map declares one ``provenance`` property
# with that enum; it is stored as ``provenance`` on whatever receives the item's
# keys (the line edge, or the child node). EIE sends the provenance beside the
# value and CEG stores it beside the value; with
# ``sync_provenance_precedence_enabled`` the sync Cypher compares the incoming
# provenance with the stored one. A field or item that declares none stays in
# legacy mode. The names and the two values are the agreed wire vocabulary,
# never a domain term.

PROVENANCE_SUFFIX = "_provenance"
ITEM_PROVENANCE = "provenance"
PROVENANCE_OBSERVED = "observed"
PROVENANCE_ENRICHED = "enriched"
PROVENANCE_VALUES = frozenset({PROVENANCE_OBSERVED, PROVENANCE_ENRICHED})


def provenance_companion(name: str) -> str:
    """The declared property that carries ``name``'s provenance."""
    return f"{name}{PROVENANCE_SUFFIX}"


def _is_provenance_declaration(prop: PropertySpec) -> bool:
    return prop.type is PropertyType.ENUM and set(prop.values or []) == PROVENANCE_VALUES


def provenance_governed(properties: Sequence[PropertySpec]) -> frozenset[str]:
    """Storable properties whose ``<name>_provenance`` companion is declared ``enum[observed|enriched]``."""
    by_name = {prop.name: prop for prop in properties}
    governed: set[str] = set()
    for name, prop in by_name.items():
        companion = by_name.get(provenance_companion(name))
        if name.endswith(PROVENANCE_SUFFIX) or prop.wire_only or companion is None:
            continue
        if _is_provenance_declaration(companion):
            governed.add(name)
    return frozenset(governed)


def provenance_defects(owner: str, properties: Sequence[PropertySpec]) -> list[str]:
    """Every ``*_provenance`` declaration is the enum companion of a storable property beside it."""
    by_name = {prop.name: prop for prop in properties}
    defects: list[str] = []
    for name, prop in by_name.items():
        if not name.endswith(PROVENANCE_SUFFIX):
            continue
        base = by_name.get(name[: -len(PROVENANCE_SUFFIX)])
        if base is None or base.wire_only:
            defects.append(
                f"{owner} property {name!r}: provenance companion of no storable property declared beside it"
            )
        if not _is_provenance_declaration(prop):
            defects.append(
                f"{owner} property {name!r}: provenance companion must be declared "
                f"enum[{'|'.join(sorted(PROVENANCE_VALUES))}], found {prop.declared_type}"
            )
    return defects


def item_provenance_declared(properties: Sequence[PropertySpec]) -> bool:
    """The scope declares one item-level ``provenance`` as ``enum[observed|enriched]``."""
    declared = {prop.name: prop for prop in properties}.get(ITEM_PROVENANCE)
    return declared is not None and _is_provenance_declaration(declared)


def item_provenance_defects(owner: str, properties: Sequence[PropertySpec], *, field_companions: bool) -> list[str]:
    """An item-level ``provenance`` is the enum; an item map carries no per-field companion."""
    defects: list[str] = []
    for prop in properties:
        if prop.name == ITEM_PROVENANCE and not _is_provenance_declaration(prop):
            defects.append(
                f"{owner} property {ITEM_PROVENANCE!r}: item provenance must be declared "
                f"enum[{'|'.join(sorted(PROVENANCE_VALUES))}], found {prop.declared_type}"
            )
        if not field_companions and prop.name.endswith(PROVENANCE_SUFFIX):
            defects.append(
                f"{owner} property {prop.name!r}: an item declares one {ITEM_PROVENANCE!r}, not a per-field companion"
            )
    return defects


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
    defects.extend([node_defect] if node_defect else [])
    defects.extend(_line_identity_defects(scope, child, declared))
    return defects


def _line_identity_defects(scope: _EndpointScope, child: ChildSyncSpec, declared: PropertySpec | None) -> list[str]:
    """edgeidentity / edgeproperties / fromrow are scalars of the item and of the edge."""
    names = list(child.edgeproperties or [])
    carried = list(child.fromrow or [])
    if child.edgeidentity is None and not names and not carried:
        return []
    defects: list[str] = []
    if child.edgeidentity is None or child.edgeidentity not in names:
        defects.append(f"{scope.where}: childsync {child.field!r} edgeidentity must be one of its edgeproperties")
    if child.targetid in names or child.edgeidentity == child.targetid:
        defects.append(
            f"{scope.where}: childsync {child.field!r} must not use targetid {child.targetid!r} as an edge property"
        )
    item_props = {prop.name: prop for prop in _item_properties(declared)}
    if child.edgedirection == "childtoparent":
        source_label, target_label = child.targetnode, scope.target.label
    else:
        source_label, target_label = scope.target.label, child.targetnode
    matched = [
        item
        for item in scope.domain_spec.ontology.edges
        if item.type == child.edgetype and item.from_ == source_label and item.to == target_label
    ]
    edge = matched[0] if len(matched) == 1 else None
    if len(matched) > 1:
        defects.append(
            f"{scope.where}: childsync edgetype {child.edgetype!r} matches more than one {source_label}->{target_label} edge"
        )
    edge_props = {prop.name: prop for prop in edge.properties} if edge is not None else {}
    if edge is None:
        defects.append(f"{scope.where}: childsync edgetype {child.edgetype!r} is not declared in ontology.edges")
    for name in names:
        if not _is_scalar(item_props.get(name)):
            defects.append(
                f"{scope.where}: childsync edge property {name!r} must be a scalar property of {child.field}'s items"
            )
        if edge is not None and not _is_scalar(edge_props.get(name)):
            defects.append(
                f"{scope.where}: childsync edge property {name!r} must be a scalar property of edge {child.edgetype}"
            )
    for name in carried:
        if name not in names:
            defects.append(f"{scope.where}: childsync fromrow {name!r} must be one of edgeproperties {names}")
    return defects


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
    from engine.config.settings import settings

    if settings.sync_provenance_precedence_enabled:
        defects.extend(_provenance_scope_defects(scope, endpoint))
    return defects


def childsync_edges(domain_spec: DomainSpec, parent_label: str, child: ChildSyncSpec) -> list[EdgeSpec]:
    """The ontology edges a childsync writes: its type between its parent and child, in its direction."""
    source, target = (
        (child.targetnode, parent_label)
        if child.edgedirection == "childtoparent"
        else (
            parent_label,
            child.targetnode,
        )
    )
    return [
        edge
        for edge in domain_spec.ontology.edges
        if edge.type == child.edgetype and edge.from_ == source and edge.to == target
    ]


def _item_provenance_store_defect(scope: _EndpointScope, child: ChildSyncSpec, edges: list[EdgeSpec]) -> str | None:
    """A declared item ``provenance`` must reach the line edge or the child node, never be dropped."""
    from engine.config.settings import settings

    child_node = _node(scope.domain_spec, child.targetnode)
    # Line edges are written only with buyer_line_identity_enabled; otherwise item keys reach the child node.
    if child.edgeproperties and settings.buyer_line_identity_enabled:
        if ITEM_PROVENANCE in child.edgeproperties and any(item_provenance_declared(e.properties) for e in edges):
            return None
        where = f"edgeproperties and on edge {child.edgetype}"
    elif child_node is not None and item_provenance_declared(child_node.properties):
        return None
    else:
        where = f"on child node {child.targetnode}"
    return (
        f"{scope.where}: childsync {child.field!r} declares item {ITEM_PROVENANCE!r}; it must also be declared {where}"
    )


def _provenance_scope_defects(scope: _EndpointScope, endpoint: SyncEndpointSpec) -> list[str]:
    """Provenance companions on the target node, each childsync item map, child node and edge."""
    defects = provenance_defects(f"{scope.where}: {scope.target.label}", scope.target.properties)
    governed = provenance_governed(scope.target.properties)
    updated = set(endpoint.fieldsupdated or [])
    for name in sorted(governed):
        if provenance_companion(name) in updated and name not in updated:
            defects.append(
                f"{scope.where}: fieldsupdated {provenance_companion(name)!r} moves only with {name!r}; "
                f"list {name!r} too or drop the companion"
            )
    for child in endpoint.childsync or []:
        item = _item_properties(scope.props.get(child.field))
        defects.extend(item_provenance_defects(f"{scope.where}: {child.field} items", item, field_companions=False))
        edges = childsync_edges(scope.domain_spec, scope.target.label, child)
        if item_provenance_declared(item):
            store_defect = _item_provenance_store_defect(scope, child, edges)
            defects.extend([store_defect] if store_defect else [])
        child_node = _node(scope.domain_spec, child.targetnode)
        if child_node is not None:
            # A child node may be another endpoint's target, with field companions of its own.
            owner = f"{scope.where}: {child_node.label}"
            defects.extend(item_provenance_defects(owner, child_node.properties, field_companions=True))
        for edge in edges:
            owner = f"{scope.where}: edge {edge.type}"
            defects.extend(item_provenance_defects(owner, edge.properties, field_companions=False))
    return defects


_ISO_DURATION = re.compile(
    r"P(?!$)(?:\d+(?:\.\d+)?Y)?(?:\d+(?:\.\d+)?M)?(?:\d+(?:\.\d+)?W)?(?:\d+(?:\.\d+)?D)?"
    r"(?:T(?=\d)(?:\d+(?:\.\d+)?H)?(?:\d+(?:\.\d+)?M)?(?:\d+(?:\.\d+)?S)?)?"
)

_MISSING = object()


# JSON type names in the vocabulary declarations use; bool before int (bool is an int).
_JSON_TYPES: tuple[tuple[type | tuple[type, ...], str], ...] = (
    (bool, "bool"),
    (int, "int"),
    (float, "float"),
    (str, "string"),
    ((list, tuple), "list"),
    (dict, "map"),
)


def actual_type(value: Any) -> str:
    """The JSON type of a row value, in the vocabulary declarations use."""
    if value is None:
        return "null"
    return next((name for kind, name in _JSON_TYPES if isinstance(value, kind)), type(value).__name__)


def _iso(value: str, parse: Any) -> bool:
    try:
        parse(value)
    except ValueError:
        return False
    return True


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


# Per scalar type: (accepts the JSON type, rule an accepted value must also meet, what the rule names).
_SCALAR_RULES: dict[PropertyType, tuple[Callable[[Any], bool], Callable[[Any, list[str] | None], bool], str]] = {
    PropertyType.STRING: (_is_str, lambda _v, _values: True, ""),
    PropertyType.INT: (lambda v: isinstance(v, int) and not isinstance(v, bool), lambda _v, _values: True, ""),
    PropertyType.FLOAT: (
        lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        lambda v, _values: math.isfinite(v),
        "non-finite",
    ),
    PropertyType.BOOL: (lambda v: isinstance(v, bool), lambda _v, _values: True, ""),
    PropertyType.DATETIME: (_is_str, lambda v, _values: _iso(v, datetime.fromisoformat), "not an ISO-8601 datetime"),
    PropertyType.DATE: (_is_str, lambda v, _values: _iso(v, date.fromisoformat), "not an ISO-8601 date"),
    PropertyType.DURATION: (
        _is_str,
        lambda v, _values: _ISO_DURATION.fullmatch(v) is not None,
        "not an ISO-8601 duration",
    ),
    PropertyType.ENUM: (_is_str, lambda v, values: not values or v in values, "not a declared value"),
}


def _scalar_mismatch(type_: PropertyType, values: list[str] | None, value: Any) -> str | None:
    """How ``value`` fails a scalar declaration (the ``got`` part of a defect), else None."""
    got = actual_type(value)
    rule = _SCALAR_RULES.get(type_)
    if rule is None or not rule[0](value):  # shapes are dispatched before scalars
        return got
    _accepts, valid, what = rule
    return None if valid(value, values) else f"{got} ({what})"


@dataclass
class _Walk:
    """Accumulates defects and undeclared keys for one batch."""

    refuse_undeclared: bool
    defects: list[str] = field(default_factory=list)
    undeclared: Counter[str] = field(default_factory=Counter)

    def defect(self, row: int, path: str, declared: str, got: str) -> None:
        self.defects.append(f"row {row} {path}: expected {declared}, got {got}")

    def undeclared_key(self, row: int, path: str, *, counted_as: str, value: Any) -> None:
        if self.refuse_undeclared:
            self.defect(row, path, "no such declared property", actual_type(value))
        else:
            self.undeclared[counted_as] += 1


@dataclass(frozen=True)
class _At:
    """Where a value sits: its row, its defect path and the key path undeclared keys count under."""

    walk: _Walk
    row: int
    path: str
    counted_as: str

    def defect(self, declared: str, got: str) -> None:
        self.walk.defect(self.row, self.path, declared, got)

    def child(self, path_suffix: str, counted_suffix: str) -> _At:
        return _At(self.walk, self.row, f"{self.path}{path_suffix}", f"{self.counted_as}{counted_suffix}")


def _check_list(at: _At, decl: PropertySpec | ItemSpec, value: Any) -> None:
    items = getattr(decl, "items", None)
    if not isinstance(value, list):
        at.defect(render_declared_type(decl.type, decl.values, items), actual_type(value))
        return
    if items is None:  # unreachable: PropertySpec.validate_shape requires items
        at.defect(render_declared_type(decl.type, decl.values, items), "undeclared element type")
        return
    item_declared = render_declared_type(items.type, items.values, None)
    for index, element in enumerate(value):
        element_at = at.child(f"[{index}]", "[]")
        if element is None:
            element_at.defect(item_declared, "null")
        else:
            _check_value(element_at, items, element)


def _check_map_value(at: _At, decl: PropertySpec | ItemSpec, value: Any) -> None:
    if not isinstance(value, dict):
        at.defect(render_declared_type(decl.type, decl.values, None), actual_type(value))
        return
    _check_map(at, decl.properties or [], value)


def _check_scalar(at: _At, decl: PropertySpec | ItemSpec, value: Any) -> None:
    got = _scalar_mismatch(decl.type, decl.values, value)
    if got is not None:
        at.defect(render_declared_type(decl.type, decl.values, None), got)


_SHAPE_CHECKS: dict[PropertyType, Callable[[_At, PropertySpec | ItemSpec, Any], None]] = {
    PropertyType.LIST: _check_list,
    PropertyType.MAP: _check_map_value,
}


def _check_value(at: _At, decl: PropertySpec | ItemSpec, value: Any) -> None:
    """Check a non-null ``value`` against ``decl``; lists and maps recurse."""
    _SHAPE_CHECKS.get(decl.type, _check_scalar)(at, decl, value)


def _check_member(at: _At, prop: PropertySpec, member: Any) -> None:
    """One declared map member: missing, null, or a value to check."""
    if member is _MISSING:
        if prop.required:
            at.defect(f"{prop.declared_type} (required)", "missing")
    elif member is None:
        if prop.required or not prop.nullable:
            at.defect(f"{prop.declared_type} (non-null)", "null")
    else:
        _check_value(at, prop, member)


def _check_map(at: _At, properties: list[PropertySpec], value: dict[str, Any]) -> None:
    declared = {prop.name: prop for prop in properties}
    for prop in properties:
        _check_member(at.child(f".{prop.name}", f".{prop.name}"), prop, value.get(prop.name, _MISSING))
    for key in value:
        if key not in declared:
            at.walk.undeclared_key(at.row, f"{at.path}.{key}", counted_as=f"{at.counted_as}.{key}", value=value[key])


@dataclass(frozen=True)
class ChildProjection:
    """One childsync block: which item keys reach the child node, and whether it is static."""

    field: str
    targetid: str
    item: PropertySpec  # the list<map> declaration on the parent
    writable: frozenset[str]  # item ∩ child node declared, storable
    static: bool
    edge_keys: frozenset[str]  # item keys written on the relationship, not the child node
    fromrow: tuple[str, ...]  # parent-row keys that fill an item missing that edge key
    # Keys under the item-level ``provenance``: on the child node (item and
    # child node both declare it) and on the line edge (item and edge do).
    governed_node: frozenset[str] = frozenset()
    governed_edge: frozenset[str] = frozenset()

    @classmethod
    def from_spec(
        cls, domain_spec: DomainSpec, item: PropertySpec, child: ChildSyncSpec, parent_label: str = ""
    ) -> ChildProjection:
        """Writable keys: declared by the item map and by the child node, storable on both."""
        child_node = _node(domain_spec, child.targetnode)
        child_props = _properties(child_node)
        writable = frozenset(
            prop.name
            for prop in _item_properties(item)
            if prop.name in child_props and not prop.wire_only and not child_props[prop.name].wire_only
        )
        item_names = {prop.name for prop in _item_properties(item)}
        edge_keys = frozenset(name for name in (child.edgeproperties or []) if name in item_names)
        fromrow = tuple(name for name in (child.fromrow or []) if name in edge_keys)
        static = child_node is not None and child_node.managedby is ManagedByType.STATIC
        # Item-level provenance: governs every non-identity key the item writes.
        item_governed = item_provenance_declared(_item_properties(item))
        governed_node: frozenset[str] = frozenset()
        if item_governed and child_node is not None and item_provenance_declared(child_node.properties):
            if ITEM_PROVENANCE in writable:
                governed_node = writable - {ITEM_PROVENANCE, child.targetid}
        governed_edge: frozenset[str] = frozenset()
        edges = childsync_edges(domain_spec, parent_label, child)
        if (
            item_governed
            and ITEM_PROVENANCE in edge_keys
            and any(item_provenance_declared(e.properties) for e in edges)
        ):
            governed_edge = edge_keys - {ITEM_PROVENANCE, child.edgeidentity or ""}
        return cls(
            child.field, child.targetid, item, writable, static, edge_keys, fromrow, governed_node, governed_edge
        )

    def carried_prop(self, key: str) -> PropertySpec | None:
        """The item declaration of a parent-row key this child copies onto its edge."""
        if key not in self.fromrow:
            return None
        for prop in _item_properties(self.item):
            if prop.name == key:
                return prop
        return None

    def project(self, items: Any, parent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Each item map keeping only the writable keys; non-maps are dropped.

        With buyer line identity on, edge keys are kept too, and a parent-row
        ``fromrow`` value fills an item that does not carry that key. The item
        value wins, including a blank string.
        """
        from engine.config.settings import settings

        keep = self.writable
        fill: dict[str, Any] = {}
        if settings.buyer_line_identity_enabled and self.edge_keys:
            keep = self.writable | self.edge_keys
            source = parent or {}
            fill = {key: source[key] for key in self.fromrow if key in source}
        projected: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            row = {key: value for key, value in item.items() if key in keep}
            for key, value in fill.items():
                if key not in row:
                    row[key] = value
            projected.append(row)
        return projected


@dataclass(frozen=True)
class SyncRowContract:
    """What one node sync endpoint's rows may carry, built from the tenant spec."""

    path: str
    target_label: str
    idproperty: str
    properties: dict[str, PropertySpec]
    children: tuple[ChildProjection, ...]
    taxonomy_lists: frozenset[str]  # taxonomy fields declared list<scalar>

    @classmethod
    def from_spec(cls, domain_spec: DomainSpec, endpoint: SyncEndpointSpec) -> SyncRowContract:
        """Build the contract, or raise ``ValueError`` listing the endpoint's shape defects."""
        defects = endpoint_shape_defects(domain_spec, endpoint)
        if endpoint.targetedge or not endpoint.targetnode or not endpoint.idproperty:
            defects = defects or [f"sync {endpoint.path}: not a node sync endpoint (targetnode + idproperty)"]
        if defects:
            raise ValueError("; ".join(defects))
        props = _properties(_node(domain_spec, endpoint.targetnode))
        return cls(
            path=endpoint.path,
            target_label=str(endpoint.targetnode),
            idproperty=str(endpoint.idproperty),
            properties=props,
            children=tuple(
                ChildProjection.from_spec(domain_spec, props[child.field], child, str(endpoint.targetnode))
                for child in endpoint.childsync or []
            ),
            taxonomy_lists=frozenset(
                edge.field for edge in endpoint.taxonomyedges or [] if props[edge.field].type is PropertyType.LIST
            ),
        )

    @property
    def stored(self) -> frozenset[str]:
        """Node properties the generator writes: declared and storable."""
        return frozenset(name for name, prop in self.properties.items() if not prop.wire_only)

    @property
    def governed(self) -> frozenset[str]:
        """Stored target-node properties whose provenance companion is declared (never the id)."""
        governed = provenance_governed(list(self.properties.values())) - {self.idproperty}
        return frozenset(name for name in governed if provenance_companion(name) in self.stored)

    def _walk_field(self, walk: _Walk, index: int, key: str, value: Any) -> None:
        """One top-level key of a row: undeclared, null, or a value to check."""
        prop = self.properties.get(key)
        if prop is None:
            carried = self._carried(key)
            if carried is not None:
                self._check_carried(walk, index, key, carried, value)
                return
            walk.undeclared_key(index, key, counted_as=key, value=value)
        elif value is None:
            if key != self.idproperty and not prop.nullable:
                walk.defect(index, key, f"{prop.declared_type} (non-null)", "null")
        else:
            _check_value(_At(walk, index, key, key), prop, value)

    def _walk_row(self, walk: _Walk, index: int, row: Any) -> None:
        if not isinstance(row, dict):
            walk.defects.append(f"row {index}: expected map, got {actual_type(row)}")
            return
        if row.get(self.idproperty) is None:
            got = "missing" if self.idproperty not in row else "null"
            id_declared = self.properties[self.idproperty].declared_type
            walk.defect(index, self.idproperty, f"{id_declared} (required id)", got)
        for key, value in row.items():
            self._walk_field(walk, index, key, value)

    def _carried(self, key: str) -> PropertySpec | None:
        """A parent-row key a childsync copies onto its relationship, when line identity is on."""
        from engine.config.settings import settings

        if not settings.buyer_line_identity_enabled:
            return None
        for child in self.children:
            carried = child.carried_prop(key)
            if carried is not None:
                return carried
        return None

    def _check_carried(self, walk: _Walk, index: int, key: str, carried: PropertySpec, value: Any) -> None:
        """Type-check a fromrow value. It is declared, so it is not an undeclared drop."""
        if value is None:
            if not carried.nullable:
                walk.defect(index, key, f"{carried.declared_type} (non-null)", "null")
            return
        _check_value(_At(walk, index, key, key), carried, value)

    def _walk(self, batch: list[Any], *, refuse_undeclared: bool) -> _Walk:
        walk = _Walk(refuse_undeclared=refuse_undeclared)
        for index, row in enumerate(batch):
            self._walk_row(walk, index, row)
        return walk

    def defects(self, batch: list[Any], *, refuse_undeclared: bool) -> list[str]:
        """Every defect of every row; empty when the whole batch conforms."""
        return self._walk(batch, refuse_undeclared=refuse_undeclared).defects

    def project(self, batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Rows in write shape, plus how often each undeclared key was dropped.

        ``node`` keeps only declared, storable properties present in the row
        (absent stays absent, so ``SET n += row.node`` leaves them unchanged;
        an explicit null still removes one). ``children`` keeps, per childsync
        field, each item's properties the child node declares. An omitted child
        field is null and does not replace keyed relationships; a present list does.
        """
        dropped = self._walk(batch, refuse_undeclared=False).undeclared
        stored = self.stored
        rows = [
            {
                "node": {key: value for key, value in row.items() if key in stored},
                "children": {
                    child.field: None if child.field not in row else child.project(row[child.field], row)
                    for child in self.children
                },
            }
            for row in batch
        ]
        return rows, dict(sorted(dropped.items()))
