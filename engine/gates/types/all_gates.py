"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [gates, types, implementation]
owner: engine-team
status: active
--- /L9_META ---

All 10 gate type implementations in one file.
Production-grade, enterprise-quality, frontier AI lab standard.
"""

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from engine.config.schema import DomainSpec, GateSpec
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

_PARAM_KEY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")
_MAX_PARAM_KEY_LEN = 64

# C-009: spec tokens interpolated verbatim pass through a literal allow-list —
# a lookup raises on anything else — so an operator or combinator read from
# untrusted YAML can never carry Cypher.
_OPERATORS: dict[str, str] = {
    op: op for op in (">=", "<=", ">", "<", "=", "!=", "<>", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH")
}
_LOGIC: dict[str, str] = {"AND": "AND", "OR": "OR"}


# ============================================================================
# BASE GATE
# ============================================================================


class BaseGate(ABC):
    """Abstract base class for all gate types."""

    def __init__(self, spec: GateSpec, domain_spec: DomainSpec):
        """
        Initialize gate.

        Args:
            spec: Gate specification
            domain_spec: Full domain specification
        """
        self.spec = spec
        self.domain_spec = domain_spec
        # C-009: data values a gate needs at run time never appear in the
        # compiled fragment as literals. compile() registers them here under
        # the parameter names the fragment references, and the caller merges
        # `query_params` into the execute_query parameters.
        self._query_params: dict[str, Any] = {}

    @abstractmethod
    def compile(self) -> str:
        """
        Compile gate into Cypher WHERE clause fragment.

        Returns:
            Cypher clause (without NULL handling)
        """

    @property
    def query_params(self) -> dict[str, Any]:
        """Cypher parameters registered by the most recent compile() call."""
        return dict(self._query_params)

    def _bind_param(self, suffix: str, value: Any) -> str:
        """Register ``value`` as a Cypher parameter and return its ``$name`` reference.

        The name is derived from the gate name so fragments from different gates
        never collide; both parts are reduced to ``[A-Za-z0-9_]`` so the name is
        always a valid Cypher parameter identifier.
        """
        safe_gate = _PARAM_KEY_UNSAFE_RE.sub("_", self.spec.name)
        safe_suffix = _PARAM_KEY_UNSAFE_RE.sub("_", suffix)
        raw_key = f"gate_{safe_gate}_{safe_suffix}"
        if len(raw_key) > _MAX_PARAM_KEY_LEN:
            # Keep the key an identifier of bounded length; the digest keeps it unique per gate.
            digest = hashlib.sha256(safe_gate.encode()).hexdigest()[:16]
            raw_key = f"gate_{digest}_{safe_suffix}"[:_MAX_PARAM_KEY_LEN]
        key = sanitize_label(raw_key)
        self._query_params[key] = value
        return f"${key}"

    def _prop_ref(self, prop: str) -> str:
        """Format property reference (property name validated as an identifier)."""
        name = sanitize_label(prop.removeprefix("candidate."))
        return f"candidate.{name}"

    def _param_ref(self, param: str) -> str:
        """Format query parameter reference (parameter path validated as identifiers)."""
        parts = param.removeprefix("$").split(".")
        if parts[0] != "query":
            parts.insert(0, "query")
        return "$" + ".".join(sanitize_label(part) for part in parts)


# ============================================================================
# 1. RANGE GATE
# ============================================================================


class RangeGate(BaseGate):
    """Candidate property falls within [min, max] from query."""

    def compile(self) -> str:
        """
        Example: candidate.creditscore >= $query.mincreditscore
                 AND candidate.creditscore <= $query.maxcreditscore
        """
        if not self.spec.candidateprop:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop required for range gate")
        if not self.spec.queryparam_min or not self.spec.queryparam_max:
            raise ValueError(f"Gate '{self.spec.name}': queryparam_min and queryparam_max required")

        prop = self._prop_ref(self.spec.candidateprop)
        min_param = self._param_ref(self.spec.queryparam_min)
        max_param = self._param_ref(self.spec.queryparam_max)

        return f"{prop} >= {min_param} AND {prop} <= {max_param}"


# ============================================================================
# 2. THRESHOLD GATE
# ============================================================================


class ThresholdGate(BaseGate):
    """Candidate property meets minimum/maximum threshold."""

    def compile(self) -> str:
        """
        Example: candidate.creditscore >= $query.mincreditscore
        """
        if not self.spec.candidateprop:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop required")
        if not self.spec.queryparam:
            raise ValueError(f"Gate '{self.spec.name}': queryparam required")
        if not self.spec.operator:
            raise ValueError(f"Gate '{self.spec.name}': operator required (>=, <=, >, <, =)")

        prop = self._prop_ref(self.spec.candidateprop)
        param = self._param_ref(self.spec.queryparam)
        operator = _OPERATORS[self.spec.operator]

        return f"{prop} {operator} {param}"


# ============================================================================
# 3. BOOLEAN GATE
# ============================================================================


class BooleanGate(BaseGate):
    """Boolean flag match (true/false or presence)."""

    def compile(self) -> str:
        """
        Example: candidate.vaeligible = $query.vaeligible
        """
        if not self.spec.candidateprop:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop required")
        if not self.spec.queryparam:
            raise ValueError(f"Gate '{self.spec.name}': queryparam required")

        prop = self._prop_ref(self.spec.candidateprop)
        param = self._param_ref(self.spec.queryparam)

        return f"{prop} = {param}"


# ============================================================================
# 4. COMPOSITE GATE
# ============================================================================


class CompositeGate(BaseGate):
    """Logical combination of multiple sub-gates (AND/OR)."""

    def compile(self) -> str:
        """
        Example: (gate1 AND gate2) OR gate3
        """
        if not self.spec.subgates:
            raise ValueError(f"Gate '{self.spec.name}': subgates required")
        if not self.spec.logic:
            raise ValueError(f"Gate '{self.spec.name}': logic required (AND/OR)")

        self._query_params = {}
        # Find subgate specs by name
        subgate_clauses = []
        for subgate_name in self.spec.subgates:
            subgate_spec = next((g for g in self.domain_spec.gates if g.name == subgate_name), None)
            if not subgate_spec:
                raise ValueError(f"Subgate '{subgate_name}' not found in domain spec")

            # Recursively compile subgate
            from engine.gates.registry import GateRegistry

            gate_class = GateRegistry.get_gate_class(subgate_spec.type)
            gate_instance = gate_class(subgate_spec, self.domain_spec)
            subgate_clauses.append(f"({gate_instance.compile()})")
            self._query_params.update(gate_instance.query_params)

        logic_op = f" {_LOGIC[self.spec.logic.upper()]} "
        return logic_op.join(subgate_clauses)


# ============================================================================
# 5. ENUMMAP GATE
# ============================================================================


class EnumMapGate(BaseGate):
    """Query enum value maps to candidate's allowed set."""

    def compile(self) -> str:
        """
        Example: $query.propertytype IN candidate.allowedpropertytypes
        """
        if not self.spec.candidateprop:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop required")
        if not self.spec.queryparam:
            raise ValueError(f"Gate '{self.spec.name}': queryparam required")

        prop = self._prop_ref(self.spec.candidateprop)
        param = self._param_ref(self.spec.queryparam)
        self._query_params = {}

        # Check if mapping is provided (query value → candidate values)
        if self.spec.mapping:
            # Build CASE WHEN for complex mapping. Mapping keys and values are
            # data compared against properties, not labels: they may contain
            # spaces, dashes or anything else, so they travel as $parameters
            # (C-009) rather than as quoted literals in the fragment.
            cases = []
            for index, (query_val, candidate_vals) in enumerate(self.spec.mapping.items()):
                key_ref = self._bind_param(f"key_{index}", query_val)
                values_ref = self._bind_param(f"values_{index}", list(candidate_vals))
                cases.append(f"WHEN {param} = {key_ref} THEN {prop} IN {values_ref}")

            case_expr = " ".join(cases)
            return f"CASE {case_expr} ELSE false END"
        # Simple membership check
        return f"{param} IN {prop}"


