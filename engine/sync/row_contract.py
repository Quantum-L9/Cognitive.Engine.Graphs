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


_ISO_DURATION = re.compile(
    r"P(?!$)(?:\d+(?:\.\d+)?Y)?(?:\d+(?:\.\d+)?M)?(?:\d+(?:\.\d+)?W)?(?:\d+(?:\.\d+)?D)?"
    r"(?:T(?=\d)(?:\d+(?:\.\d+)?H)?(?:\d+(?:\.\d+)?M)?(?:\d+(?:\.\d+)?S)?)?"
)

_MISSING = object()


def actual_type(value: Any) -> str:
    """The JSON type of a row value, in the vocabulary declarations use."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "list"
    if isinstance(value, dict):
        return "map"
    return type(value).__name__


def _iso(value: str, parse: Any) -> bool:
    try:
        parse(value)
    except ValueError:
        return False
    return True


# ── scalar checks: one per declared type, each returning the ``got`` of a defect ──


ScalarCheck = Callable[[Any, list[str] | None], str | None]


def _type_check(accepts: Callable[[Any], bool]) -> ScalarCheck:
    """A check that only tests the JSON type."""

    def check(value: Any, _values: list[str] | None) -> str | None:
        return None if accepts(value) else actual_type(value)

    return check


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_float(value: Any, _values: list[str] | None) -> str | None:
    if not _is_number(value):
        return actual_type(value)
    return None if math.isfinite(value) else f"{actual_type(value)} (non-finite)"


def _text_check(valid: Callable[[str], bool], what: str) -> ScalarCheck:
    """A string whose text must also be ``valid`` (an ISO-8601 form)."""

    def check(value: Any, _values: list[str] | None) -> str | None:
        if not isinstance(value, str):
            return actual_type(value)
        return None if valid(value) else f"string (not an ISO-8601 {what})"

    return check


def _check_enum(value: Any, values: list[str] | None) -> str | None:
    if not isinstance(value, str):
        return actual_type(value)
    return "string (not a declared value)" if values and value not in values else None


_SCALAR_CHECKS: dict[PropertyType, ScalarCheck] = {
    PropertyType.STRING: _type_check(lambda value: isinstance(value, str)),
    PropertyType.INT: _type_check(_is_int),
    PropertyType.FLOAT: _check_float,
    PropertyType.BOOL: _type_check(lambda value: isinstance(value, bool)),
    PropertyType.DATETIME: _text_check(lambda text: _iso(text, datetime.fromisoformat), "datetime"),
    PropertyType.DATE: _text_check(lambda text: _iso(text, date.fromisoformat), "date"),
    PropertyType.DURATION: _text_check(lambda text: _ISO_DURATION.fullmatch(text) is not None, "duration"),
    PropertyType.ENUM: _check_enum,
}


def _scalar_mismatch(type_: PropertyType, values: list[str] | None, value: Any) -> str | None:
    """How ``value`` fails a scalar declaration (the ``got`` part of a defect), else None."""
    check = _SCALAR_CHECKS.get(type_)
    if check is None:  # shapes are dispatched before scalars
        return actual_type(value)
    return check(value, values)


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

    @classmethod
    def from_spec(cls, domain_spec: DomainSpec, item: PropertySpec, child: ChildSyncSpec) -> ChildProjection:
        """Writable keys: declared by the item map and by the child node, storable on both."""
        child_node = _node(domain_spec, child.targetnode)
        child_props = _properties(child_node)
        writable = frozenset(
            prop.name
            for prop in _item_properties(item)
            if prop.name in child_props and not prop.wire_only and not child_props[prop.name].wire_only
        )
        static = child_node is not None and child_node.managedby is ManagedByType.STATIC
        return cls(child.field, child.targetid, item, writable, static)

    def project(self, items: Any) -> list[dict[str, Any]]:
        """Each item map keeping only the writable keys; non-maps are dropped."""
        return [
            {key: value for key, value in item.items() if key in self.writable}
            for item in items or []
            if isinstance(item, dict)
        ]


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
                ChildProjection.from_spec(domain_spec, props[child.field], child) for child in endpoint.childsync or []
            ),
            taxonomy_lists=frozenset(
                edge.field for edge in endpoint.taxonomyedges or [] if props[edge.field].type is PropertyType.LIST
            ),
        )

    @property
    def stored(self) -> frozenset[str]:
        """Node properties the generator writes: declared and storable."""
        return frozenset(name for name, prop in self.properties.items() if not prop.wire_only)

    def _walk_field(self, walk: _Walk, index: int, key: str, value: Any) -> None:
        """One top-level key of a row: undeclared, null, or a value to check."""
        prop = self.properties.get(key)
        if prop is None:
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

    def _walk(self, batch: list[Any], *, refuse_undeclared: bool) -> _Walk:
        walk = _Walk(refuse_undeclared=refuse_undeclared)
        for index, row in enumerate(batch):
            self._walk_row(walk, index, row)
        return walk

    def defects(self, batch: list[Any], *, refuse_undeclared: bool) -> list[str]:
        """Every defect of every row; empty when the whole batch conforms."""
        return self._walk(batch, refuse_undeclared=refuse_undeclared).defects

    def _project_row(self, row: dict[str, Any], stored: frozenset[str]) -> dict[str, Any]:
        return {
            "node": {key: value for key, value in row.items() if key in stored},
            "children": {child.field: child.project(row.get(child.field)) for child in self.children},
        }

    def project(self, batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Rows in write shape, plus how often each undeclared key was dropped.

        ``node`` keeps only declared, storable properties present in the row
        (absent stays absent, so ``SET n += row.node`` leaves them unchanged;
        an explicit null still removes one). ``children`` keeps, per childsync
        field, each item's properties the child node declares.
        """
        dropped = self._walk(batch, refuse_undeclared=False).undeclared
        stored = self.stored
        return [self._project_row(row, stored) for row in batch], dict(sorted(dropped.items()))
