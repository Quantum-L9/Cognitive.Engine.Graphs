"""Observational shadow comparison outputs (TASK-055).

Does not replace primary match authority.
"""

from engine.shadow.compare import RankedCandidate, ShadowComparison, emit_shadow_comparison

__all__ = [
    "RankedCandidate",
    "ShadowComparison",
    "emit_shadow_comparison",
]
