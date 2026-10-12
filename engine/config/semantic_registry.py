"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [semantics, registry]
owner: engine-team
status: active
--- /L9_META ---

Semantic registry: the entity labels a domain admits to entity resolution.

Domain neutrality (Stage 2, R10): the registry is built from the domain spec's
``semantic_registry.entity_labels`` (and falls back to the ontology's node
labels when the domain declares none). The former module-level
``CANONICAL_ENTITY_TYPES`` allowlist ("company", "facility", ...) was an
isolated legacy constant with no live caller and encoded one vertical's
vocabulary; it was never a runtime restriction and is gone. The live
restriction is ``DomainSpec.semantic_registry`` as read by ``handle_resolve``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.config.schema import DomainSpec


@dataclass(frozen=True)
class SemanticRegistry:
    """Immutable set of entity labels admitted to semantic resolution for one domain."""

    allowed_types: frozenset[str]

    def is_allowed(self, canonical_label: str) -> bool:
        return canonical_label in self.allowed_types

    @classmethod
    def from_spec(cls, spec: DomainSpec) -> SemanticRegistry:
        """Entity labels the domain admits: its declared registry labels, else its ontology."""
        declared = spec.semantic_registry.entity_labels
        labels = declared if declared else [node.label for node in spec.ontology.nodes]
        return cls(allowed_types=frozenset(labels))


__all__ = ["SemanticRegistry"]
