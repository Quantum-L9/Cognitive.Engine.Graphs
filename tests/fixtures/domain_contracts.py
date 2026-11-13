"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, fixtures, domain-neutrality, payload-contracts]
owner: engine-team
status: active
--- /L9_META ---

Shared verification inputs for the domain-neutrality acceptance tests.

Two kinds of thing live here, deliberately apart:

* ``PLASTICOS_EXAMPLE_CONTRACT`` — the admission contract the repository's
  PlasticOS *example* fixtures (``contracts/payloads/examples``,
  ``contracts/match_request.json``) are written against. It is hand-declared so
  the payload-model tests do not depend on loading the PlasticOS pack; it
  mirrors that pack's declared vocabulary and is an example value, not engine
  knowledge.
* ``SYNTHETIC_DOMAINS_ROOT`` and ``synthetic_contract`` — two unrelated
  synthetic domain packs under ``tests/fixtures/domains`` with unfamiliar node
  types, relationship names, directions, outcome signals and source systems.
  They prove the engine executes a domain's declared contract rather than a
  built-in recipe. Verification inputs only; never canonical domain products.

``DESIGN_PACKS_ROOT`` is the repository's design and test packs: the
top-level ``domains/`` directory until the relocation in issue #400 A5/D5
moves them under ``tests/fixtures/design_packs``. They are product-shaped
examples read by tests and tools, never loaded in production, and they stay
out of ``SYNTHETIC_DOMAINS_ROOT`` so that root remains domain-neutral
(ADR-112).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.config.loader import DomainPackLoader
from engine.models.payloads import DomainContract

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_DOMAINS_ROOT = REPO_ROOT / "tests" / "fixtures" / "domains"
DESIGN_PACKS_ROOT = REPO_ROOT / "domains"
PAYLOAD_EXAMPLES = REPO_ROOT / "contracts" / "payloads" / "examples"

PLASTICOS_EXAMPLE_CONTRACT = DomainContract(
    domain_id="plasticos",
    domain_version="1.1.0",
    match_directions=frozenset({"supply_opportunity_to_buyer_facility", "buyer_demand_to_supply_opportunity"}),
    entity_types=frozenset(
        {"Facility", "MaterialIntake", "PolymerFamily", "MaterialForm", "SupplyOpportunity", "BuyerDemand"}
    ),
    source_systems=frozenset({"ceg", "operator", "external_source", "eie", "odoo"}),
    outcome_types=frozenset(
        {
            "reviewed",
            "contacted",
            "quoted",
            "accepted",
            "rejected",
            "completed",
            "claim_raised",
            "paid",
            "repeat_business",
        }
    ),
)

# Vocabulary of the two synthetic packs, kept in one place so substitution-based
# fixture generation and direct assertions agree.
SYNTHETIC_VOCABULARY: dict[str, dict[str, str]] = {
    "orbital-logistics": {
        "direction": "window_to_depot",
        "entity_type": "OrbitalDepot",
        "outcome_type": "docked",
        "source_system": "station_ledger",
    },
    "cheese-aging": {
        "direction": "batch_to_cave",
        "entity_type": "AgingCave",
        "outcome_type": "tasted",
        "source_system": "creamery_books",
    },
}


def synthetic_loader() -> DomainPackLoader:
    return DomainPackLoader(config_path=str(SYNTHETIC_DOMAINS_ROOT))


def synthetic_contract(domain_id: str) -> DomainContract:
    """Admission contract derived from a synthetic pack through the real loader."""
    return DomainContract.from_spec(synthetic_loader().load_domain(domain_id))


def _rewrite(value: Any, replacements: dict[str, str]) -> Any:
    """Recursively substitute vocabulary strings in a JSON-like structure."""
    if isinstance(value, dict):
        return {k: _rewrite(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, replacements) for v in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def example_payload_for(domain_id: str, example_name: str) -> dict[str, Any]:
    """A repository example fixture re-expressed in a synthetic domain's vocabulary.

    Only the contract-resolved fields change (domain, direction, entity type,
    outcome type, source system); everything else stays the example's shape.
    """
    vocab = SYNTHETIC_VOCABULARY[domain_id]
    replacements = {
        "plasticos": domain_id,
        "supply_opportunity_to_buyer_facility": vocab["direction"],
        "buyer_demand_to_supply_opportunity": vocab["direction"],
        "SupplyOpportunity": vocab["entity_type"],
        "Facility": vocab["entity_type"],
        "accepted": vocab["outcome_type"],
        "odoo": vocab["source_system"],
    }
    data = json.loads((PAYLOAD_EXAMPLES / example_name).read_text(encoding="utf-8"))
    return _rewrite(data, replacements)


def write_synthetic_fixture_set(domain_id: str, target_root: Path) -> Path:
    """Materialize a complete examples/ + negative_examples/ set for a synthetic domain.

    Used to drive the payload contract compiler against a domains root that
    holds no PlasticOS directory at all.
    """
    examples = target_root / "examples"
    negatives = target_root / "negative_examples"
    examples.mkdir(parents=True, exist_ok=True)
    negatives.mkdir(parents=True, exist_ok=True)
    for path in sorted(PAYLOAD_EXAMPLES.glob("*.json")):
        (examples / path.name).write_text(json.dumps(example_payload_for(domain_id, path.name), indent=2))
    repo_negatives = PAYLOAD_EXAMPLES.parent / "negative_examples"
    for path in sorted(repo_negatives.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rewritten = _rewrite_negative(data, domain_id)
        (negatives / path.name).write_text(json.dumps(rewritten, indent=2))
    return target_root


def _rewrite_negative(data: dict[str, Any], domain_id: str) -> dict[str, Any]:
    """Re-express a negative fixture so only its intended defect remains."""
    vocab = SYNTHETIC_VOCABULARY[domain_id]
    replacements = {
        "plasticos": domain_id,
        "supply_opportunity_to_buyer_facility": vocab["direction"],
        "buyer_demand_to_supply_opportunity": vocab["direction"],
        "SupplyOpportunity": vocab["entity_type"],
        "Facility": vocab["entity_type"],
        "odoo": vocab["source_system"],
    }
    return _rewrite(data, replacements)
