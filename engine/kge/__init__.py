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

``ParkedSubsystemError`` is imported eagerly from ``engine.kge.parked``, which
imports no parked module. The other public names resolve lazily (PEP 562):
importing ``engine.kge`` loads no parked module. ``from engine.kge import X``
still works and loads only the module that defines ``X``.
"""

from __future__ import annotations

import importlib
from typing import Any

from engine.kge.parked import ParkedSubsystemError

_BEAM_SEARCH = "engine.kge.beam_search"
_COMPOUND_E3D = "engine.kge.compound_e3d"
_CROSS_DIMENSIONAL = "engine.kge.cross_dimensional_ensemble"
_ENSEMBLE = "engine.kge.ensemble"
_PARETO_ENSEMBLE = "engine.kge.pareto_ensemble"
_TRANSFORMATIONS = "engine.kge.transformations"

_EXPORTS: dict[str, str] = {
    "BeamSearchConfig": _BEAM_SEARCH,
    "BeamSearchEngine": _BEAM_SEARCH,
    "CascadeVariant": _BEAM_SEARCH,
    "CompoundE3D": _COMPOUND_E3D,
    "CompoundE3DConfig": _COMPOUND_E3D,
    "CrossDimensionalEnsemble": _CROSS_DIMENSIONAL,
    "CrossDimensionalResult": _CROSS_DIMENSIONAL,
    "DimensionalScore": _CROSS_DIMENSIONAL,
    "EnsembleController": _ENSEMBLE,
    "VariantScore": _ENSEMBLE,
    "ParetoEnsembleController": _PARETO_ENSEMBLE,
    "ParetoEnsembleResult": _PARETO_ENSEMBLE,
    "Flip": _TRANSFORMATIONS,
    "Hyperplane": _TRANSFORMATIONS,
    "Rotation": _TRANSFORMATIONS,
    "Scale": _TRANSFORMATIONS,
    "Shear": _TRANSFORMATIONS,
    "Transformation3D": _TRANSFORMATIONS,
    "Translation": _TRANSFORMATIONS,
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
