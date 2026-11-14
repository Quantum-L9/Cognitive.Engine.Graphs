"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [kge]
tags: [kge, embeddings]
owner: engine-team
status: active
--- /L9_META ---

Knowledge Graph Embedding (KGE) — Phase 4 module.

PARKED (CEG-KGE-PARK, operator decision 2026-11-14): CompoundE3D, KGE beam
search, the KGE Pareto ensemble and the cross-dimensional ensemble are parked.
Their constructors raise ``engine.kge.parked.ParkedSubsystemError``. Code kept,
not deleted — see docs/PARKED_SUBSYSTEMS.md. ``EnsembleController`` and the 3D
transformations are generic and stay unparked.

The public names below resolve lazily (PEP 562): importing ``engine.kge`` or
``engine.kge.parked`` loads no parked module. ``from engine.kge import X``
still works and loads only the module that defines ``X``.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "BeamSearchConfig": "engine.kge.beam_search",
    "BeamSearchEngine": "engine.kge.beam_search",
    "CascadeVariant": "engine.kge.beam_search",
    "CompoundE3D": "engine.kge.compound_e3d",
    "CompoundE3DConfig": "engine.kge.compound_e3d",
    "CrossDimensionalEnsemble": "engine.kge.cross_dimensional_ensemble",
    "CrossDimensionalResult": "engine.kge.cross_dimensional_ensemble",
    "DimensionalScore": "engine.kge.cross_dimensional_ensemble",
    "EnsembleController": "engine.kge.ensemble",
    "VariantScore": "engine.kge.ensemble",
    "ParetoEnsembleController": "engine.kge.pareto_ensemble",
    "ParetoEnsembleResult": "engine.kge.pareto_ensemble",
    "Flip": "engine.kge.transformations",
    "Hyperplane": "engine.kge.transformations",
    "Rotation": "engine.kge.transformations",
    "Scale": "engine.kge.transformations",
    "Shear": "engine.kge.transformations",
    "Transformation3D": "engine.kge.transformations",
    "Translation": "engine.kge.transformations",
    "ParkedSubsystemError": "engine.kge.parked",
}

__all__ = [
    "BeamSearchConfig",
    "BeamSearchEngine",
    "CascadeVariant",
    "CompoundE3D",
    "CompoundE3DConfig",
    "CrossDimensionalEnsemble",
    "CrossDimensionalResult",
    "DimensionalScore",
    "EnsembleController",
    "Flip",
    "Hyperplane",
    "ParetoEnsembleController",
    "ParetoEnsembleResult",
    "ParkedSubsystemError",
    "Rotation",
    "Scale",
    "Shear",
    "Transformation3D",
    "Translation",
    "VariantScore",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module 'engine.kge' has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
