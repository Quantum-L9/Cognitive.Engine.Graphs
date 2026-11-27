"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, pydantic, domain-spec]
owner: engine-team
status: active
--- /L9_META ---

Pydantic schema models for domain pack validation.
Defines the contract every spec.yaml must satisfy.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class ManagedByType(StrEnum):
    """Entity lifecycle management source."""

    SYNC = "sync"
    GDS = "gds"
    STATIC = "static"
    API = "api"
    COMPUTED = "computed"
    CUSTOMAGGREGATION = "customaggregation"


class PropertyType(StrEnum):
    """Supported property data types.

    ``list`` and ``map`` are shapes, not scalars: a ``list`` declares its element
    type in ``items`` and a ``map`` declares its keys in ``properties``
    (``PropertySpec``). Neo4j stores primitives and arrays of primitives only, so
    ``list<primitive>`` is storable while ``map`` and ``list<map>`` are wire-only
    shapes a sync endpoint consumes (``childsync``) and never stores.
    """

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DATETIME = "datetime"
    DATE = "date"
    DURATION = "duration"
    ENUM = "enum"
    LIST = "list"
    MAP = "map"


# Shapes that declare structure rather than a scalar value.
SHAPE_TYPES = frozenset({PropertyType.LIST, PropertyType.MAP})


class EdgeDirection(StrEnum):
    """Edge directionality."""

    DIRECTED = "DIRECTED"
    UNDIRECTED = "UNDIRECTED"


class EdgeCategory(StrEnum):
    """Semantic edge classification."""

    CAPABILITY = "capability"
    TAXONOMY = "taxonomy"
    TRANSACTION = "transaction"
    EXCLUSION = "exclusion"
    PRECOMPUTED = "precomputed"
    MARKET = "market"
    CONTEXT = "context"
    REFERRAL = "referral"
    INTENT = "intent"


class GateType(StrEnum):
    """Gate type registry."""

    RANGE = "range"
    THRESHOLD = "threshold"
    BOOLEAN = "boolean"
    COMPOSITE = "composite"
    ENUMMAP = "enummap"
    EXCLUSION = "exclusion"
    SELFRANGE = "selfrange"
    FRESHNESS = "freshness"
    TEMPORALRANGE = "temporalrange"
    TRAVERSAL = "traversal"


class NullBehavior(StrEnum):
    """NULL value handling strategy."""

    PASS = "pass"
    FAIL = "fail"


class ComputationType(StrEnum):
    """Scoring computation function registry."""

    GEODECAY = "geodecay"
    LOGNORMALIZED = "lognormalized"
    COMMUNITYMATCH = "communitymatch"
    INVERSELINEAR = "inverselinear"
    CANDIDATEPROPERTY = "candidateproperty"
    WEIGHTEDRATE = "weightedrate"
    PRICEALIGNMENT = "pricealignment"
    TEMPORALPROXIMITY = "temporalproximity"
    CUSTOMCYPHER = "customcypher"
    TRAVERSALALIAS = "traversalalias"
    KGE = "kge"
    VARIANTDISCOVERY = "variantdiscovery"
    ENSEMBLECONFIDENCE = "ensembleconfidence"
    PREFERENCEATTENTION = "preference_attention"
    COMMUNITYBRIDGE = "community_bridge"


class ScoringSource(StrEnum):
    """Scoring dimension data source."""

    CANDIDATEPROPERTY = "candidateproperty"
    QUERYPROPERTY = "queryproperty"
    COMPUTED = "computed"
    TRAVERSALALIAS = "traversalalias"
    EXTERNAL = "external"


class NullStrategy(StrEnum):
    """Null score handling strategy for scoring dimensions (HGKR S2-01).

    Controls behavior when a scoring dimension cannot compute for a candidate.
    Inspired by HGKR Equation 4: non-participant embedding inheritance.
    """

    ZERO = "zero"  # Current default: score 0.0 when dimension can't compute
    INHERIT_PRIOR = "inherit_prior"  # Carry forward last successful score
    POPULATION_MEAN = "population_mean"  # Substitute population mean for dimension


class AggregationStrategy(StrEnum):
    """GDS aggregation strategy (HGKR S2-03).

    Controls how neighbor signals are aggregated during graph computation.
    Paper: GAT for item-tailed graphs, GraphSAGE for entity-tailed graphs.
    Maps HGKR aggregator types to CEG equivalents:
    - auto: Auto-select based on edge category and node role
    - mean: GraphSAGE-style mean aggregation (default safe baseline)
    - attention_weighted: GAT-style attention (for item-tailed relations)
    - max: Max-pool aggregation
    - sample_aggregate: Sample + aggregate (context/temporal)
    """

    AUTO = "auto"  # Auto-select based on edge category and node role
    MEAN = "mean"  # Equal neighbor treatment (entity/taxonomy endpoints)
    ATTENTION_WEIGHTED = "attention_weighted"  # Attention-based (candidate endpoints)
    MAX_POOL = "max"  # Max-pool aggregation
    SAMPLE_AGGREGATE = "sample_aggregate"  # Sample + aggregate (context/temporal)


class ColdStartFallback(StrEnum):
    """Cold-start fallback strategy for preference_attention (HGKR S2-20).

    Controls scoring when a candidate has zero historical outcomes.
    Paper motivation: KGs solve cold-start via graph-structural signals.
    """

    ZERO = "zero"  # Score 0.0 for cold-start entities
    STRUCTURAL_SIMILARITY = "structural_similarity"  # Adamic-Adar / shared neighbors
    POPULATION_MEAN = "population_mean"  # Average score across all candidates


class ScoringAggregation(StrEnum):
    """Score combination method."""

    ADDITIVE = "additive"
    MULTIPLICATIVE = "multiplicative"


class SyncStrategy(StrEnum):
    """Batch sync Cypher generation strategy."""

    UNWINDMERGE = "UNWINDMERGE"
    UNWINDMATCHSET = "UNWINDMATCHSET"


def _shape_defect(owner: str, type_: PropertyType, items: object, properties: object) -> str | None:
    """Why a declared type and its ``items`` / ``properties`` disagree, else None."""
    if type_ is PropertyType.LIST:
        if items is None:
            return f"{owner}: type list requires items (the element type)"
        if properties is not None:
            return f"{owner}: type list declares its element shape in items, not properties"
    elif type_ is PropertyType.MAP:
        if not properties:
            return f"{owner}: type map requires a non-empty properties list"
        if items is not None:
            return f"{owner}: type map declares its keys in properties, not items"
    elif items is not None or properties is not None:
        return f"{owner}: items/properties are declared only for type list or map, not {type_.value}"
    return None


def _duplicate_property_defect(owner: str, properties: list[PropertySpec] | None) -> str | None:
    names = [prop.name for prop in properties or []]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        return f"{owner}: duplicate map property name(s) {duplicates}"
    return None


class ItemSpec(BaseModel):
    """Element type of a ``list`` property: a primitive, an enum, or a ``map``.

    A list of lists is refused: Neo4j cannot store one and no sync shape
    consumes one. Unknown keys are refused (a new model, strict from the start).
    """

    model_config = ConfigDict(extra="forbid")

    type: PropertyType
    values: list[str] | None = None  # For enum items
    properties: list[PropertySpec] | None = None  # For map items
    unit: str | None = None
    description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def refuse_list_of_list(cls, data: object) -> object:
        """Named before the generic unknown-key refusal would report ``items.items``."""
        if isinstance(data, dict) and data.get("type") == PropertyType.LIST.value:
            msg = "items: a list of lists is not a declarable shape"
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def validate_item_shape(self) -> ItemSpec:
        """A map item declares its properties; a scalar item declares none."""
        defect = _shape_defect("items", self.type, None, self.properties) or _duplicate_property_defect(
            "items", self.properties
        )
        if defect is not None:
            raise ValueError(defect)
        return self


class PropertySpec(BaseModel):
    """Property definition for nodes and edges.

    ``items`` is required iff ``type`` is ``list`` (the element type: any
    primitive, enum, or ``map``); ``properties`` is required iff ``type`` is
    ``map`` (its keys, each a ``PropertySpec``). YAML syntax::

        - {name: certifications, type: list, items: {type: string}}
        - name: stocked_grades
          type: list
          items:
            type: map
            properties:
              - {name: code, type: string, required: true}
    """

    name: str
    type: PropertyType
    unit: str | None = None
    required: bool = False
    nullable: bool = True
    values: list[str] | None = None  # For enum types
    managedby: ManagedByType | None = None
    description: str | None = None
    items: ItemSpec | None = None  # For list types
    properties: list[PropertySpec] | None = None  # For map types

    @model_validator(mode="after")
    def validate_shape(self) -> PropertySpec:
        """``items`` iff list, ``properties`` iff map (purely additive: scalars are unchanged)."""
        owner = f"property {self.name!r}"
        defect = _shape_defect(owner, self.type, self.items, self.properties) or _duplicate_property_defect(
            owner, self.properties
        )
        if defect is not None:
            raise ValueError(defect)
        return self

    @property
    def wire_only(self) -> bool:
        """``map`` and ``list<map>``: a sync wire shape Neo4j cannot store as a property."""
        if self.type is PropertyType.MAP:
            return True
        return self.type is PropertyType.LIST and self.items is not None and self.items.type is PropertyType.MAP

    @property
    def declared_type(self) -> str:
        """The declared type as defects render it: ``float``, ``list<string>``, ``list<map>``."""
        return render_declared_type(self.type, self.values, self.items)


