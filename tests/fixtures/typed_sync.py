"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, fixtures, sync, contract, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

Shared builders for the typed sync contract tests: the synthetic
``orbital-logistics`` pack (vocabulary no shipped pack shares) and PlasticOS,
loaded from disk and mutated per test as raw YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine.config.schema import DomainSpec, SyncEndpointSpec
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPOTS = "/v1/sync/orbital-depots"


def orbital_raw() -> dict[str, Any]:
    return yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())


def plasticos() -> DomainSpec:
    return DomainSpec.model_validate(yaml.safe_load((DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text()))


def spec(raw: dict[str, Any] | None = None) -> DomainSpec:
    return DomainSpec.model_validate(raw if raw is not None else orbital_raw())


def endpoint(domain_spec: DomainSpec, path: str) -> SyncEndpointSpec:
    assert domain_spec.sync is not None
    return next(item for item in domain_spec.sync.endpoints if item.path == path)


def node_props(raw: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return next(node for node in raw["ontology"]["nodes"] if node["label"] == label)["properties"]


def depot_props(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {prop["name"]: prop for prop in node_props(raw, "OrbitalDepot")}


def drop_depot_prop(raw: dict[str, Any], name: str) -> None:
    props = node_props(raw, "OrbitalDepot")
    props.remove(next(prop for prop in props if prop["name"] == name))


def depot_endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in raw["sync"]["endpoints"] if item["path"] == DEPOTS)
