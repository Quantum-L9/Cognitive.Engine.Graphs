"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, schema, sync, relationship, evidence, identity, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

Tenant-configured provider association mapping (QM.POC0.TENANT_ASSOCIATION_MAPPING.004).

The tenant domain spec is the single authority for the two declarations that let
a provider observation become graph evidence with a scoreable identity:

- ``sync.endpoints[].observedfrom`` on a relationship endpoint names the provider
  associations that endpoint accepts as observations. The relationship type,
  direction and endpoint labels come from the endpoint itself (D-2, D-10, D-11).
- ``sync.endpoints[].refnamespace`` on a node endpoint is the one place a provider
  record id is mapped to a graph ref (``<namespace>:<record id>``). Node sync,
  relationship sync and scoring resolve the same identity from it (D-13).

``validate_tenant_mapping`` runs from ``DomainSpec.validate_cross_references`` on
every load, unconditionally (D-7): a spec carrying a malformed, ambiguous or
conflicting declaration never loads, so it fails before any source read or graph
write. Specs that declare neither key are unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.config.schema import DomainSpec, SyncEndpointSpec


def _endpoints(domain_spec: DomainSpec) -> list[SyncEndpointSpec]:
    return list(domain_spec.sync.endpoints) if domain_spec.sync else []


def declared_ref_namespace(domain_spec: DomainSpec, label: str) -> str | None:
    """The graph-ref namespace the tenant declares for ``label``'s node ids, else None.

    ``validate_tenant_mapping`` guarantees every node endpoint of a label agrees,
    so the first declaration found is the only one.
    """
    for endpoint in _endpoints(domain_spec):
        if endpoint.targetnode == label and endpoint.refnamespace:
            return endpoint.refnamespace
    return None


def _validate_ref_namespaces(domain_spec: DomainSpec) -> None:
    by_label: dict[str, set[str | None]] = {}
    for endpoint in _endpoints(domain_spec):
        if endpoint.refnamespace is not None and (endpoint.targetedge or not endpoint.targetnode):
            msg = f"Endpoint '{endpoint.path}': refnamespace is declared on node endpoints (targetnode) only"
            raise ValueError(msg)
        if endpoint.targetnode:
            by_label.setdefault(endpoint.targetnode, set()).add(endpoint.refnamespace)

    owners: dict[str, str] = {}
    for label, namespaces in sorted(by_label.items()):
        declared = {ns for ns in namespaces if ns is not None}
        if not declared:
            continue
        if len(namespaces) > 1:
            listed = sorted(ns or "<none>" for ns in namespaces)
            msg = (
                f"Node {label!r}: every sync endpoint of a namespaced label declares the same "
                f"refnamespace, found {listed}"
            )
            raise ValueError(msg)
        namespace = declared.pop()
        if namespace in owners:
            msg = f"refnamespace {namespace!r} is declared for both {owners[namespace]!r} and {label!r}"
            raise ValueError(msg)
        owners[namespace] = label

    # A namespaced label is written only by its own node endpoints; a child or
    # taxonomy write would create ids outside the namespace.
    for endpoint in _endpoints(domain_spec):
        indirect = [child.targetnode for child in endpoint.childsync or []] + [
            edge.targetlabel for edge in endpoint.taxonomyedges or []
        ]
        for label in indirect:
            if label in owners.values():
                msg = (
                    f"Endpoint '{endpoint.path}': node {label!r} has a refnamespace and is written only "
                    "by its own node sync endpoint, not by childsync or taxonomyedges"
                )
                raise ValueError(msg)


def _validate_observations(domain_spec: DomainSpec) -> None:
    # Lazy: the generator imports the schema module (the validator pattern in schema.py).
    from engine.sync.generator import resolve_relationship_declaration

    owners: dict[tuple[str, str, str, str, int], str] = {}
    for endpoint in _endpoints(domain_spec):
        if endpoint.observedfrom is None:
            continue
        if not endpoint.observedfrom:
            msg = f"Endpoint '{endpoint.path}': observedfrom is a non-empty list; omit it to accept no observation"
            raise ValueError(msg)
        resolve_relationship_declaration(domain_spec, endpoint)
        for label in (endpoint.fromnode, endpoint.tonode):
            if label and declared_ref_namespace(domain_spec, label) is None:
                msg = (
                    f"Endpoint '{endpoint.path}': observed node {label!r} declares no refnamespace on its "
                    "node sync endpoint, so a provider record id has no graph identity"
                )
                raise ValueError(msg)
        for source in endpoint.observedfrom:
            key = source.key
            if key in owners:
                msg = (
                    f"Endpoint '{endpoint.path}': provider association {key} is already mapped by "
                    f"'{owners[key]}'; one association maps to at most one relationship"
                )
                raise ValueError(msg)
            owners[key] = endpoint.path


def validate_tenant_mapping(domain_spec: DomainSpec) -> None:
    """Refuse a malformed, ambiguous or conflicting tenant mapping declaration (raises ValueError)."""
    _validate_ref_namespaces(domain_spec)
    _validate_observations(domain_spec)