# ============================================================================
# 6. EXCLUSION GATE
# ============================================================================


class ExclusionGate(BaseGate):
    """Blocks matches based on exclusion edges (e.g., BLACKLISTED)."""

    def compile(self) -> str:
        """
        Example: NOT EXISTS((query)-[:BLACKLISTEDLENDER]->(candidate))
        """
        if not self.spec.edgetype:
            raise ValueError(f"Gate '{self.spec.name}': edgetype required")

        # Relationship type and node variables are structural identifiers
        # read straight from the domain spec, which is untrusted input:
        # sanitize before interpolation (C-009).
        edge = sanitize_label(self.spec.edgetype)
        from_node = sanitize_label(self.spec.fromnode or "query")
        to_node = sanitize_label(self.spec.tonode or "candidate")

        return f"NOT EXISTS(({from_node})-[:{edge}]->({to_node}))"


# ============================================================================
# 7. SELFRANGE GATE
# ============================================================================


class SelfRangeGate(BaseGate):
    """Candidate's own property range contains query value."""

    def compile(self) -> str:
        """
        Example: $query.mfi >= candidate.mfi_min AND $query.mfi <= candidate.mfi_max
        """
        if not self.spec.candidateprop_min or not self.spec.candidateprop_max:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop_min and candidateprop_max required")
        if not self.spec.queryparam:
            raise ValueError(f"Gate '{self.spec.name}': queryparam required")

        min_prop = self._prop_ref(self.spec.candidateprop_min)
        max_prop = self._prop_ref(self.spec.candidateprop_max)
        param = self._param_ref(self.spec.queryparam)

        return f"{param} >= {min_prop} AND {param} <= {max_prop}"


