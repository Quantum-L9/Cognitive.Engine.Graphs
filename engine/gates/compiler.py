"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [gates, compiler, cypher]
owner: engine-team
status: active
--- /L9_META ---

engine/gates/compiler.py
Compile gate definitions from domain spec YAML into executable Cypher WHERE clauses.
Handles all 10 gate types, null semantics, role exemptions, and direction filtering.

Exports: GateCompiler, bind_param_key
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Callable
from typing import Any

from engine.config.schema import (
    DomainSpec,
    GateSpec,
    GateType,
    NullBehavior,
)
from engine.gates.null_semantics import NullHandler
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

# Cypher boolean conjunction used to join WHERE fragments.
AND_JOINER = " AND "

# C-009: tokens read from the domain spec that are interpolated verbatim pass
# through a literal allow-list (a lookup raises on anything else) or through
# sanitize_label(); query parameter *names* are identifiers and must be
# validated exactly like labels, or `$name` becomes a statement injection.
_OPERATORS: dict[str, str] = {
    op: op for op in (">=", "<=", ">", "<", "=", "!=", "<>", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH")
}
_LOGIC: dict[str, str] = {"AND": "AND", "OR": "OR"}

# ── Generated parameter names (C-009) ─────────────────────
#
# A value a gate needs at run time never appears in the compiled fragment as a
# literal. The compiler registers it under a generated, validated parameter
# name and the caller merges `last_query_params` into the execute_query
# parameters. `engine/gates/types/all_gates.py` binds through the same key
# derivation so the two compilers can never collide on a name.
_PARAM_KEY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")
_MAX_PARAM_KEY_LEN = 64

# CEG-009b: a spec-authored scalar operand (`queryparam: 85.0`, `queryparam:
# true`). Anything else that is not a declared parameter name is refused.
LiteralOperand = bool | int | float


def bind_param_key(gate_name: str, suffix: str) -> str:
    """Validated Cypher parameter identifier for a value a gate binds itself.

    The name is derived from the gate name so fragments from different gates
    never collide; both parts are reduced to ``[A-Za-z0-9_]`` so the name is
    always a valid Cypher parameter identifier, and ``sanitize_label`` is the
    final word (C-009).
    """
    safe_gate = _PARAM_KEY_UNSAFE_RE.sub("_", gate_name)
    safe_suffix = _PARAM_KEY_UNSAFE_RE.sub("_", suffix)
    raw_key = f"gate_{safe_gate}_{safe_suffix}"
    if safe_gate != gate_name or len(raw_key) > _MAX_PARAM_KEY_LEN:
        # Normalisation is lossy — "risk-a" and "risk_a" both become "risk_a" —
        # so a name it changed, or one too long to keep whole, is identified by
        # a digest of the ORIGINAL name instead. Distinct names therefore never
        # share a key (hashing the normalised form would collide the same way).
        digest = hashlib.sha256(gate_name.encode()).hexdigest()[:16]
        raw_key = f"gate_{digest}_{safe_suffix}"
        if len(raw_key) > _MAX_PARAM_KEY_LEN:
            suffix_digest = hashlib.sha256(suffix.encode()).hexdigest()[:16]
            raw_key = f"gate_{digest}_{suffix_digest}"
    return sanitize_label(raw_key)


class _Operand:
    """A value a compiled predicate tests, for null handling (RU-042).

    ``guarded`` marks an operand the predicate text already lets impose no
    constraint when NULL (a per-side ``x IS NULL OR …``), so a PASS wrap need
    not repeat it; a FAIL wrap still rejects it.
    """

    __slots__ = ("guarded", "ref")

    def __init__(self, ref: str, *, guarded: bool = False) -> None:
        self.ref = ref
        self.guarded = guarded


class _Predicate(str):
    """A compiled predicate that also carries the operands it tests (RU-042)."""

    operands: tuple[_Operand, ...]

    def __new__(cls, text: str, operands: list[_Operand] | tuple[_Operand, ...] = ()) -> _Predicate:
        obj = super().__new__(cls, text)
        obj.operands = tuple(operands)
        return obj


def _operands_from_predicate() -> bool:
    from engine.config.settings import settings

    return settings.gate_null_operands_from_predicate


def _param_name(name: str | None, fallback: str = "value") -> str:
    """Validated Cypher parameter identifier for a gate's query parameter."""
    return sanitize_label(name or fallback)


def _declared(gate: GateSpec, field: str) -> str | None:
    """A gate field that is a non-empty string; anything else counts as absent.

    Read with a default so a partially specified gate (a test double built from
    the model's public surface) compiles the same as a real `GateSpec` whose
    optional field is None.
    """
    value = getattr(gate, field, None)
    if isinstance(value, str) and value.strip():
        return value
    return None


class UnboundGateParameterError(ValueError):
    """RU-010: an active gate's undeclared or required parameter resolved to null."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        msg = "Gate parameter resolved to null: " + "; ".join(problems)
        super().__init__(msg)


class GateCompiler:
    """
    Compiles GateSpec objects from a domain spec into Cypher WHERE clause fragments.

    Each gate type maps to a specific Cypher predicate pattern:
        boolean   → candidate.prop = $param
        threshold → candidate.prop >= $param (or <=, configurable)
        range     → $param_min <= candidate.prop <= $param_max  (or candidate range check)
        enum      → candidate.prop IN $param_list
        exclusion → NOT (candidate)-[:<declared edgetype>]->(query)
        composite → (gate1 AND gate2) OR gate3
        self_range→ candidate.min_prop <= $param AND $param <= candidate.max_prop
        freshness → candidate.prop >= datetime() - duration({days: $days})
        temporal_range → candidate.prop >= $start AND candidate.prop <= $end
        traversal → EXISTS { MATCH (candidate)-[:EDGE]->(t:Target) WHERE t.prop = $param }

    A gate whose `queryparam` is a scalar (`85.0`, `true`) compiles against a
    generated parameter instead of a parameter *name*; the value is exposed on
    `last_query_params` and must be merged into the execute_query parameters
    together with the resolved query (CEG-009b).

    Usage:
        compiler = GateCompiler(domain_spec)
        where_clause = compiler.compile_all_gates(domain_spec.queryschema.matchdirections[0])
        parameters = {**resolved_query, **compiler.last_query_params}
        single = compiler.compile(gate_spec)
    """

    def __init__(self, domain_spec: DomainSpec):
        self.domain_spec = domain_spec
        self._gates: list[GateSpec] = domain_spec.gates if domain_spec.gates else []
        self._compliance = getattr(domain_spec, "compliance", None)
        # C-009: literal operands bound during compilation, keyed by generated name,
        # and which (gate, suffix) owns each key so two gates can never share one.
        self._query_params: dict[str, Any] = {}
        self._bound_by: dict[str, tuple[str, str]] = {}
        # CEG-009c: composite gates currently being expanded (cycle guard).
        self._composite_stack: list[str] = []
        # RU-043: the direction compile_all_gates / compile_relaxed is compiling
        # (None for a direct compile()), checked against composite subgates.
        self._direction: str | None = None

    # ── W1-03: Gate validation (seL4-inspired) ──────────────

    def validate_gates(
        self,
        resolved_params: dict[str, object] | None = None,
        *,
        match_direction: str | None = None,
        reject_unbound: bool = False,
    ) -> list[str]:
        """W1-03: Pre-compilation gate validation.

        Checks:
        (a) Every required gate field has a corresponding parameter in the resolved set.
        (b) No gate uses an operator incompatible with the field's declared type.
        (c) Post-compilation: if a resolved parameter is None, log WARNING
            (STRICT_NULL_GATES) and, with ``reject_unbound``, reject it when the
            parameter is undeclared or a required queryschema field.

        A literal `queryparam` is bound by the compiler itself and therefore
        never looked up in the resolved set.

        RU-010 / CEG-COR-023 (R1a-DEC-03, third alternative): ``match_direction``
        restricts the check to the gates active for that direction (the same
        filter compile_all_gates applies). ``reject_unbound`` raises
        UnboundGateParameterError for every active gate whose parameter is
        missing or null and is either not declared (neither a queryschema field
        nor a derived parameter) or a ``required`` queryschema field; a null
        declared optional parameter is left to the gate's ``nullbehavior`` in
        the compiled Cypher (C-14) and only warned about.

        Returns list of warning messages (empty = all clear).
        """
        from engine.config.settings import settings

        warnings: list[str] = []
        unbound: list[str] = []
        field_required = {f.name: f.required for f in self.domain_spec.queryschema.fields}
        declared = set(field_required) | {p.name for p in self.domain_spec.derivedparameters}
        # Build a property type index from ontology
        prop_types: dict[str, str] = {}
        for node in self.domain_spec.ontology.nodes:
            for prop in node.properties:
                prop_types[prop.name] = prop.type.value

        # Operator-type compatibility matrix
        numeric_operators = frozenset({">=", "<=", ">", "<"})
        numeric_types = frozenset({"int", "float", "datetime", "date", "duration"})

        for gate in self._gates:
            if match_direction is not None and gate.matchdirections and match_direction not in gate.matchdirections:
                continue
            named_param = gate.queryparam if isinstance(gate.queryparam, str) else None

            # (a) Check that required gate params exist in resolved set
            if resolved_params is not None and named_param:
                if named_param not in resolved_params:
                    msg = f"Gate '{gate.name}': parameter '${named_param}' not in resolved parameters"
                    warnings.append(msg)
                    if settings.strict_null_gates:
                        logger.warning(msg)

            # (b) Operator-type compatibility
            if gate.operator and gate.candidateprop:
                prop_type = prop_types.get(gate.candidateprop, "")
                if gate.operator in numeric_operators and prop_type and prop_type not in numeric_types:
                    msg = (
                        f"Gate '{gate.name}': operator '{gate.operator}' incompatible with property type '{prop_type}'"
                    )
                    warnings.append(msg)
                    logger.warning(msg)

            # (c) Null-param post-check
            if resolved_params is not None and named_param:
                val = resolved_params.get(named_param)
                if val is None:
                    msg = f"Gate '{gate.name}': parameter '${named_param}' resolved to null"
                    if settings.strict_null_gates:
                        warnings.append(msg)
                        logger.warning(msg)
                    if reject_unbound and (named_param not in declared or field_required.get(named_param, False)):
                        kind = "required" if named_param in declared else "undeclared"
                        unbound.append(f"gate '{gate.name}' needs {kind} parameter '{named_param}'")

        if unbound:
            raise UnboundGateParameterError(unbound)
        return warnings

    # ── Public API ─────────────────────────────────────────

    @property
    def last_query_params(self) -> dict[str, Any]:
        """Parameters the compiler bound itself during the most recent compilation.

        Reset by `compile_all_gates` / `compile_relaxed`; `compile` accumulates so a
        composite's subgates land in the same map. Merge into the execute_query
        parameters alongside the resolved query (C-009).
        """
        return dict(self._query_params)

    def compile(self, gate: GateSpec) -> str:
        """
        Compile a single GateSpec into a Cypher WHERE fragment.

        Returns a string like:
            "candidate.min_density <= $density AND $density <= candidate.max_density"

        The fragment carries the gate's null semantics. Composite gates
        compile their subgates through this method, so it applies to a
        subgate exactly as it would to the gate on its own.
        """
        handler = self._get_handler(gate.type)
        raw_predicate = handler(gate)
        return self._wrap_null_semantics(gate, raw_predicate)

    def compile_all_gates(
        self,
        match_direction: str,
        role: str | None = None,
    ) -> str:
        """
        Compile all gates into a combined WHERE clause.

        Args:
            match_direction: Current match direction (one of the domain's queryschema.matchdirections)
            role: Candidate role for gate exemption checking (roleexempt)

        Returns:
            Combined Cypher WHERE clause (without the WHERE keyword)
        """
        self._query_params = {}
        self._bound_by = {}
        fragments: list[str] = []
        composed = self._composed_subgates(match_direction)

        for gate in self._gates:
            # Direction filter
            if gate.matchdirections and match_direction not in gate.matchdirections:
                continue
            # RU-043: a subgate contributes only through its composite.
            if gate.name in composed:
                continue

            # Role exemption
            if role and gate.roleexempt and role in gate.roleexempt:
                logger.debug(f"Gate {gate.name} exempted for role={role}")
                continue

            fragment = self._compile_for(gate, match_direction)
            if fragment:
                fragments.append(f"({fragment})")

        if not fragments:
            return "true"

        return AND_JOINER.join(fragments)

    def compile_relaxed(
        self,
        match_direction: str,
        role: str | None = None,
    ) -> str:
        """
        Compile gates for relaxed matching.
        Non-critical gates become score penalties instead of hard filters.
        Only gates with relaxedpenalty == 0.0 are included as hard WHERE.
        """
        self._query_params = {}
        self._bound_by = {}
        hard_fragments: list[str] = []
        composed = self._composed_subgates(match_direction)

        for gate in self._gates:
            if gate.matchdirections and match_direction not in gate.matchdirections:
                continue
            if gate.name in composed:
                continue
            if role and gate.roleexempt and role in gate.roleexempt:
                continue

            if math.isclose(gate.relaxedpenalty, 0.0, abs_tol=1e-9):
                fragment = self._compile_for(gate, match_direction)
                if fragment:
                    hard_fragments.append(f"({fragment})")

        if not hard_fragments:
            return "true"

        return AND_JOINER.join(hard_fragments)

    def _composed_subgates(self, match_direction: str) -> set[str]:
        """Names referenced as subgates by a composite applicable to ``match_direction`` (RU-043).

        Before, a subgate was also compiled as a top-level filter, so an OR
        composite degenerated to the AND of its subgates.
        """
        from engine.config.settings import settings

        if not settings.composite_subgate_scoping:
            return set()
        names: set[str] = set()
        for gate in self._gates:
            if gate.type != GateType.COMPOSITE or not gate.subgates:
                continue
            if gate.matchdirections and match_direction not in gate.matchdirections:
                continue
            names.update(gate.subgates)
        return names

    def _compile_for(self, gate: GateSpec, match_direction: str) -> str:
        """``compile`` with the direction recorded so composites can check their subgates' scope."""
        previous = self._direction
        self._direction = match_direction
        try:
            return self.compile(gate)
        finally:
            self._direction = previous

    # ── Parameter binding (C-009 / CEG-009b) ────────────────

    def _bind(self, gate: GateSpec, suffix: str, value: LiteralOperand | str | list[Any]) -> str:
        """Register ``value`` under a generated name and return its ``$`` reference.

        Defence in depth behind ``bind_param_key``: a key already owned by a
        different (gate, suffix) is refused rather than silently overwritten,
        which would make one predicate evaluate against another gate's operand.
        The same gate re-binding the same suffix (a gate compiled twice in one
        accumulation) is idempotent.
        """
        key = bind_param_key(gate.name, suffix)
        owner = (gate.name, suffix)
        previous = self._bound_by.get(key)
        if previous is not None and previous != owner:
            msg = (
                f"Gate '{gate.name}' and gate '{previous[0]}' both bind the Cypher parameter '${key}' "
                f"(suffixes {suffix!r} / {previous[1]!r}); rename one gate so their operands cannot collide"
            )
            raise ValueError(msg)
        self._bound_by[key] = owner
        self._query_params[key] = value
        return f"${key}"

    def _operand(self, gate: GateSpec) -> str:
        """``$`` reference for the gate's `queryparam`.

        A string is a declared parameter *name* and is validated like a label.
        A scalar is a literal operand (`queryparam: 85.0`): it is bound under a
        generated name so the fragment references `$gate_<name>_value`, never
        `$85.0` or `$True`.
        """
        value = gate.queryparam
        if value is None:
            msg = f"Gate '{gate.name}': a {gate.type.value} gate requires queryparam"
            raise ValueError(msg)
        if isinstance(value, str):
            return f"${sanitize_label(value)}"
        return self._bind(gate, "value", value)

    def _named_param(self, gate: GateSpec) -> str | None:
        """The declared parameter name behind `queryparam`, or None for a literal / absent one."""
        return gate.queryparam if isinstance(gate.queryparam, str) else None

    def _param_operands(self, gate: GateSpec, *, guarded: bool = False) -> list[_Operand]:
        """The declared `queryparam` as a null-checkable operand; a literal is never NULL."""
        named = self._named_param(gate)
        return [_Operand(f"${_param_name(named)}", guarded=guarded)] if named else []

    # ── Gate Type Handlers ─────────────────────────────────

    def _get_handler(self, gate_type: GateType) -> Callable[[GateSpec], str]:
        """Route to the correct handler for each gate type."""
        handlers = {
            GateType.BOOLEAN: self._compile_boolean,
            GateType.THRESHOLD: self._compile_threshold,
            GateType.RANGE: self._compile_range,
            GateType.ENUMMAP: self._compile_enum,
            GateType.EXCLUSION: self._compile_exclusion,
            GateType.COMPOSITE: self._compile_composite,
            GateType.SELFRANGE: self._compile_self_range,
            GateType.FRESHNESS: self._compile_freshness,
            GateType.TEMPORALRANGE: self._compile_temporal_range,
            GateType.TRAVERSAL: self._compile_traversal,
        }
        handler = handlers.get(gate_type)
        if not handler:
            logger.warning(f"Unknown gate type: {gate_type}, defaulting to passthrough")
            return lambda g: "true"
        return handler

    def _compile_boolean(self, gate: GateSpec) -> str:
        """Boolean gate: candidate.prop = $param OR candidate.prop = true."""
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        tested = [_Operand(f"candidate.{prop}"), *self._param_operands(gate)]
        if gate.queryparam is not None:
            return _Predicate(f"candidate.{prop} = {self._operand(gate)}", tested)
        return _Predicate(f"candidate.{prop} = true", tested)

    def _compile_threshold(self, gate: GateSpec) -> str:
        """
        Threshold gate: candidate.prop >= $param (default).
        Supports operator override via gate.operator: >=, <=, >, <, =
        """
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        op = _OPERATORS[gate.operator or ">="]
        tested = [_Operand(f"candidate.{prop}"), *self._param_operands(gate)]
        return _Predicate(f"candidate.{prop} {op} {self._operand(gate)}", tested)

    def _compile_range(self, gate: GateSpec) -> str:
        """
        Range gate, in one of two declared shapes:

        * query window — `queryparam_min` / `queryparam_max` name two parameters
          and the candidate's single property must fall between them:
          candidate.prop >= $min AND candidate.prop <= $max
        * candidate window — the query value falls within the candidate's own
          min/max properties (`candidateprop_min` / `candidateprop_max`, or
          `min_<prop>` / `max_<prop>`):
          candidate.min_prop <= $param AND $param <= candidate.max_prop

        Before `gate_declared_bounds_enabled`, `queryparam_min` / `queryparam_max`
        were declared by the schema and ignored here, so a query-window gate
        compiled against the placeholder parameter `$value`.
        """
        from engine.config.settings import settings

        base_prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        lower = _declared(gate, "queryparam_min")
        upper = _declared(gate, "queryparam_max")
        if settings.gate_declared_bounds_enabled and lower and upper:
            lo = sanitize_label(lower)
            hi = sanitize_label(upper)
            if _operands_from_predicate():
                # Each NULL query bound imposes no constraint on its own side.
                return _Predicate(
                    f"(${lo} IS NULL OR candidate.{base_prop} >= ${lo}) AND "
                    f"(${hi} IS NULL OR candidate.{base_prop} <= ${hi})",
                    [
                        _Operand(f"candidate.{base_prop}"),
                        _Operand(f"${lo}", guarded=True),
                        _Operand(f"${hi}", guarded=True),
                    ],
                )
            return f"candidate.{base_prop} >= ${lo} AND candidate.{base_prop} <= ${hi}"

        min_prop = sanitize_label(gate.candidateprop_min) if gate.candidateprop_min else f"min_{base_prop}"
        max_prop = sanitize_label(gate.candidateprop_max) if gate.candidateprop_max else f"max_{base_prop}"
        param = self._operand(gate)

        parts = []
        parts.append(f"(candidate.{min_prop} IS NULL OR candidate.{min_prop} <= {param})")
        parts.append(f"(candidate.{max_prop} IS NULL OR {param} <= candidate.{max_prop})")
        tested = [
            _Operand(f"candidate.{min_prop}", guarded=True),
            _Operand(f"candidate.{max_prop}", guarded=True),
            *self._param_operands(gate),
        ]
        return _Predicate(AND_JOINER.join(parts), tested)

    def _compile_enum(self, gate: GateSpec) -> str:
        """
        Enum gate: candidate.prop IN $param_list
        Or inverse: $param IN candidate.prop_list
        """
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        param = self._operand(gate)
        tested = [_Operand(f"candidate.{prop}"), *self._param_operands(gate)]
        if gate.invertible:
            return _Predicate(f"{param} IN candidate.{prop}", tested)
        return _Predicate(f"candidate.{prop} IN {param}", tested)

    def _compile_exclusion(self, gate: GateSpec) -> str:
        """
        Exclusion gate: candidate must not have an outgoing exclusion edge to
        the query entity.

        The match Cypher binds only the `candidate` node — the query entity is
        supplied as parameters, never as a matched node. Referencing a bare
        `query` variable inside a pattern expression is invalid Cypher
        ("PatternExpressions are not allowed to introduce new variables"),
        so the excluded target is matched by its entity id from the query
        parameter instead:

            NOT EXISTS { MATCH (candidate)-[:EDGE]->(excluded)
                         WHERE excluded.entity_id = $<queryparam> }

        When no queryparam identifies the query entity, the gate degrades to
        "candidate has no exclusion edge at all" — conservative but valid.
        """
        from engine.config.settings import settings
        from engine.config.structural_vocabulary import declared

        # Stage 2 follow-up (item 8): the exclusion edge and its target property
        # are the domain's declarations (declared_structural_properties_required_enabled).
        edge_type = sanitize_label(
            declared(gate.edgetype, "exclusion_edge", f"gate {gate.name!r} (exclusion) declares no edgetype")
        )
        if settings.exclusion_endpoint_validation:
            self._check_exclusion_endpoints(gate, edge_type)
        # edge_type and target_prop are sanitized via sanitize_label() above;
        # values only ever flow through $ parameters.
        exclusion_edge = "(candidate)-[:" + edge_type + "]->"
        if gate.queryparam is not None:
            target_prop = sanitize_label(
                declared(
                    gate.candidateprop,
                    "exclusion_target",
                    f"gate {gate.name!r} (exclusion) declares no candidateprop (the excluded node's property)",
                )
            )
            param = self._operand(gate)
            # candidateprop names the excluded TARGET node's property, never the
            # candidate's, so only the query parameter takes part in null handling.
            return _Predicate(
                "NOT EXISTS { MATCH "
                + exclusion_edge
                + "(excluded) WHERE excluded."
                + target_prop
                + f" = {param} "
                + "}",
                self._param_operands(gate),
            )
        return _Predicate("NOT EXISTS { MATCH " + exclusion_edge + "() }", [])

    def _check_exclusion_endpoints(self, gate: GateSpec, edge_type: str) -> None:
        """Refuse an exclusion gate the compiled predicate cannot honour (CEG-COR-019).

        The predicate always anchors the edge at the matched candidate. A gate
        declaring ``fromnode`` / ``tonode`` asks for another anchor (e.g. the
        query entity), which the compiler ignores; an ontology edge that does
        not start at a candidate label of the gate's direction(s) is one no
        candidate can carry. Either way ``NOT EXISTS`` is always true and the
        exclusion silently admits everything.
        """
        declared = [f for f in ("fromnode", "tonode") if _declared(gate, f)]
        if declared:
            msg = (
                f"Gate '{gate.name}': exclusion gates do not support {' / '.join(declared)} — the edge is always "
                "matched from the candidate, so the declared endpoint would be ignored"
            )
            raise ValueError(msg)
        ontology = getattr(self.domain_spec, "ontology", None)
        edges = getattr(ontology, "edges", None)
        matchentities = getattr(self.domain_spec, "matchentities", None)
        candidates = getattr(matchentities, "candidate", None)
        if not isinstance(edges, list) or not isinstance(candidates, list):
            return  # not a validated DomainSpec (a test double); nothing to check against
        origins = {e.from_ for e in edges if e.type == edge_type}
        if not origins:
            msg = f"Gate '{gate.name}': exclusion edge type '{edge_type}' is not declared in the ontology"
            raise ValueError(msg)
        # ``compile_all_gates`` applies the gate to each direction on its own,
        # binding ``candidate`` to that direction's label, so the edge must be
        # one the candidate of *every* applicable direction can carry. An
        # unscoped gate applies to every declared direction; checking it against
        # the union of their labels would let one direction's candidate vouch
        # for another whose NOT EXISTS is always true.
        for direction in self._exclusion_directions(gate, candidates):
            candidate_labels = {c.label for c in candidates if c.matchdirection == direction}
            if not origins & candidate_labels:
                msg = (
                    f"Gate '{gate.name}': exclusion edge '{edge_type}' starts at {sorted(origins)}, not at a "
                    f"candidate label of direction '{direction}' {sorted(candidate_labels)}; no candidate there "
                    "can carry it"
                )
                raise ValueError(msg)

    def _exclusion_directions(self, gate: GateSpec, candidates: list[Any]) -> list[str]:
        """The match directions ``compile_all_gates`` applies ``gate`` to, in declaration order."""
        if gate.matchdirections:
            return list(dict.fromkeys(gate.matchdirections))
        queryschema = getattr(self.domain_spec, "queryschema", None)
        declared = getattr(queryschema, "matchdirections", None)
        if isinstance(declared, list) and declared:
            return list(dict.fromkeys(declared))
        return list(dict.fromkeys(c.matchdirection for c in candidates))

    def _compile_composite(self, gate: GateSpec) -> str:
        """
        Composite gate: combines sub-gates with AND/OR.
        gate.subgates is a list of gate names (strings), gate.logic is 'AND' or 'OR'.

        A subgate that is not declared, an empty subgate list, and a composite
        that reaches itself through its subgates are all spec defects and raise
        (CEG-009c); the previous behaviour — warn and compile what is left —
        silently widened the gate.
        """
        from engine.config.settings import settings

        combinator = f" {_LOGIC[(gate.logic or 'AND').upper()]} "
        if not gate.subgates:
            msg = f"Gate '{gate.name}': composite gate declares no subgates"
            raise ValueError(msg)
        if gate.name in self._composite_stack:
            chain = " -> ".join([*self._composite_stack, gate.name])
            msg = f"Gate '{gate.name}': composite gates form a cycle ({chain})"
            raise ValueError(msg)

        self._composite_stack.append(gate.name)
        try:
            sub_fragments: list[str] = []
            for sub_gate_name in gate.subgates:
                sub_gate_spec = next((g for g in self._gates if g.name == sub_gate_name), None)
                if sub_gate_spec is None:
                    msg = f"Gate '{gate.name}': subgate '{sub_gate_name}' is not declared in this domain spec"
                    raise ValueError(msg)
                direction = self._direction
                if (
                    direction is not None
                    and settings.composite_subgate_scoping
                    and sub_gate_spec.matchdirections
                    and direction not in sub_gate_spec.matchdirections
                ):
                    msg = (
                        f"Gate '{gate.name}': subgate '{sub_gate_name}' is scoped to "
                        f"{sub_gate_spec.matchdirections}, not to direction '{direction}' being compiled"
                    )
                    raise ValueError(msg)
                sub_fragments.append(f"({self.compile(sub_gate_spec)})")
        finally:
            self._composite_stack.pop()

        # Each subgate already carries its own null handling.
        return _Predicate(combinator.join(sub_fragments), [])

    def _compile_self_range(self, gate: GateSpec) -> str:
        """
        Self-range gate: candidate defines its own min/max that the query value must fall into.
        Pattern: candidate.min_prop <= $param AND $param <= candidate.max_prop
        Differs from range in that both bounds come from the candidate.
        """
        base_prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        min_prop = sanitize_label(gate.candidateprop_min) if gate.candidateprop_min else f"min_{base_prop}"
        max_prop = sanitize_label(gate.candidateprop_max) if gate.candidateprop_max else f"max_{base_prop}"
        param = self._operand(gate)

        tested = [
            _Operand(f"candidate.{min_prop}", guarded=True),
            _Operand(f"candidate.{max_prop}", guarded=True),
            *self._param_operands(gate),
        ]
        return _Predicate(
            f"(candidate.{min_prop} IS NULL OR candidate.{min_prop} <= {param}) AND "
            f"(candidate.{max_prop} IS NULL OR {param} <= candidate.{max_prop})",
            tested,
        )

    def _compile_freshness(self, gate: GateSpec) -> str:
        """
        Freshness gate: candidate.prop >= datetime() - duration({days: N})
        (``date()`` for a date-declared property, ``_freshness_now``).
        Ensures data recency (e.g., rate sheets updated within 24h).
        """
        from engine.config.settings import settings

        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "updated_at"
        duration_field = "days"
        if settings.freshness_maxagedays_validated:
            # Default only when absent; 0 used to become 1 day and a negative
            # age a cutoff in the future (CEG-COR-079).
            if gate.maxagedays is None:
                duration_value = 1
            elif int(gate.maxagedays) < 1:
                msg = f"Gate '{gate.name}': freshness maxagedays must be >= 1 day, got {gate.maxagedays!r}"
                raise ValueError(msg)
            else:
                duration_value = int(gate.maxagedays)
        else:
            duration_value = int(gate.maxagedays or 1)
        now = self._freshness_now(gate, gate.candidateprop or "updated_at")
        return _Predicate(
            f"candidate.{prop} >= {now} - duration({{{duration_field}: {duration_value}}})",
            [_Operand(f"candidate.{prop}")],
        )

    def _freshness_now(self, gate: GateSpec, prop: str) -> str:
        """``date()`` when every candidate label the gate applies to declares ``prop`` a ``date``.

        A native Neo4j ``Date`` compared with a ``DateTime`` is null, so the
        legacy ``datetime()`` cutoff failed every date-declared candidate (D1).
        A ``datetime``-declared, undeclared (``updated_at``, stamped by
        ``datetime()`` in the sync generator) or mixed property keeps
        ``datetime()``. ``freshness_gate_declared_temporal_enabled`` off: always
        ``datetime()``.
        """
        from engine.config.settings import settings

        if not settings.freshness_gate_declared_temporal_enabled:
            return "datetime()"
        matchentities = getattr(self.domain_spec, "matchentities", None)
        candidates = getattr(matchentities, "candidate", None)
        ontology = getattr(self.domain_spec, "ontology", None)
        nodes = getattr(ontology, "nodes", None)
        if not isinstance(candidates, list) or not isinstance(nodes, list):
            return "datetime()"  # not a validated DomainSpec (a test double)
        directions = set(self._exclusion_directions(gate, candidates))
        labels = {c.label for c in candidates if c.matchdirection in directions}
        declared = {p.type.value for node in nodes if node.label in labels for p in node.properties if p.name == prop}
        return "date()" if declared == {"date"} else "datetime()"

    def _compile_temporal_range(self, gate: GateSpec) -> str:
        """
        Temporal range gate.

        * With `candidateprop_start` / `candidateprop_end` the candidate's window
          must overlap the query window:
          candidate.start <= $end AND candidate.end >= $start
        * Otherwise the candidate's single `candidateprop` (default `timestamp`)
          must fall between the two datetime parameters.

        Before `gate_declared_bounds_enabled` the candidate window fields were
        declared by the schema and ignored here, so such a gate compared
        `candidate.timestamp` — a property no such candidate carries.
        """
        from engine.config.settings import settings

        named = self._named_param(gate)
        start_name = _declared(gate, "queryparam_start") or (f"{named}_start" if named else None)
        end_name = _declared(gate, "queryparam_end") or (f"{named}_end" if named else None)
        if not start_name or not end_name:
            msg = (
                f"Gate '{gate.name}': temporalrange needs queryparam_start and queryparam_end "
                "(or a named queryparam to derive them from)"
            )
            raise ValueError(msg)
        start_param = sanitize_label(start_name)
        end_param = sanitize_label(end_name)

        prop_start = _declared(gate, "candidateprop_start")
        prop_end = _declared(gate, "candidateprop_end")
        if settings.gate_declared_bounds_enabled and prop_start and prop_end:
            cand_start = sanitize_label(prop_start)
            cand_end = sanitize_label(prop_end)
            if _operands_from_predicate():
                # Window overlap: each side is unconstrained when either of its
                # operands is NULL (PASS); a FAIL wrap rejects any NULL.
                return _Predicate(
                    f"(candidate.{cand_start} IS NULL OR ${end_param} IS NULL OR candidate.{cand_start} <= ${end_param})"
                    f" AND (candidate.{cand_end} IS NULL OR ${start_param} IS NULL OR "
                    f"candidate.{cand_end} >= ${start_param})",
                    [
                        _Operand(f"candidate.{cand_start}", guarded=True),
                        _Operand(f"candidate.{cand_end}", guarded=True),
                        _Operand(f"${start_param}", guarded=True),
                        _Operand(f"${end_param}", guarded=True),
                    ],
                )
            return f"candidate.{cand_start} <= ${end_param} AND candidate.{cand_end} >= ${start_param}"

        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "timestamp"
        if _operands_from_predicate():
            # The tested parameters are the start/end names, never the bare queryparam.
            return _Predicate(
                f"(${start_param} IS NULL OR candidate.{prop} >= ${start_param}) AND "
                f"(${end_param} IS NULL OR candidate.{prop} <= ${end_param})",
                [
                    _Operand(f"candidate.{prop}"),
                    _Operand(f"${start_param}", guarded=True),
                    _Operand(f"${end_param}", guarded=True),
                ],
            )
        return f"candidate.{prop} >= ${start_param} AND candidate.{prop} <= ${end_param}"

    def _compile_traversal(self, gate: GateSpec) -> str:
        """
        Traversal gate: requires an edge path to exist.
        Pattern: EXISTS { MATCH (candidate)-[:EDGE_TYPE]->(t:Target) WHERE t.prop = $param }

        Stage 2 (CEG Stage 4 proof, P4): the previous shape,
        ``exists((candidate)-[:EDGE]->(t:Label {prop: $param}))``, is rejected
        by Neo4j 5 ("PatternExpressions are not allowed to introduce new
        variables: 't'"), so every match in a direction carrying a traversal
        gate failed with a syntax error. This gate is the only spec-level way
        to constrain candidates by a related taxonomy node's property, which is
        what makes a domain pack self-sufficient. The EXISTS subquery is the
        same form the exclusion gate already uses.
        """
        from engine.config.settings import settings

        edge_type = sanitize_label(gate.edgetype) if gate.edgetype else "RELATES_TO"
        target_label = sanitize_label(gate.tonode) if gate.tonode else ""
        label_clause = f":{target_label}" if target_label else ""
        target_filter = ""

        if not settings.traversal_gate_exists_subquery:
            # Legacy predicate (C-21 rollback path). Not executable on Neo4j 5.
            if gate.candidateprop and gate.queryparam is not None:
                prop = sanitize_label(gate.candidateprop)
                param = self._operand(gate)
                target_filter = f" {{{prop}: {param}}}"
            return f"exists((candidate)-[:{edge_type}]->(t{label_clause}{target_filter}))"

        if gate.candidateprop and gate.queryparam is not None:
            # candidateprop names the TARGET node's property here (the related
            # taxonomy node), not a property of the candidate.
            prop = sanitize_label(gate.candidateprop)
            param = self._operand(gate)
            target_filter = f" WHERE t.{prop} = {param}"

        rel = ""
        edgepredicates = getattr(gate, "edgepredicates", None)
        if settings.buyer_line_identity_enabled and isinstance(edgepredicates, list) and edgepredicates:
            rel = "rel"
            edge_filter = self._edge_predicate_filter(gate)
            target_filter = f"{target_filter} AND {edge_filter}" if target_filter else f" WHERE {edge_filter}"

        hop = f"-[{rel}:{edge_type}]->" if rel else f"-[:{edge_type}]->"
        # Concatenated like the exclusion gate above: edge_type / target_label /
        # prop are sanitize_label()-validated and values only ever flow through
        # $ parameters (C-009; tools/contract_scanner.py SEC-001).
        # candidateprop is the target node's property: only the parameter is null-checked.
        # Edge-predicate literals are bound by _bind and are not null operands.
        return _Predicate(
            "EXISTS { MATCH (candidate)" + hop + "(t" + label_clause + ")" + target_filter + " }",
            self._param_operands(gate) if gate.candidateprop and gate.queryparam is not None else [],
        )

    def _edge_predicate_filter(self, gate: GateSpec) -> str:
        """AND of the relationship predicates, values bound as parameters."""
        from engine.config.schema import missing_edge_property_reason

        message = missing_edge_property_reason(self.domain_spec, gate)
        if message is not None:
            raise ValueError(message)
        clauses: list[str] = []
        for index, predicate in enumerate(gate.edgepredicates or []):
            prop = sanitize_label(predicate.property)
            if predicate.present:
                blank = self._bind(gate, f"edge{index}blank", "")
                clauses.append(f"rel.{prop} IS NOT NULL AND rel.{prop} <> {blank}")
            elif predicate.equals is not None:
                param = self._bind(gate, f"edge{index}", predicate.equals)
                clauses.append(f"rel.{prop} = {param}")
            else:
                msg = f"Gate '{gate.name}': edge predicate {predicate.property!r} needs equals or present"
                raise ValueError(msg)
        return " AND ".join(clauses)

    # ── Null Semantics ─────────────────────────────────────

    def _wrap_null_semantics(self, gate: GateSpec, predicate: str) -> str:
        """
        Wrap predicate with NULL handling based on gate's null behavior.

        NullBehavior.PASS   → (candidate.prop IS NULL OR <predicate>)
        NullBehavior.FAIL   → (candidate.prop IS NOT NULL AND <predicate>)

        A literal `queryparam` is never NULL, so only a declared parameter name
        takes part in the null check.
        """
        from engine.config.settings import settings

        null_behavior = gate.nullbehavior
        # C-14 says every gate declares its null behavior and the compiler
        # honours it. GateSpec defaults `nullbehavior` to PASS, so an explicit
        # `nullbehavior: pass` used to be indistinguishable from "not declared"
        # and was overridden by the per-type default (FAIL for boolean,
        # freshness and traversal gates). Pydantic records which fields the
        # spec actually set; an explicit declaration now wins (Stage 2 P4).
        declared = "nullbehavior" in getattr(gate, "model_fields_set", frozenset())
        if settings.gate_explicit_nullbehavior and declared and null_behavior is not None:
            pass  # spec-declared, keep as written
        elif null_behavior is None or null_behavior == NullBehavior.PASS:
            null_behavior = NullHandler.DEFAULT_BEHAVIORS.get(gate.type, NullBehavior.PASS)

        # For a traversal gate, candidateprop names the related TARGET node's
        # property, which the EXISTS subquery already tests. Wrapping it as
        # `candidate.<prop>` would make the gate pass or fail on whether the
        # candidate happens to carry a same-named property — a hidden coupling
        # between the candidate's shape and its taxonomy (Stage 2, P4). The
        # legacy wrap is kept behind the same flag as the legacy predicate.

        operands = getattr(predicate, "operands", None)
        if operands is not None and _operands_from_predicate():
            return NullHandler.wrap_operands(
                null_behavior=null_behavior,
                gate_cypher=str(predicate),
                operands=[op.ref for op in operands],
                unguarded=[op.ref for op in operands if not op.guarded],
            )

        is_traversal = gate.type == GateType.TRAVERSAL
        skip_candidate_prop = is_traversal and settings.traversal_gate_exists_subquery
        candidate_prop: str | None = None
        if gate.candidateprop and not skip_candidate_prop:
            candidate_prop = f"candidate.{sanitize_label(gate.candidateprop)}"

        named = self._named_param(gate)

        return NullHandler.wrap_gate_with_null_logic(
            gate_type=gate.type,
            null_behavior=null_behavior,
            gate_cypher=predicate,
            candidate_prop=candidate_prop,
            query_param=f"${_param_name(named)}" if named else None,
        )
