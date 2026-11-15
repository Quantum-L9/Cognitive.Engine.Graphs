"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, health, feature-flags]
owner: engine-team
status: active
--- /L9_META ---

The health API is on (operator decision 2026-11-15) and CEG still dispatches no enrichment.

EIE's enrichment trigger reads readiness through Gate `health-assess`. The
contract agreed with EIE: the surface answers, and CEG never triggers
enrichment itself, so `auto_enrich_via_gate` stays off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.config.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _default(field: str) -> object:
    return Settings.model_fields[field].default


def test_health_api_is_on_by_default() -> None:
    assert _default("health_api_enabled") is True


def test_ceg_dispatches_no_enrichment_by_default() -> None:
    assert _default("auto_enrich_via_gate") is False


def test_production_compose_states_the_health_api_explicitly() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["api"]["environment"]
    assert environment["HEALTH_API_ENABLED"] == "true"
    assert "AUTO_ENRICH_VIA_GATE" not in environment
    assert "health-assess" in environment["L9_ALLOWED_ACTIONS"].split(",")
