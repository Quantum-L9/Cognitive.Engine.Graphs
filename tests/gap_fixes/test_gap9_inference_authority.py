"""Regression coverage for removed inference-bridge and raw-KB authority."""

from __future__ import annotations

from pathlib import Path

import engine.inference_rule_registry as registry
from engine.inference_rule_registry import InferenceContext, InferenceResult, execute_rule

ROOT = Path(__file__).resolve().parents[2]


def test_removed_inference_bridge_has_no_compatibility_module() -> None:
    assert not (ROOT / "engine" / "inference_bridge.py").exists()


def test_startup_recipe_does_not_reintroduce_undeclared_kb_loading() -> None:
    source = (ROOT / "engine" / "startup_wiring.py").read_text(encoding="utf-8")
    assert "spec.kb" not in source
    assert "load_domain_rules" not in source


def test_registry_exposes_only_supported_in_code_rule_surface() -> None:
    assert not hasattr(registry, "load_domain_rules")
    assert not hasattr(registry, "NaryFact")
    assert not hasattr(registry, "to_rule_engine_format")


def test_supported_registry_still_returns_canonical_result_type() -> None:
    context = InferenceContext(tenant_id="test", domain_id="plasticos", pass_number=1)
    result = execute_rule(
        "infer_material_grade_from_mfi",
        {"melt_flow_index": 5.0, "material_type": "HDPE"},
        context,
    )
    assert isinstance(result, InferenceResult)
    assert result.field_name == "material_grade"
    assert result.value == "HD_injection"


def test_runtime_surface_contains_no_ghost_successor_reference() -> None:
    ghost_module = "inference_bridge" + "_v2"
    ghost_doc = "docs/migration/" + ghost_module + ".md"
    offenders: list[str] = []

    for path in sorted((ROOT / "engine").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if ghost_module in source or ghost_doc in source:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
