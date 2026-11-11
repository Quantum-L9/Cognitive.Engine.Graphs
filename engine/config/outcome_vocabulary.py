"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, outcomes, feedback, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

The domain's outcome vocabulary (``feedbackloop.outcome_node_label``).

CEG domain-neutrality Stage 2 follow-up (owner decision on item 5, "require
it, packs declare"): the outcome node label is the domain's declaration. The
schema no longer borrows one pack's label as a default, so a pack that writes
or reads outcomes declares it, and an outcome write or read on a pack that
declares none fails with :class:`OutcomeNodeLabelUndeclaredError` naming the
domain and the operation.

``settings.outcome_vocabulary_required_enabled`` (default on) is the C-21
rollback: off, an undeclared label resolves to the label the schema used to
default to, so existing graphs read and write exactly as before.

The outcome *edge* (``feedbackloop.outcome_edge_type``) is out of scope here;
its split stays deferred with A09.
"""

from __future__ import annotations

from typing import Any

# The label the schema defaulted to before the follow-up. Read only when
# outcome_vocabulary_required_enabled is off (C-21 rollback).
_LEGACY_OUTCOME_NODE_LABEL = "TransactionOutcome"


class OutcomeNodeLabelUndeclaredError(ValueError):
    """An outcome write or read on a domain that declares no ``feedbackloop.outcome_node_label``."""

    def __init__(self, domain_id: str, operation: str) -> None:
        self.domain_id = domain_id
        self.operation = operation
        super().__init__(
            f"Domain {domain_id!r} declares no feedbackloop.outcome_node_label, which {operation} requires; "
            "declare the outcome node label in the domain spec (feedbackloop.outcome_node_label)"
        )


def declared_outcome_node_label(domain_spec: Any) -> str | None:
    """The domain's outcome node label, or ``None`` when it declares none.

    With ``outcome_vocabulary_required_enabled`` off an undeclared label reads
    as the legacy default. Not sanitized; for comparisons only.
    """
    from engine.config.settings import settings

    feedbackloop = getattr(domain_spec, "feedbackloop", None)
    declared = getattr(feedbackloop, "outcome_node_label", None) if feedbackloop is not None else None
    if declared:
        return str(declared)
    if not settings.outcome_vocabulary_required_enabled:
        return _LEGACY_OUTCOME_NODE_LABEL
    return None


def outcome_node_label(domain_spec: Any, operation: str) -> str:
    """The outcome node label ``operation`` writes or reads.

    Raises :class:`OutcomeNodeLabelUndeclaredError` when the domain declares
    none (and the requirement is on). Callers pass the result through
    ``sanitize_label`` at the interpolation site (C-009).
    """
    label = declared_outcome_node_label(domain_spec)
    if label is None:
        domain = getattr(domain_spec, "domain", None)
        raise OutcomeNodeLabelUndeclaredError(str(getattr(domain, "id", "?")), operation)
    return label
