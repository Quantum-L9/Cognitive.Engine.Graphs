"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [kge, hoprag]
tags: [parked, kge, hoprag, compound-e3d, feature-flags]
owner: engine-team
status: active
--- /L9_META ---

Parking guard for HopRAG and CompoundE3D (CEG-KGE-PARK).

Operator decision 2026-11-14: HopRAG and CompoundE3D are PARKED. Their code
stays in the repository (``engine/hoprag/``, ``engine/kge/``) but no runtime
path may construct, train or run them. Every entry point calls
:func:`require_unparked`, which raises :class:`ParkedSubsystemError` — an
explicit, typed refusal naming the decision — instead of a silent no-op or a
fake success.

This module imports nothing from the parked modules, so a runtime caller
(``engine/handlers.py``) can consult it without loading parked code. The one
override is ``settings.parked_subsystems_enabled`` (default ``False``); it
exists so the engine can prove the parked mechanisms in tests and so a future
operator decision can un-park without a code change. See
``docs/PARKED_SUBSYSTEMS.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from engine.config.schema import KGESpec
    from engine.kge.compound_e3d import CompoundE3D

PARKED_DECISION: Final = "CEG-KGE-PARK"
PARKED_DECIDED: Final = "2026-11-14"
PARKED_DOC: Final = "docs/PARKED_SUBSYSTEMS.md"
PARKED_FLAG: Final = "PARKED_SUBSYSTEMS_ENABLED"

HOPRAG: Final = "HopRAG"
COMPOUND_E3D: Final = "CompoundE3D"

# Every subsystem the decision parks, by the name an entry point reports.
PARKED_SUBSYSTEMS: Final = frozenset(
    {
        HOPRAG,
        COMPOUND_E3D,
        "KGE beam search",
        "KGE Pareto ensemble",
        "KGE cross-dimensional ensemble",
    }
)

# ``KGESpec.model`` values whose implementation is parked.
PARKED_KGE_MODELS: Final = frozenset({COMPOUND_E3D})


class ParkedSubsystemError(RuntimeError):
    """A parked subsystem was asked to construct, train or run.

    Carries the decision, its date and the document that records it, so a
    caller can surface the refusal without re-deriving why.
    """

    def __init__(self, subsystem: str) -> None:
        self.subsystem = subsystem
        self.decision = PARKED_DECISION
        self.decided = PARKED_DECIDED
        self.doc = PARKED_DOC
        self.flag = PARKED_FLAG
        msg = (
            f"{subsystem} is parked by operator decision {PARKED_DECISION} "
            f"({PARKED_DECIDED}); it cannot be constructed, trained or run. "
            f"See {PARKED_DOC}."
        )
        super().__init__(msg)

    def to_context(self) -> dict[str, Any]:
        """Machine-readable refusal detail for an error response."""
        return {
            "subsystem": self.subsystem,
            "parked": True,
            "decision": self.decision,
            "decided": self.decided,
            "doc": self.doc,
            "flag": self.flag,
        }


def parked_subsystems_enabled() -> bool:
    """Whether the operator override un-parks the parked subsystems.

    Reads ``engine.config.settings.settings`` at call time so a test (or a
    reloaded settings object) is honoured. Only a literal ``True`` un-parks:
    a missing, unset or non-boolean value keeps the subsystems parked.
    """
    from engine.config import settings as settings_module

    return getattr(settings_module.settings, "parked_subsystems_enabled", False) is True


def is_parked_model(model: str) -> bool:
    """Whether ``KGESpec.model`` names a parked model under current settings."""
    return model in PARKED_KGE_MODELS and not parked_subsystems_enabled()


def require_unparked(subsystem: str) -> None:
    """Refuse with :class:`ParkedSubsystemError` while ``subsystem`` is parked."""
    if subsystem not in PARKED_SUBSYSTEMS:
        msg = f"{subsystem!r} is not a parked subsystem"
        raise ValueError(msg)
    if not parked_subsystems_enabled():
        raise ParkedSubsystemError(subsystem)


def construct_compound_e3d(spec: KGESpec) -> CompoundE3D:
    """The one gateway a runtime caller may use to build a CompoundE3D model.

    Refuses before the parked module is even imported, so a caller that only
    imports ``engine.kge.parked`` never loads CompoundE3D while it is parked.
    """
    require_unparked(COMPOUND_E3D)
    from engine.kge.compound_e3d import CompoundE3D, CompoundE3DConfig

    return CompoundE3D(CompoundE3DConfig.from_kge_spec(spec))
