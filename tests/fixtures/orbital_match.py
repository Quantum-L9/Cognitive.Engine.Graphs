"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, fixtures, domain-neutrality, synthetic, match]
owner: engine-team
status: active
--- /L9_META ---

Integration-test overlays for the synthetic ``orbital-logistics`` pack (ADR-112 Stage 4).

The pack itself stays minimal because many unit suites read it. A Neo4j-backed
match test that needs one more construct (a traversal gate, an extra query
field) copies the loaded spec with that construct added — the same
``model_copy`` pattern tests/unit/test_match_gate_row_scope.py uses — and serves
the copy through ``serve``. Vocabulary stays the pack's: no product terms.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from engine.config.schema import DomainSpec, GateSpec, QueryFieldSpec

ORBITAL = "orbital-logistics"
DIRECTION = "window_to_depot"

# A depot must serve the requested orbit class: one SERVES_ORBIT edge to an
# OrbitClass whose class_code equals $orbit_class (EXISTS-style traversal gate).
SERVED_ORBIT_GATE: dict[str, Any] = {
    "name": "served_orbit_gate",
    "type": "traversal",
    "edgetype": "SERVES_ORBIT",
    "tonode": "OrbitClass",
    "candidateprop": "class_code",
    "queryparam": "orbit_class",
    "nullbehavior": "fail",
    "matchdirections": [DIRECTION],
}


def with_gates(spec: DomainSpec, *gates: dict[str, Any], fields: tuple[dict[str, Any], ...] = ()) -> DomainSpec:
    """A copy of ``spec`` with ``gates`` appended and ``fields`` added to its query schema."""
    queryschema = spec.queryschema.model_copy(
        update={"fields": [*spec.queryschema.fields, *(QueryFieldSpec(**f) for f in fields)]}
    )
    return spec.model_copy(
        update={"gates": [*spec.gates, *(GateSpec(**g) for g in gates)], "queryschema": queryschema}
    )


def orbit_gated(spec: DomainSpec) -> DomainSpec:
    """``spec`` plus SERVED_ORBIT_GATE and its ``orbit_class`` query field."""
    return with_gates(spec, SERVED_ORBIT_GATE, fields=({"name": "orbit_class", "type": "string"},))


def with_feedback_loop(spec: DomainSpec) -> DomainSpec:
    """``spec`` with the outcome feedback loop and signal-weight learning switched on.

    orbital-logistics declares its outcome vocabulary but leaves the loop off;
    readers and writers of DimensionWeight return early unless both are enabled.
    """
    loop = spec.feedbackloop
    signal_weights = loop.signal_weights.model_copy(update={"enabled": True, "min_outcomes_for_recalculation": 50})
    return spec.model_copy(
        update={"feedbackloop": loop.model_copy(update={"enabled": True, "signal_weights": signal_weights})}
    )


def serve(graph_driver: Any, spec: DomainSpec) -> None:
    """Wire the handler DI so every tenant resolves to ``spec``."""
    from engine.handlers import init_dependencies
    from engine.state import get_state

    loader = MagicMock()
    loader.load_domain.return_value = spec
    get_state().reset()
    init_dependencies(graph_driver, loader)


def depot(depot_id: str, **overrides: Any) -> dict[str, Any]:
    """An OrbitalDepot row that passes the pack's own gates (hazmat, ports)."""
    row: dict[str, Any] = {
        "depot_id": depot_id,
        "accepts_hazmat": True,
        "docking_ports": 4,
        "reliability": 0.9,
        "lat": 34.05,
        "lon": -118.24,
    }
    row.update(overrides)
    return row