def render_declared_type(type_: PropertyType, values: list[str] | None, items: ItemSpec | None) -> str:
    """``list<map>``, ``list<enum[a|b]>``, ``enum[a|b]``, ``float`` — one spelling for every defect."""
    if type_ is PropertyType.LIST and items is not None:
        return f"list<{render_declared_type(items.type, items.values, None)}>"
    if type_ is PropertyType.ENUM and values:
        return f"enum[{'|'.join(values)}]"
    return type_.value


ItemSpec.model_rebuild()
PropertySpec.model_rebuild()


class NodeSpec(BaseModel):
    """Node label specification."""

    label: str
    description: str | None = None
    managedby: ManagedByType | None = None
    candidate: bool = False
    queryentity: bool = False
    taxonomy: bool = False
    auxiliary: bool = False
    matchdirection: str | None = None
    properties: list[PropertySpec] = Field(default_factory=list)


class EdgeSpec(BaseModel):
    """Edge type specification."""

    type: str
    from_: str = Field(..., alias="from")
    to: str
    direction: EdgeDirection
    category: EdgeCategory
    managedby: ManagedByType
    properties: list[PropertySpec] = Field(default_factory=list)
    # Server-owned approved relevance of this evidence edge type in [0, 1]
    # (engine/config/tenant_mapping.py; honoured only at -reviewed/-production).
    evidencerelevance: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)

    class Config:
        populate_by_name = True

    @field_validator("evidencerelevance", mode="before")
    @classmethod
    def refuse_non_numeric_relevance(cls, value: object) -> object:
        """A bool or a quoted number is not a relevance coefficient (lax float would coerce both)."""
        if isinstance(value, bool | str | bytes):
            msg = f"evidencerelevance must be a number in [0, 1], got {type(value).__name__}"
            raise ValueError(msg)
        return value


class OntologySpec(BaseModel):
    """Complete ontology (nodes + edges)."""

    nodes: list[NodeSpec]
    edges: list[EdgeSpec]

    @field_validator("nodes", mode="before")
    @classmethod
    def coerce_nodes_dict_to_list(cls, v: object) -> object:
        """Accept dict-grouped nodes (e.g. {candidates: [...], query_entities: [...]})
        in addition to the canonical flat list format."""
        if isinstance(v, dict):
            merged: list[object] = []
            for group in v.values():
                if isinstance(group, list):
                    merged.extend(group)
            return merged
        return v

    @field_validator("nodes")
    @classmethod
    def validate_unique_labels(cls, nodes: list[NodeSpec]) -> list[NodeSpec]:
        """Ensure node labels are unique."""
        labels = [n.label for n in nodes]
        if len(labels) != len(set(labels)):
            raise ValueError("Duplicate node labels detected in ontology")
        return nodes

    @field_validator("edges", mode="before")
    @classmethod
    def coerce_edges_dict_to_list(cls, v: object) -> object:
        """Accept dict-grouped edges in addition to canonical flat list format."""
        if isinstance(v, dict):
            merged: list[object] = []
            for group in v.values():
                if isinstance(group, list):
                    merged.extend(group)
            return merged
        return v

    @field_validator("edges")
    @classmethod
    def validate_unique_edge_types(cls, edges: list[EdgeSpec]) -> list[EdgeSpec]:
        """Ensure edge type + from + to tuples are unique."""
        signatures = [(e.type, e.from_, e.to) for e in edges]
        if len(signatures) != len(set(signatures)):
            raise ValueError("Duplicate edge type signatures detected")
        return edges


class MatchEntitySpec(BaseModel):
    """Match entity configuration (candidate or query)."""

    label: str
    matchdirection: str


class MatchEntitiesSpec(BaseModel):
    """Bidirectional match entity definitions."""

    candidate: list[MatchEntitySpec]
    queryentity: list[MatchEntitySpec]


class QueryFieldSpec(BaseModel):
    """Query schema field definition."""

    name: str
    type: PropertyType
    unit: str | None = None
    required: bool = False
    default: Any | None = None
    mapstotaxonomy: str | None = None  # Target taxonomy node label

    @field_validator("type")
    @classmethod
    def refuse_shape_type(cls, value: PropertyType) -> PropertyType:
        """``list`` / ``map`` are property shapes (``PropertySpec``), not match query parameters."""
        if value in SHAPE_TYPES:
            msg = f"query field type {value.value!r} is a property shape; query fields are scalars"
            raise ValueError(msg)
        return value


class QuerySchemaSpec(BaseModel):
    """Query schema for /v1/match requests."""

    matchdirections: list[str]
    fields: list[QueryFieldSpec]


class DerivedParameterSpec(BaseModel):
    """Computed parameter from query fields."""

    name: str
    expression: str  # Simple algebraic expression
    type: PropertyType
    unit: str | None = None
    description: str | None = None

    @field_validator("type")
    @classmethod
    def refuse_shape_type(cls, value: PropertyType) -> PropertyType:
        """A derived parameter is a computed scalar; ``list`` / ``map`` are property shapes."""
        if value in SHAPE_TYPES:
            msg = f"derived parameter type {value.value!r} is a property shape; derived parameters are scalars"
            raise ValueError(msg)
        return value


class TraversalStepSpec(BaseModel):
    """Traversal pattern declaration."""

    name: str
    description: str | None = None
    pattern: str  # Cypher pattern fragment
    required: bool = True
    matchdirections: list[str] | None = None


class TraversalSpec(BaseModel):
    """Traversal configuration."""

    steps: list[TraversalStepSpec]

    @model_validator(mode="after")
    def validate_traversal_hops(self) -> TraversalSpec:
        """W1-04: Validate traversal hop counts against hard cap.

        Ensures no variable-length path exceeds MAX_HOP_HARD_CAP.
        """
        import re

        from engine.config.settings import settings

        max_cap = settings.max_hop_hard_cap
        if settings.traversal_open_range_rejected:
            return self._validate_hop_ranges(max_cap)
        hop_re = re.compile(r"\*(\d+)?(?:\.\.(\d+))?")

        for step in self.steps:
            for match in hop_re.finditer(step.pattern):
                min_hops_str, max_hops_str = match.group(1), match.group(2)
                # Validate min hops
                if min_hops_str:
                    min_hops = int(min_hops_str)
                    if min_hops < 1:
                        msg = f"Traversal step '{step.name}': hop count must be >= 1, got {min_hops}"
                        raise ValueError(msg)
                # Validate max hops
                if max_hops_str:
                    max_hops = int(max_hops_str)
                elif min_hops_str:
                    max_hops = int(min_hops_str)
                else:
                    # unbounded '*' — flag it
                    max_hops = max_cap + 1
                if max_hops < 1:
                    msg = f"Traversal step '{step.name}': hop count must be >= 1, got {max_hops}"
                    raise ValueError(msg)
                if max_hops > max_cap:
                    msg = f"Traversal step '{step.name}': max hops {max_hops} exceeds hard cap {max_cap}"
                    raise ValueError(msg)

        return self

    def _validate_hop_ranges(self, max_cap: int) -> TraversalSpec:
        """RU-045 / CEG-COR-038: parse the whole `*[min][..[max]]` range token.

        The legacy regex matched `*1` inside `*1..` and read it as exactly one
        hop, so `*1..` / `*2..` loaded and were emitted unbounded. A `..` must be
        followed by an explicit upper bound; that bound is checked against the cap.
        """
        import re

        hop_re = re.compile(r"\*(\d*)(\.\.)?(\d*)")
        for step in self.steps:
            for match in hop_re.finditer(step.pattern):
                min_hops_str, range_sep, max_hops_str = match.group(1), match.group(2), match.group(3)
                if min_hops_str and int(min_hops_str) < 1:
                    msg = f"Traversal step '{step.name}': hop count must be >= 1, got {int(min_hops_str)}"
                    raise ValueError(msg)
                if range_sep and not max_hops_str:
                    msg = (
                        f"Traversal step '{step.name}': variable-length range '{match.group(0)}' "
                        "has no explicit upper bound"
                    )
                    raise ValueError(msg)
                if max_hops_str:
                    max_hops = int(max_hops_str)
                elif min_hops_str:
                    max_hops = int(min_hops_str)
                else:
                    # unbounded '*' — flag it
                    max_hops = max_cap + 1
                if max_hops < 1:
                    msg = f"Traversal step '{step.name}': hop count must be >= 1, got {max_hops}"
                    raise ValueError(msg)
                if max_hops > max_cap:
                    msg = f"Traversal step '{step.name}': max hops {max_hops} exceeds hard cap {max_cap}"
                    raise ValueError(msg)
        return self


class EdgePredicateSpec(BaseModel):
    """One predicate on the relationship a traversal gate matches.

    ``equals`` is a literal operand (bound by the compiler, never a parameter
    name). ``present`` requires the property to be a non-empty string. Exactly
    one of the two is set. Unknown keys are refused.
    """

    model_config = ConfigDict(extra="forbid")

    property: str
    equals: str | int | float | bool | None = None
    present: bool = False

    @model_validator(mode="after")
    def exactly_one_test(self) -> EdgePredicateSpec:
        has_equals = self.equals is not None
        if has_equals == self.present:
            msg = f"edge predicate {self.property!r} must set exactly one of equals or present"
            raise ValueError(msg)
        return self


