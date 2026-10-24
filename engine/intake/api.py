"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [intake, api]
tags: [intake, chassis, handler]
owner: engine-team
status: active
--- /L9_META ---

Chassis action handlers for the intake module.

Registers intake_scan, intake_compile, and intake_report actions
with the engine handler pattern (tenant, payload) → dict.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec
from engine.intake.crm_field_scanner import scan_crm_fields
from engine.intake.impact_reporter import analyse_impact, format_impact_summary
from engine.intake.intake_compiler import compile_intake
from engine.intake.intake_schema import FieldMapping

logger = logging.getLogger(__name__)

_domain_loader: DomainPackLoader | None = None


def init_intake(domain_loader: DomainPackLoader) -> None:
    """Inject dependencies at startup."""
    global _domain_loader
    _domain_loader = domain_loader


def _require_loader() -> DomainPackLoader:
    if _domain_loader is None:
        raise RuntimeError("Intake dependencies not initialized. Call init_intake() first.")
    return _domain_loader


def _require_key(payload: dict[str, Any], key: str, action: str) -> Any:
    """Extract required key with structured error."""
    if key not in payload:
        raise ValueError(f"Missing required field '{key}' in {action} payload")
    return payload[key]


def _optional_str_map(payload: dict[str, Any], key: str, action: str) -> dict[str, str] | None:
    """Optional ``{str: str}`` table, validated before it reaches the compiler."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"Field '{key}' in {action} payload must be an object of string → string")
    return value


def _optional_str_list_map(payload: dict[str, Any], key: str, action: str) -> dict[str, list[str]] | None:
    """Optional ``{str: [str, ...]}`` table, validated before it reaches the compiler."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(item, str) for item in v)
        for k, v in value.items()
    ):
        raise ValueError(f"Field '{key}' in {action} payload must be an object of string → list of strings")
    return value


async def handle_intake_scan(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Scan CRM fields against graph spec ontology.

    Payload:
      - crm_fields: dict[str, str] | list[str]  — CRM field export
      - domain: str (optional)                   — domain ID override

    Returns:
      ScanResult as dict.
    """
    loader = _require_loader()
    crm_fields = _require_key(payload, "crm_fields", "intake_scan")
    domain_id = payload.get("domain", tenant)

    spec: DomainSpec = loader.load_domain(domain_id)
    result = scan_crm_fields(crm_fields, spec)

    logger.info("intake_scan complete: %d matched, %d unmatched", len(result.matched), len(result.unmatched))
    return result.model_dump(mode="json")


async def handle_intake_compile(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Compile CRM fields + business profile into domain_spec.yaml.

    Payload:
      - crm_fields: dict[str, str] | list[str]  — CRM field export
      - business_profile: str                    — company description
      - domain: str (optional)                   — domain ID override
      - vertical_keywords: dict[str, str] (optional)      — keyword → domain id, caller-supplied example data
      - derivation_graph: dict[str, list[str]] (optional) — target → source fields, caller-supplied
      - ambiguity_patterns: dict[str, list[str]] (optional) — ambiguous name → candidates, caller-supplied

    The engine ships none of the three tables (Stage 2 R06): the sales
    demonstration passes its own example data explicitly.

    Returns:
      IntakeResult as dict.
    """
    loader = _require_loader()
    crm_fields = _require_key(payload, "crm_fields", "intake_compile")
    business_profile = _require_key(payload, "business_profile", "intake_compile")
    domain_id = payload.get("domain", tenant)

    spec: DomainSpec = loader.load_domain(domain_id)
    result = compile_intake(
        crm_fields,
        business_profile,
        spec,
        vertical_keywords=_optional_str_map(payload, "vertical_keywords", "intake_compile"),
        derivation_graph=_optional_str_list_map(payload, "derivation_graph", "intake_compile"),
        ambiguity_patterns=_optional_str_list_map(payload, "ambiguity_patterns", "intake_compile"),
    )

    logger.info("intake_compile complete: tier=%s", result.delivery_tier)
    return result.model_dump(mode="json")


async def handle_intake_report(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Generate impact report for an entity.

    Payload:
      - entity_id: str    — entity to analyse
      - domain: str        — domain ID
      - crm_fields: dict[str, str] | list[str] (optional) — for ad-hoc analysis

    Returns:
      ImpactAnalysis as dict + formatted summary.
    """
    loader = _require_loader()
    domain_id = _require_key(payload, "domain", "intake_report")
    spec: DomainSpec = loader.load_domain(domain_id)

    # If crm_fields provided, scan and analyse
    crm_fields = payload.get("crm_fields")
    if crm_fields is not None:
        scan = scan_crm_fields(crm_fields, spec)
        mappings: list[FieldMapping] = list(scan.matched)
    else:
        # No CRM fields → analyse with empty set (shows full gap)
        mappings = []

    impact = analyse_impact(mappings, spec)
    summary = format_impact_summary(impact)

    logger.info("intake_report complete: tier=%s", impact.tier_recommendation)
    return {
        **impact.model_dump(mode="json"),
        "summary": summary,
    }


def register_intake_handlers(handler_registry: Any) -> None:
    """Register intake actions with the chassis handler registry."""
    handler_registry.register_handler("intake_scan", handle_intake_scan)
    handler_registry.register_handler("intake_compile", handle_intake_compile)
    handler_registry.register_handler("intake_report", handle_intake_report)
    logger.info("Registered 3 intake action handlers: intake_scan, intake_compile, intake_report")
