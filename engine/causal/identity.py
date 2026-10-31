"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [causal]
tags: [causal, identity]
owner: engine-team
status: active
--- /L9_META ---

Node identity resolution for causal Cypher (RU-068 / CEG-COR-057, PO-04).

The causal subsystem matched every node by a hardcoded ``entity_id`` property,
a PlasticOS convention. This module resolves the identity property a domain
declares for a label, using the same rule as ``handle_match`` (Stage 2 P4,
``spec_declared_candidate_id``): the sync endpoint's ``idproperty`` for the
label, else the node's first ``required`` property, else ``entity_id``. The
domain's outcome label (``feedbackloop.outcome_node_label``) is identified by
``outcome_id``, the key ``handle_outcomes`` writes. Returned names are
sanitized and safe to interpolate as Cypher property keys (C-9).
"""

from __future__ import annotations

from typing import Any

from engine.config.schema import DomainSpec
from engine.utils.security import sanitize_label

FALLBACK_ID_PROPERTY = "entity_id"
OUTCOME_ID_PROPERTY = "outcome_id"


def declared_id_property(domain_spec: DomainSpec, label: str) -> str:
    """The identity property the domain declares for ``label`` (sanitized)."""
    from engine.config.settings import settings

    if not settings.spec_declared_candidate_id:
        return FALLBACK_ID_PROPERTY
    sync = getattr(domain_spec, "sync", None)
    if sync:
        for endpoint in sync.endpoints:
            if endpoint.targetnode == label and endpoint.idproperty:
                return sanitize_label(endpoint.idproperty)
    for node in domain_spec.ontology.nodes:
        if node.label == label:
            for prop in node.properties:
                if prop.required:
                    return sanitize_label(prop.name)
    return FALLBACK_ID_PROPERTY


def node_id_property(domain_spec: DomainSpec, label: str) -> str:
    """``outcome_id`` for the domain's outcome label, else the declared id property."""
    feedbackloop: Any = getattr(domain_spec, "feedbackloop", None)
    if feedbackloop is not None and label == feedbackloop.outcome_node_label:
        return OUTCOME_ID_PROPERTY
    return declared_id_property(domain_spec, label)