class GateSpec(BaseModel):
    """Gate definition."""

    name: str
    description: str | None = None
    type: GateType
    candidateprop: str | None = None
    # A string is the NAME of a declared query parameter (`queryparam: density`
    # → `$density`). A YAML scalar (`queryparam: 85.0`, `queryparam: true`) is a
    # literal OPERAND: the compiler binds it under a generated parameter name
    # (CEG-009b). The earlier validator stringified scalars, so `true` became
    # the parameter name `$True` and `85.0` the refused name `$85.0`.
    queryparam: str | int | float | bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_keys(cls, data: object) -> object:
        """RU-053 / CEG-COR-081: refuse a gate key that is not a declared field.

        Pydantic ignores unknown keys by default, so the documented-but-wrong
        `null_behavior: fail` compiled as the PASS default. Field names are the
        YAML keys (no aliases, PYDANTIC_YAML_MAPPING.md), so `model_fields` is
        the complete set of accepted keys.
        """
        from engine.config.settings import settings

        if not settings.gate_spec_forbid_unknown_keys or not isinstance(data, dict):
            return data
        unknown = sorted(str(key) for key in data if key not in cls.model_fields)
        if unknown:
            by_squashed = {name.replace("_", "").lower(): name for name in cls.model_fields}
            hints = [
                f"'{key}' (did you mean '{by_squashed[key.replace('_', '').lower()]}'?)"
                for key in unknown
                if key.replace("_", "").lower() in by_squashed
            ]
            msg = f"Gate '{data.get('name')}' has unknown key(s) {unknown}"
            if hints:
                msg += f"; {', '.join(hints)}"
            raise ValueError(msg)
        return data

    @field_validator("queryparam", mode="before")
    @classmethod
    def validate_queryparam_shape(cls, v: object) -> object:
        """Accept a parameter name or a scalar literal; refuse anything else outright."""
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        msg = f"queryparam must be a parameter name or a scalar literal, got {type(v).__name__}"
        raise ValueError(msg)

    operator: str | None = None

    @field_validator("operator", mode="before")
    @classmethod
    def validate_operator(cls, v: object) -> object:
        """W1-03: Validate gate operator is a known safe value."""
        if v is None:
            return v
        allowed = {">=", "<=", ">", "<", "=", "!=", "<>", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH"}
        if str(v) not in allowed:
            msg = f"Gate operator '{v}' is not in the allowed set: {sorted(allowed)}"
            raise ValueError(msg)
        return v

    logic: str | None = None  # For composite gates: "AND" / "OR"
    nullbehavior: NullBehavior = NullBehavior.PASS
    roleexempt: list[str] | None = None
    relaxedpenalty: float = 0.0
    matchdirections: list[str] | None = None
    invertible: bool = False
    strictwhen: str | None = None  # Conditional strictness expression
    cypheroverride: str | None = None  # Custom Cypher for complex gates
    # Type-specific fields
    subgates: list[str] | None = None  # For composite
    mapping: dict[str, list[str]] | None = None  # For enummap
    edgetype: str | None = None  # For exclusion
    fromnode: str | None = None
    tonode: str | None = None
    maxagedays: int | None = None  # For freshness
    candidateprop_min: str | None = None  # For selfrange, temporalrange
    candidateprop_max: str | None = None
    queryparam_min: str | None = None  # For range, temporalrange
    queryparam_max: str | None = None
    queryparam_start: str | None = None  # For temporalrange
    queryparam_end: str | None = None
    candidateprop_start: str | None = None
    candidateprop_end: str | None = None
    pattern: str | None = None  # For traversal gate
    condition: str | None = None
    # Traversal only: predicates on the matched relationship, AND-ed with the
    # target-node comparison inside the same EXISTS. Empty keeps the target-only
    # form. Honoured when buyer_line_identity_enabled is on.
    edgepredicates: list[EdgePredicateSpec] | None = None


# ND-DEAD (CEG scoring audit L2): declared options no engine code reads. A value
# other than the default promised behaviour the engine never delivered.
_UNREAD_FEEDBACK_BLEND = 0.7


def _dead_option_defect(keys: list[str]) -> str:
    return f"option(s) {', '.join(keys)} are declared but not read by any engine code; remove them"


def _scoring_strict() -> bool:
    """D3 rollback switch, read at validation time."""
    from engine.config.settings import settings

    return bool(settings.scoring_spec_strict_validation_enabled)


def _traversal_binds(traversal: TraversalSpec | None, alias: str, direction: str) -> bool:
    """Whether a traversal step applicable to ``direction`` declares a node bound to ``alias``.

    ``TraversalAssembler`` filters steps by ``matchdirections`` exactly so: a
    step without them applies to every direction.
    """
    if traversal is None:
        return False
    node_re = re.compile(rf"\(\s*{re.escape(alias)}\s*(?::[A-Za-z_][A-Za-z0-9_]*)?\s*[)\{{]")
    return any(
        node_re.search(step.pattern)
        for step in traversal.steps
        if not step.matchdirections or direction in step.matchdirections
    )


class ScoringDimensionMetadata(BaseModel):
    """Computation-specific declarations of a scoring dimension (``metadata:``).

    Exactly the keys ``ScoringAssembler`` reads, typed. Unknown keys are refused
    (a new model, strict from the start), so a misspelled key fails validation
    instead of being dropped; a key the dimension's computation does not read is
    refused by ``ScoringDimensionSpec``.

    ``preference_attention``: ``success_value`` (required at admission — which
    outcome value counts as success is the domain's declaration),
    ``success_property``, ``outcome_relation``, ``outcome_node``,
    ``outcome_community_property``. ``community_bridge``: ``community_properties``.
    ``temporalproximity``: ``frequency_property``, ``frequency_weight``,
    ``frequency_scale``, ``acceleration_property``, ``acceleration_weight``.
    """

    model_config = ConfigDict(extra="forbid")

    # --- preference_attention ---
    success_value: str | None = None
    success_property: str | None = None
    outcome_relation: str | None = None
    outcome_node: str | None = None
    outcome_community_property: str | None = None
    # --- community_bridge ---
    community_properties: list[str] | None = None
    # --- temporalproximity (CEG-FEEDBACK-1, FEATURE_GATES §37) ---
    # Auxiliary recency signals, each contributing only when declared: a count
    # property (divided by frequency_scale, clamped to [0, 1]) and an
    # acceleration flag (0/1).
    frequency_property: str | None = None
    frequency_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency_scale: float | None = Field(default=None, gt=0.0)
    acceleration_property: str | None = None
    acceleration_weight: float | None = Field(default=None, ge=0.0, le=1.0)


# The metadata keys each computation reads (engine/scoring/assembler.py).
_METADATA_KEYS_READ: dict[ComputationType, frozenset[str]] = {
    ComputationType.PREFERENCEATTENTION: frozenset(
        {"success_value", "success_property", "outcome_relation", "outcome_node", "outcome_community_property"}
    ),
    ComputationType.COMMUNITYBRIDGE: frozenset({"community_properties"}),
    ComputationType.TEMPORALPROXIMITY: frozenset(
        {"frequency_property", "frequency_weight", "frequency_scale", "acceleration_property", "acceleration_weight"}
    ),
}


def dimension_metadata(dim: Any) -> Any:
    """A scoring dimension's metadata as the mapping the compilers read.

    A pack-declared ``ScoringDimensionMetadata`` yields its declared keys (an
    unset or null key is absent, so the compilers' defaults apply). A mapping
    assigned by code that builds a dimension directly is returned unchanged; no
    metadata is ``{}``.
    """
    raw = getattr(dim, "metadata", None)
    if not raw:
        return {}
    if isinstance(raw, ScoringDimensionMetadata):
        return raw.model_dump(exclude_none=True)
    return raw


class ScoringDimensionSpec(BaseModel):
    """Scoring dimension definition."""

    name: str
    description: str | None = None
    source: ScoringSource
    candidateprop: str | None = None
    queryprop: str | None = None
    alias: str | None = None  # Traversal alias reference
    computation: ComputationType
    expression: str | None = None  # For customcypher
    normalization: str | None = None
    minvalue: float | None = None
    maxvalue: float | None = None
    defaultwhennull: float = 0.0
    decayconstant: float | None = None  # For geodecay: distance k in meters
    # For geodecay: longitude operands (RU-037); default candidate.lon / $lon.
    candidateprop_lon: str | None = None
    queryprop_lon: str | None = None
    bias: float | None = None  # For communitymatch: multiplicative boost
    weightkey: str
    defaultweight: float
    aggregation: ScoringAggregation = ScoringAggregation.ADDITIVE
    matchdirections: list[str] | None = None
    edge_type_strategy: dict[str, ComputationType] = Field(
        default_factory=dict,
        description="Per-edge-type computation overrides",
    )
    # --- HGKR Pass 2 extensions ---
    null_strategy: NullStrategy = NullStrategy.ZERO  # S2-01: null inheritance
    entity_types: list[str] = Field(default_factory=list)  # S2-12: entity type tracking
    cold_start_fallback: ColdStartFallback = ColdStartFallback.ZERO  # S2-20: cold-start
    soft_match: bool = False  # S2-17: graduated community scoring (communitymatch only)
    # Computation-specific declarations (preference_attention, community_bridge).
    metadata: ScoringDimensionMetadata | None = None

    @model_validator(mode="before")
    @classmethod
    def refuse_unknown_keys(cls, data: Any) -> Any:
        """D3 (scoring_spec_strict_validation_enabled): a scoring dimension key the model does not declare is refused.

        ``extra="forbid"`` semantics, applied under the rollback flag instead of
        ``model_config`` so the flag can restore the legacy (ignored) behaviour.
        """
        if not isinstance(data, dict) or not _scoring_strict():
            return data
        unknown = sorted(str(key) for key in data if key not in cls.model_fields)
        if unknown:
            name = data.get("name", "?")
            msg = f"Scoring dimension {name!r}: unknown key(s) {', '.join(unknown)}"
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def validate_structural_values(self) -> ScoringDimensionSpec:
        """D3: refuse values the scoring compiler cannot honour (scoring_spec_strict_validation_enabled)."""
        if not _scoring_strict():
            return self
        if not math.isfinite(self.defaultweight) or self.defaultweight < 0.0:
            msg = f"Scoring dimension {self.name!r}: defaultweight must be finite and >= 0, got {self.defaultweight!r}"
            raise ValueError(msg)
        if not math.isfinite(self.defaultwhennull):
            msg = f"Scoring dimension {self.name!r}: defaultwhennull must be finite, got {self.defaultwhennull!r}"
            raise ValueError(msg)
        if self.aggregation is ScoringAggregation.MULTIPLICATIVE:
            msg = (
                f"Scoring dimension {self.name!r}: aggregation 'multiplicative' is not implemented "
                "(the score is always the additive weighted sum)"
            )
            raise ValueError(msg)
        dead = [
            key
            for key, unset in (("normalization", None), ("edge_type_strategy", {}), ("entity_types", []))
            if getattr(self, key) != unset
        ]
        if dead:
            msg = f"Scoring dimension {self.name!r}: {_dead_option_defect(dead)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_metadata_keys(self) -> ScoringDimensionSpec:
        """Refuse a metadata key the dimension's computation does not read."""
        if self.metadata is None:
            return self
        read = _METADATA_KEYS_READ.get(self.computation, frozenset())
        unread = sorted(self.metadata.model_fields_set - read)
        if unread:
            msg = (
                f"Scoring dimension {self.name!r}: metadata key(s) {', '.join(unread)} are not read by "
                f"computation {self.computation.value!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_numeric_scales(self) -> ScoringDimensionSpec:
        """SCR-09: refuse a divisor or scale the compiler cannot divide by.

        Under ``scoring_spec_strict_validation_enabled`` (the D3 strict-validation
        switch, ``_scoring_strict``; default on): ``maxvalue`` <= 0 where it is a divisor or scale
        (lognormalized, pricealignment, temporalproximity), ``decayconstant`` <= 0
        for geodecay, and ``minvalue`` >= ``maxvalue`` for inverselinear (with the
        compiler defaults 0 and 100 for an undeclared bound).
        """
        if not _scoring_strict():
            return self
        defect = _numeric_scale_defect(self)
        if defect:
            msg = f"Scoring dimension {self.name!r}: {defect}"
            raise ValueError(msg)
        return self


_MAXVALUE_SCALED = frozenset(
    {ComputationType.LOGNORMALIZED, ComputationType.PRICEALIGNMENT, ComputationType.TEMPORALPROXIMITY}
)


def _numeric_scale_defect(dim: ScoringDimensionSpec) -> str | None:
    """SCR-09: the first divisor/scale defect of ``dim``, or ``None``."""
    computation = dim.computation
    if computation in _MAXVALUE_SCALED and dim.maxvalue is not None and dim.maxvalue <= 0:
        return f"maxvalue must be > 0 for {computation.value}, got {dim.maxvalue!r}"
    if computation is ComputationType.GEODECAY and dim.decayconstant is not None and dim.decayconstant <= 0:
        return f"decayconstant must be > 0 for geodecay, got {dim.decayconstant!r}"
    if computation is ComputationType.INVERSELINEAR:
        low = dim.minvalue if dim.minvalue is not None else 0.0
        high = dim.maxvalue if dim.maxvalue is not None else 100.0
        if low >= high:
            return f"inverselinear needs minvalue < maxvalue, got minvalue={low!r}, maxvalue={high!r}"
    return None


class AdaptiveSampleSizeSpec(BaseModel):
    """Adaptive K-parameter configuration (HGKR S2-13).

    Auto-tunes preference_sample_size based on graph density.
    Paper: K=24 for sparse (ml-latest), K=32 for dense (MOOCCube).
    """

    enabled: bool = False
    min_k: int = 16
    max_k: int = 48
    density_metric: str = "avg_edges_per_node"  # or "total_interactions"


class ScoringSpec(BaseModel):
    """Scoring configuration."""

    dimensions: list[ScoringDimensionSpec]
    preference_sample_size: int | str = Field(
        default=28,
        description="Historical preference sample size for attention scoring (int or 'auto')",
    )
    adaptive_sample_size: AdaptiveSampleSizeSpec = Field(default_factory=AdaptiveSampleSizeSpec)
    preference_attention_feedback_blend: float = 0.7  # S2-06: alpha for feedback fusion

    @field_validator("preference_sample_size", mode="before")
    @classmethod
    def refuse_non_integer_sample_size(cls, value: Any) -> Any:
        """D3: judge the declared value before ``int | str`` coerces it (``True`` -> 1, ``16.0`` -> 16)."""
        if not _scoring_strict():
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"preference_sample_size must be an integer, got {value!r} (no value resolves 'auto')"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_structure(self) -> ScoringSpec:
        """D3: duplicate dimension names and an unresolved sample size are refused (scoring_spec_strict_validation_enabled).

        A dimension name is its Cypher alias and its learned-weight key, so a
        second declaration silently shadows the first. ``preference_sample_size``
        is bound as an integer ``LIMIT``; ``auto`` (or any string) is never resolved.
        """
        if not _scoring_strict():
            return self
        names = [dim.name for dim in self.dimensions]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            msg = f"Duplicate scoring dimension name(s): {duplicates}"
            raise ValueError(msg)
        size = self.preference_sample_size
        if isinstance(size, bool) or not isinstance(size, int):
            msg = f"preference_sample_size must be an integer, got {size!r} (no value resolves 'auto')"
            raise ValueError(msg)
        if size < 1:
            msg = f"preference_sample_size must be >= 1, got {size}"
            raise ValueError(msg)
        dead = []
        if self.adaptive_sample_size != AdaptiveSampleSizeSpec():
            dead.append("adaptive_sample_size")
        if not math.isclose(self.preference_attention_feedback_blend, _UNREAD_FEEDBACK_BLEND):
            dead.append("preference_attention_feedback_blend")
        if dead:
            msg = f"scoring: {_dead_option_defect(dead)}"
            raise ValueError(msg)
        return self


class SoftSignalSpec(BaseModel):
    """Soft signal bonus/penalty."""

    name: str
    bonus: float
    candidateprop: str | None = None
    queryparam: str | None = None
    threshold: Any | None = None
    matchtype: str | None = None  # equals, lessthan, greaterthan, between
    condition: str | None = None
    matchdirections: list[str] | None = None


class TaxonomyEdgeSpec(BaseModel):
    """Taxonomy edge for sync auto-linking.

    ``field`` is a property the endpoint's ``targetnode`` declares as a scalar
    (one edge) or ``list<scalar>`` (one edge per element); admission refuses any
    other declaration (``engine/sync/row_contract.py``).
    """

    field: str
    edgetype: str
    targetlabel: str
    targetid: str


class ChildSyncSpec(BaseModel):
    """Nested child entity sync.

    ``field`` is a property the endpoint's ``targetnode`` declares as
    ``list<map>``; ``targetid`` is a required property of that map. The shape
    is a declaration, checked at admission (``endpoint_shape_defects`` in
    ``engine/sync/row_contract.py``): a pack whose childsync field is not
    declared this way is withheld. Checking each row against it at sync
    ingress is the sync row contract's job, not this model's. On the wire the
    children are a JSON array of objects, never a JSON-encoded string.
    """

    field: str  # targetnode property declared list<map>; one child per element
    targetnode: str
    targetid: str
    edgetype: str
    edgedirection: str  # parenttochild, childtoparent
    # When set, each item is also a relationship identity: MERGE keys the edge
    # on ``edgeidentity`` and writes ``edgeproperties`` on that relationship,
    # not on the child node. ``fromrow`` names parent-row keys copied onto an
    # item that omits them (the item value wins). Honoured when
    # buyer_line_identity_enabled is on. Admission: engine/sync/row_contract.py.
    edgeidentity: str | None = None
    edgeproperties: list[str] | None = None
    fromrow: list[str] | None = None


PROVIDER_TOKEN_PATTERN = r"^[a-z][a-z0-9_-]{1,31}$"
PROVIDER_OBJECT_TYPE_PATTERN = r"^(?:[a-z][a-z0-9_]{0,63}|\d+-\d+)$"
PROVIDER_ASSOCIATION_CATEGORY_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
REF_NAMESPACE_PATTERN = r"^[a-z0-9_.-]{1,64}$"


class ObservedAssociationSourceSpec(BaseModel):
    """A provider association a relationship endpoint accepts as an observation.

    Records of ``fromobjecttype`` are the endpoint's ``fromnode`` (subject),
    records of ``toobjecttype`` its ``tonode`` (object). The block names the
    provider-side identity of the observation only; the relationship type and
    labels come from the enclosing endpoint, and relevance is never declared here.
    Object types are provider-native (D-4); the category is pattern-checked, not
    enumerated (D-5); the provider is a neutral token (D-3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(pattern=PROVIDER_TOKEN_PATTERN)
    fromobjecttype: str = Field(pattern=PROVIDER_OBJECT_TYPE_PATTERN)
    toobjecttype: str = Field(pattern=PROVIDER_OBJECT_TYPE_PATTERN)
    associationcategory: str = Field(pattern=PROVIDER_ASSOCIATION_CATEGORY_PATTERN)
    associationtypeid: StrictInt = Field(ge=1)

    @property
    def key(self) -> tuple[str, str, str, str, int]:
        """The association identity; unique across a spec (one observation, one relationship)."""
        return (
            self.provider,
            self.fromobjecttype,
            self.toobjecttype,
            self.associationcategory,
            self.associationtypeid,
        )


class SyncEndpointSpec(BaseModel):
    """Sync endpoint definition."""

    path: str
    method: str = "POST"
    targetnode: str | None = None
    targetedge: str | None = None
    fromnode: str | None = None
    tonode: str | None = None
    idproperty: str | None = None
    batchstrategy: SyncStrategy
    taxonomyedges: list[TaxonomyEdgeSpec] | None = None
    childsync: list[ChildSyncSpec] | None = None
    fieldsupdated: list[str] | None = None  # For PATCH operations
    # Node endpoints only: node ids are graph refs ``<refnamespace>:<record id>`` (D-13).
    refnamespace: str | None = Field(default=None, pattern=REF_NAMESPACE_PATTERN)
    # Relationship endpoints only: provider associations this edge accepts as observations.
    observedfrom: list[ObservedAssociationSourceSpec] | None = None


class SyncSpec(BaseModel):
    """Sync configuration."""

    endpoints: list[SyncEndpointSpec]


class GDSJobScheduleSpec(BaseModel):
    """GDS job schedule."""

    type: str  # cron, manual
    cron: str | None = None


class GDSProjectionSpec(BaseModel):
    """GDS graph projection."""

    nodelabels: list[str]
    edgetypes: list[str]


class GDSNodeSimilaritySpec(BaseModel):
    """Bounds for a ``nodeSimilarity`` GDS job (CEG-GDS-SIM).

    Maps one-to-one onto the ``gds.nodeSimilarity.write`` configuration keys
    ``topK``, ``similarityCutoff``, ``degreeCutoff`` and ``similarityMetric``.
    ``topk`` is always sent and capped, so a run writes at most ``topk``
    relationships per compared node. ``similaritycutoff`` left unset keeps the
    GDS default (1E-42: zero-similarity pairs are never written).
    """

    model_config = ConfigDict(extra="forbid")

    topk: int = Field(default=10, ge=1, le=100, description="Similar nodes written per source node")
    similaritycutoff: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Lowest similarity score written; unset keeps the GDS default"
    )
    degreecutoff: int = Field(default=1, ge=1, description="Lowest node degree compared")
    similaritymetric: Literal["JACCARD", "OVERLAP", "COSINE"] = "JACCARD"


class GDSJobSpec(BaseModel):
    """GDS job definition."""

    name: str
    algorithm: str
    schedule: GDSJobScheduleSpec
    projection: GDSProjectionSpec
    writeproperty: str | None = None
    writeto: str | None = None  # Target node label
    writeedge: str | None = None
    writeproperties: list[dict[str, Any]] | None = None
    sourceedge: str | None = None
    filter: str | None = None
    aggregate: str | None = None
    ratenumeratorfilter: str | None = None
    ratedenominatorfilter: str | None = None
    recencydecay: bool = False
    recencyhalflifedays: int | None = None
    # --- HGKR Pass 2 extensions ---
    depends_on: list[str] = Field(
        default_factory=list,
        description="Job algorithm names that must complete before this job runs (DAG ordering)",
    )
    propagation_depth: int = Field(
        default=2,
        ge=1,
        le=5,
        description="L-hop propagation depth for iterative algorithms",
    )
    aggregation_strategy: AggregationStrategy = Field(
        default=AggregationStrategy.AUTO,
        description="Neighborhood aggregation method",
    )
    per_relation_params: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-relation-type algorithm parameter overrides",
    )
    stability_runs: int = 1  # S2-15: multi-run averaging for non-deterministic algos
    # CEG-GDS-SIM: bounds for algorithm ``nodeSimilarity``; unset uses the
    # GDSNodeSimilaritySpec defaults. Ignored by every other algorithm.
    nodesimilarity: GDSNodeSimilaritySpec | None = None


class GDSSpec(BaseModel):
    """GDS jobs configuration."""

    jobs: list[GDSJobSpec] = Field(default_factory=list, alias="gdsjobs")

    class Config:
        populate_by_name = True


class KGEBeamSearchSpec(BaseModel):
    """KGE beam search configuration."""

    beamwidth: int = 10
    maxdepth: int = 3


class KGEEnsembleSpec(BaseModel):
    """KGE ensemble strategy."""

    strategy: str  # weightedaverage, rankaggregation, mixtureofexperts
    cypherweight: float = 0.6
    kgeweight: float = 0.4


class KGEVectorIndexSpec(BaseModel):
    """Vector index for KGE embeddings."""

    name: str
    dimension: int = 256
    similarityfunction: str = "cosine"


class KGESpec(BaseModel):
    """Knowledge graph embedding configuration."""

    model: str = "CompoundE3D"
    embeddingdim: int = 256
    trainingrelations: list[str] = Field(default_factory=list)
    beamsearch: KGEBeamSearchSpec | None = None
    ensemble: KGEEnsembleSpec | None = None
    vectorindex: KGEVectorIndexSpec | None = None


class CalibrationPair(BaseModel):
    """A labeled calibration pair defining expected score range for two entities."""

    node_a: str
    node_b: str
    expected_score_min: float = Field(ge=0.0, le=1.0)
    expected_score_max: float = Field(ge=0.0, le=1.0)
    label: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> CalibrationPair:
        """Ensure min <= max."""
        if self.expected_score_min > self.expected_score_max:
            msg = f"Calibration pair: expected_score_min ({self.expected_score_min}) > expected_score_max ({self.expected_score_max})"
            raise ValueError(msg)
        return self


class CalibrationSpec(BaseModel):
    """Calibration configuration for forward simulation verification."""

    pairs: list[CalibrationPair] = Field(default_factory=list)
    weight_set: str | None = None


class ComplianceRegimeSpec(BaseModel):
    """Compliance regime identifier."""

    name: str
    description: str | None = None


class ProhibitedFactorsSpec(BaseModel):
    """Prohibited factors enforcement."""

    enabled: bool = True
    blockedfields: list[str] = Field(default_factory=list)
    enforcement: str = "compiletime"  # compiletime, runtime, compiletimeandruntime
    auditonviolation: bool = True


class AuditSpec(BaseModel):
    """Audit logging configuration."""

    enabled: bool = True
    logmatchrequests: bool = True
    logmatchresults: bool = True
    logsyncoperations: bool = True
    logadminoperations: bool = True
    retentiondays: int = 90


class PIISpec(BaseModel):
    """PII handling configuration."""

    enabled: bool = False
    fields: list[str] = Field(default_factory=list)
    candidatefields: list[str] = Field(default_factory=list)
    handling: str = "hash"  # hash, encrypt, redact, tokenize
    encryptionkeysource: str = "env"  # env, vault, kms


class RetentionSpec(BaseModel):
    """Data retention policies."""

    transactionttldays: int | None = None
    auditttldays: int | None = None
    piittldays: int | None = None


class ComplianceSpec(BaseModel):
    """Compliance and governance configuration."""

    regimes: list[ComplianceRegimeSpec] = Field(default_factory=list)
    prohibitedfactors: ProhibitedFactorsSpec | None = None
    audit: AuditSpec | None = None
    pii: PIISpec | None = None
    retention: RetentionSpec | None = None
    gdpr_subject_id_field: str = "data_subject_id"  # W6-02: configurable per-domain


class SignalWeightsSpec(BaseModel):
    """Signal weight retraining configuration."""

    model_config = {"frozen": True}

    enabled: bool = False
    min_weight: float = 0.1
    max_weight: float = 5.0
    lookback_days: int = 90
    min_sample_size: int = 10


class CausalEdgeTypeSpec(BaseModel):
    """Declared causal edge type for runtime validation."""

    model_config = {"frozen": True}

    edge_type: str
    confidence_threshold: float = 0.5


class CausalSpec(BaseModel):
    """Causal edge validation configuration."""

    model_config = {"frozen": True}

    enabled: bool = False
    causal_edges: list[CausalEdgeTypeSpec] = Field(default_factory=list)
    enforce_temporal_precedence: bool = True


class CounterfactualSpec(BaseModel):
    """Counterfactual scenario generation configuration."""

    model_config = {"frozen": True}

    enabled: bool = False
    max_scenarios_per_outcome: int = 3
    min_confidence: float = 0.3
    comparison_pool_size: int = 10


class SemanticRegistrySpec(BaseModel):
    """Entity resolution / semantic registry configuration."""

    model_config = {"frozen": True}

    enabled: bool = False
    entity_labels: list[str] = Field(default_factory=list)
    similarity_threshold: float = 0.85
    property_weight: float = 0.5
    structural_weight: float = 0.3
    behavioral_weight: float = 0.2
    comparison_properties: list[str] = Field(default_factory=list)
    max_candidates: int = 20


class CapabilitySpec(BaseModel):
    """W3-02: A named capability token in the domain spec.

    Each entry maps a set of actions to a list of allowed subjects (tenant IDs
    or ``"*"`` for wildcard). Compiled into a ``CapabilitySet`` at load time.

    Example YAML::

        capabilities:
          - name: match_read
            actions: [match:read]
            allowed_subjects: ["*"]
          - name: sync_write
            actions: [sync:write]
            allowed_subjects: [acme, globex]
          - name: admin_all
            actions: [admin:write, admin:kge]
            allowed_subjects: [acme]
    """

    name: str
    actions: list[str]
    allowed_subjects: list[str] = Field(default_factory=list)


class PluginsSpec(BaseModel):
    """Plugin extension points."""

    customgatetypes: list[str] = Field(default_factory=list)
    customcomputationtypes: list[str] = Field(default_factory=list)
    prematchhooks: list[str] = Field(default_factory=list)
    postmatchhooks: list[str] = Field(default_factory=list)
    syncvalidators: list[str] = Field(default_factory=list)
    # Names of registered inference rules (engine.inference_rule_registry) this
    # domain authorizes the admin `emit_inference_feedback` pathway to execute.
    # Empty means none: the generic engine never runs business inference a
    # domain did not declare.
    inference_rules: list[str] = Field(default_factory=list)
    # Per-rule parameters, keyed by rule name, for authorized rules whose
    # business vocabulary is the domain's (e.g. infer_buyer_persona's personas
    # and keywords). Validated at admission by
    # engine.inference_rule_registry.rule_parameter_defects.
    inference_rule_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SignalWeightSpec(BaseModel):
    """Spec for outcome-based signal weight learning."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    recalculation_cadence_days: int = 30
    min_outcomes_for_recalculation: int = 100
    baseline_weight: float = 1.0
    max_weight: float = 3.0
    min_weight: float = 0.1
    frequency_adjustment: bool = True
    confidence_dampening: bool = True
    penalty_threshold: float = 0.5
    penalty_factor: float = 0.3


class FeedbackLoopSpec(BaseModel):
    """Spec for the outcome feedback convergence loop."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    signal_weights: SignalWeightSpec = Field(default_factory=SignalWeightSpec)
    propagation_boost_factor: float = 1.15
    propagation_similarity_threshold: float = 0.4
    outcome_edge_type: str = "RESULTED_IN"
    # The domain's outcome node label. No engine default: a pack that writes or
    # reads outcomes declares it (engine.config.outcome_vocabulary; Stage 2
    # follow-up, outcome_vocabulary_required_enabled).
    outcome_node_label: str | None = None
    drift_threshold: float = 0.15
    drift_window_days: int = 30


class CausalEdgeSpec(BaseModel):
    """Domain spec declaration for causal edge types."""

    model_config = ConfigDict(frozen=True)

    edge_type: str
    source_label: str
    target_label: str
    required_properties: list[str] = Field(default_factory=list)
    temporal_validation: bool = True
    confidence_threshold: float = 0.0
    # SI-27: the source node's event-timestamp property. With
    # settings.causal_attribution_time_ordered on, first/last/position-based
    # attribution orders touchpoints of this source label by it.
    source_timestamp_property: str | None = None


class CausalSubgraphSpec(BaseModel):
    """Spec for causal subgraph configuration."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    causal_edges: list[CausalEdgeSpec] = Field(default_factory=list)
    attribution_enabled: bool = False
    counterfactual_enabled: bool = False
    temporal_decay_enabled: bool = False
    chain_depth_limit: int = 5


class ParetoObjectiveSpec(BaseModel):
    """Single objective dimension for Pareto optimization."""

    model_config = ConfigDict(frozen=True)

    dimension: str = Field(description="Scoring dimension name")
    direction: str = Field(default="maximize", description="maximize or minimize")
    weight_hint: float = Field(default=1.0, ge=0.0, le=1.0, description="Initial weight hint")

    @model_validator(mode="after")
    def refuse_unread_weight_hint(self) -> ParetoObjectiveSpec:
        """ND-DEAD: the Pareto front reads dimension and direction only (scoring_spec_strict_validation_enabled)."""
        if _scoring_strict() and not math.isclose(self.weight_hint, 1.0):
            msg = f"Pareto objective {self.dimension!r}: {_dead_option_defect(['weight_hint'])}"
            raise ValueError(msg)
        return self


class ParetoConfigSpec(BaseModel):
    """Pareto-specific configuration."""

    model_config = ConfigDict(frozen=True)

    objectives: list[ParetoObjectiveSpec] = Field(default_factory=list)
    front_size_limit: int = Field(default=50, ge=1, description="Max candidates on Pareto front")


class ArbitrationConstraintSpec(BaseModel):
    """Hard or soft constraint on a scoring dimension."""

    model_config = ConfigDict(frozen=True)

    dimension: str = Field(description="Scoring dimension to constrain")
    threshold: float = Field(ge=0.0, le=1.0, description="Minimum acceptable score")
    hard: bool = Field(default=False, description="If True, reject candidates below threshold")
    penalty: float = Field(default=0.5, ge=0.0, le=1.0, description="Soft penalty multiplier when below threshold")


class HardConstraint(BaseModel):
    """Hard constraint for DecisionPolicy — fails if not met."""

    model_config = ConfigDict(frozen=True)

    metric: str = Field(description="Input metric to evaluate (e.g. 'risk', 'compliance_pass')")
    operator: str = Field(description="Comparison operator: eq, lt, lte, gt, gte")
    value: bool | float | int | str = Field(description="Threshold value for comparison")


class PolicyWeights(BaseModel):
    """Weight distribution for composite score calculation."""

    model_config = ConfigDict(frozen=True)

    revenue: float = Field(ge=0.0, le=1.0)
    margin: float = Field(ge=0.0, le=1.0)
    risk: float = Field(ge=0.0, le=1.0)
    capacity: float = Field(ge=0.0, le=1.0)


class PolicyThresholds(BaseModel):
    """Decision thresholds for approve/reject/escalate outcomes."""

    model_config = ConfigDict(frozen=True)

    approve_threshold: float = Field(ge=0.0, le=1.0, description="Composite score >= this → approve")
    reject_threshold: float = Field(ge=0.0, le=1.0, description="Composite score < this → reject")
    conflict_tolerance: float = Field(ge=0.0, le=1.0, description="Signal spread > this → escalate")


class DecisionPolicy(BaseModel):
    """Decision policy for the ArbitrationEngine.

    Defines hard constraints, scoring weights, and decision thresholds
    used by engine/arbitration/engine.py to resolve approve/reject/defer/escalate.
    """

    model_config = ConfigDict(frozen=True)

    version: str = Field(description="Policy version identifier")
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    weights: PolicyWeights
    thresholds: PolicyThresholds


class DecisionArbitrationSpec(BaseModel):
    """Multi-objective decision arbitration configuration.

    When enabled, the match handler applies Pareto dominance filtering
    and constraint enforcement to the candidate set before returning results.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    optimization_method: str = Field(default="pareto", description="pareto or weighted_sum")
    pareto_config: ParetoConfigSpec = Field(default_factory=ParetoConfigSpec)
    constraints: list[ArbitrationConstraintSpec] = Field(default_factory=list)
    policy_weights: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Named weight policies, e.g. {'balanced': {'geo': 0.5, 'price': 0.5}}",
    )

    @model_validator(mode="after")
    def refuse_unread_options(self) -> DecisionArbitrationSpec:
        """ND-DEAD: optimization_method and policy_weights have no reader (scoring_spec_strict_validation_enabled)."""
        if not _scoring_strict():
            return self
        dead = [
            key
            for key, unset in (("optimization_method", "pareto"), ("policy_weights", {}))
            if getattr(self, key) != unset
        ]
        if dead:
            msg = f"decision_arbitration: {_dead_option_defect(dead)}"
            raise ValueError(msg)
        return self


class FeatureCatalogEntry(BaseModel):
    """Harvested feature taxonomy entry (PACK-026 → DomainSpec)."""

    model_config = ConfigDict(frozen=True)

    feature_id: str
    # The system that owns this feature's evidence. An identifier the domain
    # declares (e.g. the engine itself, an enrichment node, the business system
    # of record) — not a closed list the engine maintains. Payload admission
    # resolves `source_record.system` against the owners declared here plus
    # the core systems (engine.models.payloads.CORE_SOURCE_SYSTEMS).
    owner: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    scoring_dimension: str | None = None
    evidence_required: bool = True
    description: str | None = None


class RecommendedActionSpec(BaseModel):
    """Deterministic recommended next action for match explanations."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    when: Literal["missing_evidence", "failed_gate", "low_score", "ineligible"]
    message: str


class ExplanationCatalogSpec(BaseModel):
    """Explanation / recommended-action taxonomy (no tensor runtime)."""

    model_config = ConfigDict(frozen=True)

    version: str = "1.0.0"
    contribution_ordering: Literal["deterministic_feature_id", "score_desc"] = "deterministic_feature_id"
    recommended_actions: list[RecommendedActionSpec] = Field(default_factory=list)
    missing_evidence_codes: list[str] = Field(default_factory=list)


class OutcomeSignalSpec(BaseModel):
    """Business outcome signal used for feedback calibration."""

    model_config = ConfigDict(frozen=True)

    signal: str
    calibration_label: bool = True


class DomainActivation(StrEnum):
    """Serving posture a domain pack declares for itself.

    ``active`` packs are served as soon as they validate and their gates compile
    (``engine.config.loader.withholding_reason``). ``dormant`` packs are
    discoverable on disk but withheld until an operator sets
    ``DORMANT_DOMAIN_PACKS_ENABLED=true``. The posture belongs to the authorized
    domain owner's contract, so the engine never names a domain to withhold it.
    """

    ACTIVE = "active"
    DORMANT = "dormant"


class TemplateProvenance(BaseModel):
    """Which vertical template a customer spec was derived from (CEG-IDENT-01).

    Under the identity contract a tenant id is the spec id and the database name,
    so a vertical ("roofing-company") is never itself a tenant: a customer spec
    lives in ``domains/<tenant>/spec.yaml`` with ``domain.id == <tenant>`` and
    records the vertical it was cut from here. The engine reads this block for
    provenance only; it never resolves a tenant to a template at runtime.
    """

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: str | None = None


class DomainMetadata(BaseModel):
    """Domain pack metadata."""

    id: str
    name: str
    description: str | None = None
    version: str
    alternatedomains: list[str] = Field(default_factory=list)
    activation: DomainActivation = DomainActivation.ACTIVE
    # Required on customer specs by docs/contracts/DOMAIN_SPEC_VERSIONING.md;
    # absent on the shipped vertical packs, which are the templates themselves.
    template: TemplateProvenance | None = None

    @field_validator("version")
    @classmethod
    def validate_version_semver(cls, v: str) -> str:
        """RU-057 / CEG-COR-152: `{major}.{minor}.{patch}[-{stage}]` per DOMAIN_SPEC_VERSIONING.md.

        `latest`, `v2`, dates and other free-form strings were accepted. The
        stage, when present, is one of the contract's six production stages.
        Components are ASCII digits ([0-9]); a regex digit class would also accept Unicode decimals.
        """
        import re

        from engine.config.settings import settings

        if not settings.domain_version_semver_enforced:
            return v
        semver = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        stages = "seed|discovered|inferred|proposed|reviewed|production"
        if re.fullmatch(rf"{semver}(?:-(?:{stages}))?", v) is None:
            msg = (
                f"domain.version {v!r} is not semver: expected "
                f"'<major>.<minor>.<patch>' with an optional '-<stage>' suffix ({stages.replace('|', ', ')})"
            )
            raise ValueError(msg)
        return v


def missing_edge_property_reason(spec: DomainSpec, gate: GateSpec) -> str | None:
    """Reject a traversal predicate property the declared edge does not define.

    Sanitizing the name and compiling the comparison admits the pack, then Neo4j
    evaluates the missing relationship property as null and drops every candidate.
    """
    predicates = gate.edgepredicates or []
    if not predicates:
        return None
    edge_type = gate.edgetype or ""
    matched = [
        edge
        for edge in spec.ontology.edges
        if edge.type == edge_type
        and (gate.fromnode is None or edge.from_ == gate.fromnode)
        and (gate.tonode is None or edge.to == gate.tonode)
    ]
    if not matched:
        return "Gate " + gate.name + " edge predicate references unknown edge type " + edge_type
    if len(matched) > 1:
        return "Gate " + gate.name + " edge predicate matches more than one " + edge_type + " edge"
    declared = {prop.name: prop for prop in matched[0].properties}
    for predicate in predicates:
        prop = declared.get(predicate.property)
        if prop is None:
            return (
                "Gate "
                + gate.name
                + " edge predicate property "
                + predicate.property
                + " is not declared on edge "
                + edge_type
            )
        if predicate.present and prop.type is not PropertyType.STRING:
            return (
                "Gate "
                + gate.name
                + " edge predicate present on "
                + predicate.property
                + " requires a string property of edge "
                + edge_type
            )
    return None


def scoring_candidate_labels(spec: DomainSpec, direction: str) -> list[str]:
    """The candidate label(s) ``handle_match`` scores in ``direction``.

    ``handle_match`` takes the ``matchentities.candidate`` entries declared for
    the direction and matches ``(candidate:<first label>)``; a direction with
    none is refused there. The producer checks read the same selection
    (CEG-R01 / CEG-06), so what they validate is what the match reads.
    """
    return [entity.label for entity in spec.matchentities.candidate if entity.matchdirection == direction][:1]


def dimension_candidate_properties(spec: DomainSpec, dim: ScoringDimensionSpec) -> set[str]:
    """Candidate properties that ``dim`` can read in every match direction it applies to (CEG-R01).

    Under ``scoring_spec_strict_validation_enabled``: for each direction the
    dimension scores (``matchdirections``, else every declared direction), the
    properties declared on the candidate label ``handle_match`` scores there
    (``scoring_candidate_labels``); the result is their intersection, so a
    producer declared on another direction's candidate does not count. A
    direction with no candidate label contributes nothing (``handle_match``
    refuses it). Off: the legacy union over every candidate node.
    """
    if not _scoring_strict():
        labels = {entity.label for entity in spec.matchentities.candidate}
        return {p.name for n in spec.ontology.nodes if n.candidate or n.label in labels for p in n.properties}
    declared = {node.label: {p.name for p in node.properties} for node in spec.ontology.nodes}
    directions = dim.matchdirections or list(spec.queryschema.matchdirections)
    per_direction = [
        set.intersection(*(declared.get(label, set()) for label in labels))
        for labels in (scoring_candidate_labels(spec, direction) for direction in directions)
        if labels
    ]
    return set.intersection(*per_direction) if per_direction else set()


def _no_producer(spec: DomainSpec, dim: ScoringDimensionSpec) -> str | None:
    """ND-PARAM: a null / cold-start option whose input nothing produces (the FEATURE_GATES §16 producer rules).

    ``population_mean`` reads ``$_popmean_<dim>``, bound only when the pack
    declares it as a queryschema field or derived parameter; ``inherit_prior``
    reads ``candidate._prior_<dim>`` and ``structural_similarity`` reads
    ``candidate.shared_neighbor_score``, written by sync only when a candidate
    node declares them. Without a producer the option silently does nothing.
    """
    params = {f.name for f in spec.queryschema.fields} | {p.name for p in spec.derivedparameters}
    props = dimension_candidate_properties(spec, dim)
    needs: list[tuple[str, str, set[str], str]] = []
    if (
        dim.null_strategy is NullStrategy.POPULATION_MEAN
        or dim.cold_start_fallback is ColdStartFallback.POPULATION_MEAN
    ):
        needs.append(("population_mean", "_popmean_" + dim.name, params, "query parameter"))
    if dim.null_strategy is NullStrategy.INHERIT_PRIOR:
        needs.append(("inherit_prior", "_prior_" + dim.name, props, "candidate property"))
    if dim.cold_start_fallback is ColdStartFallback.STRUCTURAL_SIMILARITY:
        needs.append(("structural_similarity", "shared_neighbor_score", props, "candidate property"))
    for option, name, declared_names, kind in needs:
        if name not in declared_names:
            return (
                "Scoring dimension "
                + repr(dim.name)
                + ": "
                + option
                + " reads the "
                + kind
                + " "
                + repr(name)
                + ", which the pack does not declare"
            )
    return None


class DomainSpec(BaseModel):
    """Complete domain pack specification."""

    domain: DomainMetadata
    ontology: OntologySpec
    matchentities: MatchEntitiesSpec
    queryschema: QuerySchemaSpec
    traversal: TraversalSpec | None = None
    gates: list[GateSpec]
    scoring: ScoringSpec
    derivedparameters: list[DerivedParameterSpec] = Field(default_factory=list)
    softsignals: list[SoftSignalSpec] = Field(default_factory=list)
    sync: SyncSpec | None = None
    gdsjobs: list[GDSJobSpec] = Field(default_factory=list)
    kge: KGESpec | None = None
    compliance: ComplianceSpec | None = None
    capabilities: list[CapabilitySpec] = Field(default_factory=list)
    plugins: PluginsSpec | None = None
    calibration: CalibrationSpec | None = None
    feedbackloop: FeedbackLoopSpec = Field(default_factory=FeedbackLoopSpec)
    causal: CausalSubgraphSpec = Field(default_factory=CausalSubgraphSpec)
    counterfactual: CounterfactualSpec = Field(default_factory=CounterfactualSpec)
    semantic_registry: SemanticRegistrySpec = Field(default_factory=SemanticRegistrySpec)
    decision_arbitration: DecisionArbitrationSpec = Field(default_factory=DecisionArbitrationSpec)
    decision_policy: DecisionPolicy | None = None
    feature_catalog: list[FeatureCatalogEntry] = Field(default_factory=list)
    explanations: ExplanationCatalogSpec = Field(default_factory=ExplanationCatalogSpec)
    outcome_signals: list[OutcomeSignalSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cross_references(self) -> DomainSpec:
        """Validate cross-references between sections.

        W1-01 (seL4-inspired): Enforces global invariants across the domain spec,
        mirroring seL4's approach of verifying system-wide consistency rather than
        checking individual objects in isolation. Gated by DOMAIN_STRICT_VALIDATION.
        """
        from engine.config.settings import settings

        # Collect all node labels
        node_labels = {n.label for n in self.ontology.nodes}

        # Collect all node properties per label
        node_props: dict[str, set[str]] = {}
        for node in self.ontology.nodes:
            node_props[node.label] = {p.name for p in node.properties}

        # Collect all edge types
        edge_types = {e.type for e in self.ontology.edges}

        # Collect all query schema field names + derived param names (valid gate params)
        query_field_names = {f.name for f in self.queryschema.fields}
        derived_param_names = {p.name for p in self.derivedparameters}
        all_param_names = query_field_names | derived_param_names

        # Validate candidate/queryentity references
        for candidate in self.matchentities.candidate:
            if candidate.label not in node_labels:
                msg = f"Candidate label '{candidate.label}' not found in ontology"
                raise ValueError(msg)

        for qe in self.matchentities.queryentity:
            if qe.label not in node_labels:
                msg = f"Query entity label '{qe.label}' not found in ontology"
                raise ValueError(msg)

        # (a) W1-01: Every edge source/target type references a declared node
        if settings.domain_strict_validation:
            for edge in self.ontology.edges:
                if edge.from_ not in node_labels:
                    msg = f"Edge '{edge.type}' source '{edge.from_}' not found in ontology nodes"
                    raise ValueError(msg)
                if edge.to not in node_labels:
                    msg = f"Edge '{edge.type}' target '{edge.to}' not found in ontology nodes"
                    raise ValueError(msg)

        # RU-049 / CEG-COR-076: gate names are identities (compiled gate params,
        # composite subgate references); a second literal must not overwrite the first.
        if settings.gate_name_uniqueness:
            seen_gate_names: set[str] = set()
            duplicate_gate_names: set[str] = set()
            for gate in self.gates:
                if gate.name in seen_gate_names:
                    duplicate_gate_names.add(gate.name)
                seen_gate_names.add(gate.name)
            if duplicate_gate_names:
                msg = f"Duplicate gate name(s): {sorted(duplicate_gate_names)}"
                raise ValueError(msg)

        # Validate gate property references
        for gate in self.gates:
            predicate_message = missing_edge_property_reason(self, gate)
            if predicate_message is not None:
                raise ValueError(predicate_message)
            if gate.candidateprop and "." not in gate.candidateprop:
                # Check if any candidate node has this property
                found = False
                for candidate_ent in self.matchentities.candidate:
                    if candidate_ent.label in node_props and gate.candidateprop in node_props[candidate_ent.label]:
                        found = True
                        break
                if not found:
                    # Warning only, might be traversal alias
                    pass

            if gate.type == GateType.EXCLUSION and gate.edgetype:
                if gate.edgetype not in edge_types:
                    msg = f"Gate '{gate.name}' references unknown edge type '{gate.edgetype}'"
                    raise ValueError(msg)

            # (d) W1-01: No gate references an undeclared parameter. A literal
            # queryparam is an operand, not a reference, and is bound by the compiler.
            if settings.domain_strict_validation and isinstance(gate.queryparam, str) and all_param_names:
                if gate.queryparam not in all_param_names:
                    # Allow params that look like compound refs (e.g. with _min/_max suffixes)
                    base = gate.queryparam.removesuffix("_min").removesuffix("_max")
                    base = base.removesuffix("_start").removesuffix("_end")
                    if base not in all_param_names and gate.queryparam not in all_param_names:
                        if settings.domain_undeclared_param_rejected:
                            # RU-050 / CEG-COR-077: the temporalrange compiler derives
                            # `<q>_start` / `<q>_end` from a named queryparam; both
                            # declared makes the reference valid.
                            derived_declared = (
                                gate.type == GateType.TEMPORALRANGE
                                and f"{gate.queryparam}_start" in all_param_names
                                and f"{gate.queryparam}_end" in all_param_names
                            )
                            if not derived_declared:
                                msg = f"Gate '{gate.name}' references undeclared parameter '{gate.queryparam}'"
                                raise ValueError(msg)
                        else:
                            import logging

                            _logger = logging.getLogger(__name__)
                            _logger.warning(
                                "Gate '%s' references undeclared parameter '%s'",
                                gate.name,
                                gate.queryparam,
                            )

        # (c) W1-01: Scoring dimension default weights sum check (per match direction)
        if settings.domain_strict_validation and self.scoring and self.scoring.dimensions:
            weight_ceiling = 1.0
            tolerance = 1e-9
            buckets: dict[str, float] = {}
            for dim in self.scoring.dimensions:
                keys = dim.matchdirections or ["*"]
                for key in keys:
                    buckets[key] = buckets.get(key, 0.0) + dim.defaultweight
            if settings.weight_ceiling_includes_unscoped:
                # RU-044: an unscoped dimension scores in every direction, so each
                # declared direction (and any direction a dimension is scoped to)
                # carries the unscoped weight on top of its own.
                unscoped = buckets.get("*", 0.0)
                directions = [*self.queryschema.matchdirections, *(k for k in buckets if k != "*")]
                for direction in dict.fromkeys(directions):
                    buckets[direction] = buckets.get(direction, 0.0) + unscoped
            for key, weight_sum in buckets.items():
                if weight_sum > weight_ceiling + tolerance:
                    msg = (
                        f"Scoring dimension default weights sum to {weight_sum:.4f} "
                        f"for direction {key!r}, exceeding {weight_ceiling}"
                    )
                    raise ValueError(msg)

        # Validate GDS job references
        for job in self.gdsjobs:
            for label in job.projection.nodelabels:
                if label not in node_labels:
                    msg = f"GDS job '{job.name}' references unknown node label '{label}'"
                    raise ValueError(msg)
            for etype in job.projection.edgetypes:
                if etype not in edge_types:
                    msg = f"GDS job '{job.name}' references unknown edge type '{etype}'"
                    raise ValueError(msg)

        # S2-04: Ablation-backed aggregation strategy validation
        # HGKR Table 3 shows mixed strategies outperform uniform by +0.3-0.5% AUC
        if settings.domain_strict_validation and len(self.gdsjobs) > 1:
            strategies = {
                j.aggregation_strategy for j in self.gdsjobs if j.aggregation_strategy != AggregationStrategy.AUTO
            }
            if len(strategies) == 1 and strategies != set():
                import logging

                _agg_logger = logging.getLogger(__name__)
                _agg_logger.warning(
                    "All GDS jobs use uniform aggregation strategy '%s'. "
                    "HGKR research shows mixed strategies outperform uniform "
                    "by +0.3-0.5%% AUC. Consider using 'auto' or varying "
                    "strategies per edge category.",
                    strategies.pop(),
                )

        # Tenant association mapping, node ref namespaces and approved relevance:
        # unconditional, so a malformed declaration never loads (D-7).
        from engine.config.tenant_mapping import validate_tenant_mapping

        validate_tenant_mapping(self)

        return self

    @model_validator(mode="after")
    def validate_scoring_references(self) -> DomainSpec:
        """D3: scoring references the compiler cannot resolve (scoring_spec_strict_validation_enabled).

        An arbitration constraint naming an undeclared dimension constrains
        nothing; ``preference_attention`` matches from ``qe``, so a pack whose
        traversal binds no ``qe`` node fails every match that scores it.
        """
        if not _scoring_strict():
            return self
        declared = {dim.name for dim in self.scoring.dimensions}
        unknown = sorted({c.dimension for c in self.decision_arbitration.constraints if c.dimension not in declared})
        if unknown:
            msg = f"decision_arbitration constraint(s) name undeclared scoring dimension(s): {unknown}"
            raise ValueError(msg)
        for direction in self.queryschema.matchdirections:
            attention = [
                d.name
                for d in self.scoring.dimensions
                if d.computation is ComputationType.PREFERENCEATTENTION
                and (not d.matchdirections or direction in d.matchdirections)
            ]
            if attention and not _traversal_binds(self.traversal, "qe", direction):
                msg = (
                    f"Scoring dimension(s) {attention} use preference_attention, which matches from `qe`, "
                    f"but no traversal step applicable to direction {direction!r} binds a `qe` node"
                )
                raise ValueError(msg)
        unproduced = [defect for dim in self.scoring.dimensions if (defect := _no_producer(self, dim)) is not None]
        if unproduced:
            raise ValueError("; ".join(unproduced))
        return self
