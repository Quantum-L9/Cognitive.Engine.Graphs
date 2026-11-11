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
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.config.schema import (
    SHAPE_TYPES,
    DomainSpec,
    ItemSpec,
    ManagedByType,
    NodeSpec,
    PropertySpec,
    PropertyType,
    SyncEndpointSpec,
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


# ISO-8601 duration (``P1Y2M``, ``PT36H``, ``P2W``); at least one component.
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


def _scalar_mismatch(type_: PropertyType, values: list[str] | None, value: Any) -> str | None:
    """How ``value`` fails a scalar declaration (the ``got`` part of a defect), else None."""
    got = actual_type(value)
    if type_ is PropertyType.STRING:
        ok = isinstance(value, str)
    elif type_ is PropertyType.INT:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif type_ is PropertyType.FLOAT:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        if ok and not math.isfinite(value):
            return f"{got} (non-finite)"
    elif type_ is PropertyType.BOOL:
        ok = isinstance(value, bool)
    elif type_ is PropertyType.DATETIME:
        ok = isinstance(value, str)
        if ok and not _iso(value, datetime.fromisoformat):
            return f"{got} (not an ISO-8601 datetime)"
    elif type_ is PropertyType.DATE:
        ok = isinstance(value, str)
        if ok and not _iso(value, date.fromisoformat):
            return f"{got} (not an ISO-8601 date)"
    elif type_ is PropertyType.DURATION:
        ok = isinstance(value, str)
        if ok and _ISO_DURATION.fullmatch(value) is None:
            return f"{got} (not an ISO-8601 duration)"
    elif type_ is PropertyType.ENUM:
        ok = isinstance(value, str)
        if ok and values and value not in values:
            return f"{got} (not a declared value)"
    else:  # pragma: no cover - shapes are dispatched before scalars
        ok = False
    return None if ok else got


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


def _check_value(
    walk: _Walk,
    row: int,
    path: str,
    *,
    counted_as: str,
    decl: PropertySpec | ItemSpec,
    value: Any,
) -> None:
    """Check a non-null ``value`` against ``decl``; recurse into lists and maps."""
    items = getattr(decl, "items", None)
    declared = render_declared_type(decl.type, decl.values, items)
    if decl.type is PropertyType.LIST:
        if not isinstance(value, list):
            walk.defect(row, path, declared, actual_type(value))
            return
        if items is None:  # unreachable: PropertySpec.validate_shape requires items
            walk.defect(row, path, declared, "undeclared element type")
            return
        item_declared = render_declared_type(items.type, items.values, None)
        for index, element in enumerate(value):
            if element is None:
                walk.defect(row, f"{path}[{index}]", item_declared, "null")
            else:
                _check_value(walk, row, f"{path}[{index}]", counted_as=f"{counted_as}[]", decl=items, value=element)
        return
    if decl.type is PropertyType.MAP:
        if not isinstance(value, dict):
            walk.defect(row, path, declared, actual_type(value))
            return
        _check_map(walk, row, path, counted_as=counted_as, properties=decl.properties or [], value=value)
        return
    got = _scalar_mismatch(decl.type, decl.values, value)
    if got is not None:
        walk.defect(row, path, declared, got)


def _check_map(
    walk: _Walk, row: int, path: str, *, counted_as: str, properties: list[PropertySpec], value: dict[str, Any]
) -> None:
    declared = {prop.name: prop for prop in properties}
    for prop in properties:
        member = value.get(prop.name, _MISSING)
        member_path = f"{path}.{prop.name}"
        if member is _MISSING:
            if prop.required:
                walk.defect(row, member_path, f"{prop.declared_type} (required)", "missing")
        elif member is None:
            if prop.required or not prop.nullable:
                walk.defect(row, member_path, f"{prop.declared_type} (non-null)", "null")
        else:
            _check_value(walk, row, member_path, counted_as=f"{counted_as}.{prop.name}", decl=prop, value=member)
    for key in value:
        if key not in declared:
            walk.undeclared_key(row, f"{path}.{key}", counted_as=f"{counted_as}.{key}", value=value[key])


@dataclass(frozen=True)
class ChildProjection:
    """One childsync block: which item keys reach the child node, and whether it is static."""

    field: str
    targetid: str
    item: PropertySpec  # the list<map> declaration on the parent
    writable: frozenset[str]  # item ∩ child node declared, storable
    static: bool


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
        target = _node(domain_spec, endpoint.targetnode)
        props = _properties(target)
        children: list[ChildProjection] = []
        for child in endpoint.childsync or []:
            item = props[child.field]
            item_properties = item.items.properties if item.items is not None else None
            child_node = _node(domain_spec, child.targetnode)
            child_props = _properties(child_node)
            writable = frozenset(
                prop.name
                for prop in item_properties or []
                if prop.name in child_props and not prop.wire_only and not child_props[prop.name].wire_only
            )
            static = child_node is not None and child_node.managedby is ManagedByType.STATIC
            children.append(ChildProjection(child.field, child.targetid, item, writable, static))
        taxonomy_lists = frozenset(
            edge.field for edge in endpoint.taxonomyedges or [] if props[edge.field].type is PropertyType.LIST
        )
        return cls(
            path=endpoint.path,
            target_label=str(endpoint.targetnode),
            idproperty=str(endpoint.idproperty),
            properties=props,
            children=tuple(children),
            taxonomy_lists=taxonomy_lists,
        )

    @property
    def stored(self) -> frozenset[str]:
        """Node properties the generator writes: declared and storable."""
        return frozenset(name for name, prop in self.properties.items() if not prop.wire_only)

    def _walk(self, batch: list[Any], *, refuse_undeclared: bool) -> _Walk:
        walk = _Walk(refuse_undeclared=refuse_undeclared)
        id_decl = self.properties[self.idproperty]
        for index, row in enumerate(batch):
            if not isinstance(row, dict):
                walk.defects.append(f"row {index}: expected map, got {actual_type(row)}")
                continue
            if row.get(self.idproperty) is None:
                got = "missing" if self.idproperty not in row else "null"
                walk.defect(index, self.idproperty, f"{id_decl.declared_type} (required id)", got)
            for key, value in row.items():
                prop = self.properties.get(key)
                if prop is None:
                    walk.undeclared_key(index, key, counted_as=key, value=value)
                elif value is None:
                    if key != self.idproperty and not prop.nullable:
                        walk.defect(index, key, f"{prop.declared_type} (non-null)", "null")
                else:
                    _check_value(walk, index, key, counted_as=key, decl=prop, value=value)
        return walk

    def defects(self, batch: list[Any], *, refuse_undeclared: bool) -> list[str]:
        """Every defect of every row; empty when the whole batch conforms."""
        return self._walk(batch, refuse_undeclared=refuse_undeclared).defects

    def project(self, batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Rows in write shape, plus how often each undeclared key was dropped.

        ``node`` keeps only declared, storable properties present in the row
        (absent stays absent, so ``SET n += row.node`` leaves them unchanged;
        an explicit null still removes one). ``children`` keeps, per childsync
        field, each item's properties the child node declares.
        """
        dropped = self._walk(batch, refuse_undeclared=False).undeclared
        stored = self.stored
        rows: list[dict[str, Any]] = []
        for row in batch:
            node = {key: value for key, value in row.items() if key in stored}
            children = {
                child.field: [
                    {key: value for key, value in item.items() if key in child.writable}
                    for item in row.get(child.field) or []
                    if isinstance(item, dict)
                ]
                for child in self.children
            }
            rows.append({"node": node, "children": children})
        return rows, dict(sorted(dropped.items()))
