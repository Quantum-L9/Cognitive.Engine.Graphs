"""Regression coverage for removed inference-bridge and raw-KB authority."""

from __future__ import annotations

from pathlib import Path

from engine import inference_rule_registry as registry

ROOT = Path(__file__).resolve().parents[2]


def test_removed_inference_bridge_has_no_compatibility_module() -> None:
    assert not (ROOT / "engine" / "inference_bridge.py").exists()


def test_no_module_reintroduces_undeclared_kb_loading() -> None:
    """The recipe file this guard used to read has since been removed as an
    unrunnable gap-fix artifact, so scan the whole engine tree instead. Broader
    than the original assertion, and no longer tied to one file's existence.
    """
    banned = ("spec.kb", "load_domain_rules")
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "engine").rglob("*.py"))
        if any(token in path.read_text(encoding="utf-8") for token in banned)
    ]
    assert offenders == []


def test_startup_recipe_module_stays_removed() -> None:
    """engine/startup_wiring.py instructed operators to wire five artifacts that
    no longer exist, and could never run (its first import named a package that
    is absent). It must not come back.
    """
    assert not (ROOT / "engine" / "startup_wiring.py").exists()


def test_registry_exposes_only_supported_in_code_rule_surface() -> None:
    assert not hasattr(registry, "load_domain_rules")
    assert not hasattr(registry, "NaryFact")
    assert not hasattr(registry, "to_rule_engine_format")


def test_supported_registry_still_returns_canonical_result_type() -> None:
    context = registry.InferenceContext(tenant_id="test", domain_id="plasticos", pass_number=1)
    result = registry.execute_rule(
        "infer_material_grade_from_mfi",
        {"melt_flow_index": 5.0, "material_type": "HDPE"},
        context,
    )
    assert isinstance(result, registry.InferenceResult)
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
