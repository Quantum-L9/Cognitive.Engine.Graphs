"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [explanations, harvest, deterministic]
owner: engine-team
status: active
--- /L9_META ---

Deterministic explanation helpers for harvested DomainSpec catalogs (TASK-018).
No tensor / einsum path.
"""

from __future__ import annotations

from typing import Any

from engine.config.schema import DomainSpec, ExplanationCatalogSpec


def _as_score(value: Any) -> float:
    """Convert contribution values without numeric coercion helpers."""

    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, int):
        return value + 0.0
    if isinstance(value, float):
        return value
    return 0.0


def order_feature_contributions(
    contributions: list[dict[str, Any]],
    catalog: ExplanationCatalogSpec,
) -> list[dict[str, Any]]:
    """Return contributions in a deterministic order."""

    if catalog.contribution_ordering == "score_desc":

        def key(item: dict[str, Any]) -> tuple[float, str]:
            return (-_as_score(item.get("contribution")), str(item.get("feature_id", "")))

        return sorted(contributions, key=key)
    return sorted(contributions, key=lambda item: str(item.get("feature_id", "")))


def recommended_actions_for(
    *,
    spec: DomainSpec,
    missing_evidence: list[str] | None = None,
    failed_gates: list[str] | None = None,
    eligible: bool = True,
    low_score: bool = False,
) -> list[str]:
    """Map match state to deterministic recommended action messages."""

    actions = spec.explanations.recommended_actions
    messages: list[str] = []
    if missing_evidence:
        messages.extend(a.message for a in actions if a.when == "missing_evidence")
    if failed_gates:
        messages.extend(a.message for a in actions if a.when == "failed_gate")
    if not eligible:
        messages.extend(a.message for a in actions if a.when == "ineligible")
    if low_score and eligible:
        messages.extend(a.message for a in actions if a.when == "low_score")
    seen: set[str] = set()
    ordered: list[str] = []
    for msg in messages:
        if msg not in seen:
            seen.add(msg)
            ordered.append(msg)
    return ordered


def assert_no_tensor_runtime() -> None:
    """Fail closed if a parallel tensor runtime package is present."""

    import importlib.util

    if importlib.util.find_spec("engine.tensor") is not None:
        raise RuntimeError("engine.tensor must not exist; tensor cartridge runtime is rejected")
