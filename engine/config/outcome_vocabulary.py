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


# ── preference_attention success value (scoring dimension metadata) ──────────

# Prefix of the admission defect for a preference_attention dimension that
# declares no metadata.success_value
# (settings.preference_attention_metadata_required_enabled).
SCORING_METADATA_DEFECT_PREFIX = "undeclared scoring metadata: "


def preference_attention_success_value_defect(name: str) -> str:
    """The refusal for a ``preference_attention`` dimension without ``metadata.success_value``.

    One text for the scoring compiler's ``ValueError`` and the admission defect,
    so admission quotes the compiler verbatim.
    """
    return (
        f"Dimension '{name}': preference_attention requires metadata.success_value "
        "(the outcome value the domain counts as success); the engine has no default"
    )


def undeclared_success_value_dimensions(spec: Any) -> list[str]:
    """Names of ``preference_attention`` dimensions that declare no ``metadata.success_value`` (flag on only).

    Which outcome value counts as success is the domain's declaration; the
    scoring compiler has no default and refuses the dimension on every match.
    With ``settings.preference_attention_metadata_required_enabled`` on the
    loader withholds such a pack at admission (:func:`scoring_metadata_defects`)
    instead; off, nothing is reported and the compiler's refusal stands.
    """
    from engine.config.schema import dimension_metadata
    from engine.config.settings import settings

    if not settings.preference_attention_metadata_required_enabled:
        return []
    scoring = getattr(spec, "scoring", None)
    names: list[str] = []
    for dim in getattr(scoring, "dimensions", None) or []:
        computation = getattr(dim, "computation", None)
        if str(getattr(computation, "value", computation)) != "preference_attention":
            continue
        if dimension_metadata(dim).get("success_value") is None:
            names.append(str(dim.name))
    return names


def scoring_metadata_defects(names: list[str]) -> list[str]:
    """The named admission defects for :func:`undeclared_success_value_dimensions`."""
    return [SCORING_METADATA_DEFECT_PREFIX + preference_attention_success_value_defect(name) for name in names]