# ============================================================================
# 8. FRESHNESS GATE
# ============================================================================


class FreshnessGate(BaseGate):
    """Candidate data must be fresher than threshold."""

    def compile(self) -> str:
        """
        Example: duration.between(candidate.ratesheetdate, datetime()).days <= 7
        """
        if not self.spec.candidateprop:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop (timestamp) required")
        if not self.spec.maxagedays:
            raise ValueError(f"Gate '{self.spec.name}': maxagedays required")

        prop = self._prop_ref(self.spec.candidateprop)
        max_age = int(self.spec.maxagedays)

        return f"duration.between({prop}, datetime()).days <= {max_age}"


# ============================================================================
# 9. TEMPORALRANGE GATE
# ============================================================================


class TemporalRangeGate(BaseGate):
    """Candidate's temporal window overlaps with query window."""

    def compile(self) -> str:
        """
        Example: candidate.availablestart <= $query.needbydate
                 AND candidate.availableend >= $query.needbydate
        """
        if not self.spec.candidateprop_start or not self.spec.candidateprop_end:
            raise ValueError(f"Gate '{self.spec.name}': candidateprop_start and candidateprop_end required")
        if not self.spec.queryparam_start or not self.spec.queryparam_end:
            raise ValueError(f"Gate '{self.spec.name}': queryparam_start and queryparam_end required")

        cand_start = self._prop_ref(self.spec.candidateprop_start)
        cand_end = self._prop_ref(self.spec.candidateprop_end)
        query_start = self._param_ref(self.spec.queryparam_start)
        query_end = self._param_ref(self.spec.queryparam_end)

        return f"{cand_start} <= {query_end} AND {cand_end} >= {query_start}"


# ============================================================================
# 10. TRAVERSAL GATE
# ============================================================================


class TraversalGate(BaseGate):
    """Gate checks condition on a traversed relationship or node."""

    def compile(self) -> str:
        """
        Example: EXISTS { MATCH (candidate)-[:OPERATESIN]->(s:State)
                          WHERE s.statecode = $query.propertystate }
        """
        if not self.spec.pattern:
            raise ValueError(f"Gate '{self.spec.name}': pattern required")
        if not self.spec.condition:
            raise ValueError(f"Gate '{self.spec.name}': condition required")

        # `pattern` and `condition` are spec-authored Cypher by design — the
        # traversal gate is the domain spec's escape hatch and has no
        # value-level grammar to validate against. The live GateCompiler
        # (engine/gates/compiler.py) does not honour these fields; packs that
        # rely on them are withheld behind `unvalidated_domain_packs_enabled`
        # (CEG-009). The waiver below keeps the scanner honest about that.
        pattern = self.spec.pattern
        condition = self.spec.condition

        return f"EXISTS {{ MATCH {pattern} WHERE {condition} }}"  # cypher-lint: allow spec-authored Cypher escape hatch, withheld by CEG-009


# ============================================================================
# EXPORT ALL GATE CLASSES
# ============================================================================

__all__ = [
    "BaseGate",
    "BooleanGate",
    "CompositeGate",
    "EnumMapGate",
    "ExclusionGate",
    "FreshnessGate",
    "RangeGate",
    "SelfRangeGate",
    "TemporalRangeGate",
    "ThresholdGate",
    "TraversalGate",
]
