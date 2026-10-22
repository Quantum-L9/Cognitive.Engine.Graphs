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
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

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

# Bounds on the spec-authored expression grammar (strictwhen, traversal
# conditions) so a pathological spec cannot exhaust the parser.
_MAX_EXPRESSION_LEN = 512
_MAX_EXPRESSION_DEPTH = 16


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
    if len(raw_key) > _MAX_PARAM_KEY_LEN:
        # Keep the key an identifier of bounded length; the digest keeps it unique per gate.
        digest = hashlib.sha256(safe_gate.encode()).hexdigest()[:16]
        raw_key = f"gate_{digest}_{safe_suffix}"[:_MAX_PARAM_KEY_LEN]
    return sanitize_label(raw_key)


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


# ── Spec-authored expression grammar (CEG-009a / CEG-009c) ─────────────────
#
# `strictwhen` and a traversal gate's `condition` are the two places a domain
# spec writes a predicate in free text. Neither is Cypher: both are parsed
# against the grammar below and re-emitted, so a spec can never carry Cypher
# into the query and every literal travels as a bound parameter (C-009).
# There is no eval, exec or compile anywhere on this path.
#
#   expr      := or_expr
#   or_expr   := and_expr ( OR and_expr )*
#   and_expr  := not_expr ( AND not_expr )*
#   not_expr  := NOT not_expr | primary
#   primary   := '(' expr ')'
#              | operand IS [NOT] NULL
#              | operand cmp operand
#   operand   := '$' ident | '$query.' ident | 'query.' ident      (a query parameter)
#              | variable '.' ident                                 (a bound node's property)
#              | variable                                           (only before IS [NOT] NULL)
#              | 'string' | number | true | false                   (a literal → bound parameter)
#   cmp       := = | != | <> | < | <= | > | >=
#
# `variable` is whichever node names the caller binds: `candidate` for
# strictwhen; `candidate` plus the pattern's own variable for a traversal
# condition. `query` is never a node — the query entity travels as parameters.

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<string>'[^']*')
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<param>\$(?:query\.)?[A-Za-z_][A-Za-z0-9_]*)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)
      | (?P<op><>|!=|<=|>=|=|<|>)
      | (?P<lparen>\()
      | (?P<rparen>\))
    )""",
    re.VERBOSE,
)
_KEYWORDS = frozenset({"AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE"})
_CMP_OPERATORS: dict[str, str] = {op: op for op in ("=", "!=", "<>", "<", "<=", ">", ">=")}


class _Token(NamedTuple):
    kind: str
    text: str


class _Operand(NamedTuple):
    cypher: str
    is_node: bool  # a bare bound variable, legal only before IS [NOT] NULL


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    end = len(expression)
    while pos < end:
        if expression[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(expression, pos)
        if match is None or match.lastgroup is None:
            msg = f"unexpected character {expression[pos]!r} at offset {pos}"
            raise ValueError(msg)
        kind = match.lastgroup
        text = match.group(kind)
        if kind == "ident" and text.upper() in _KEYWORDS:
            kind, text = "keyword", text.upper()
        tokens.append(_Token(kind, text))
        pos = match.end()
    return tokens


class _ConditionCompiler:
    """Recursive-descent compiler for the grammar above.

    ``variables`` maps each node name the condition may reference to the
    Cypher variable it compiles to. ``bind`` registers a literal and returns
    its ``$parameter`` reference.
    """

    def __init__(
        self,
        expression: str,
        *,
        variables: Mapping[str, str],
        bind: Callable[[LiteralOperand | str], str],
    ) -> None:
        if len(expression) > _MAX_EXPRESSION_LEN:
            msg = f"expression exceeds {_MAX_EXPRESSION_LEN} characters"
            raise ValueError(msg)
        self._tokens = _tokenize(expression)
        self._pos = 0
        self._variables = variables
        self._bind = bind

    # -- token helpers --------------------------------------------------

    def _peek(self, offset: int = 0) -> _Token | None:
        index = self._pos + offset
        return self._tokens[index] if index < len(self._tokens) else None

    def _next(self) -> _Token:
        token = self._peek()
        if token is None:
            msg = "unexpected end of expression"
            raise ValueError(msg)
        self._pos += 1
        return token

    def _accept_keyword(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "keyword" and token.text == word:
            self._pos += 1
            return True
        return False

    def _expect_keyword(self, word: str) -> None:
        if not self._accept_keyword(word):
            token = self._peek()
            found = token.text if token is not None else "end of expression"
            msg = f"expected {word}, found {found!r}"
            raise ValueError(msg)

    # -- grammar --------------------------------------------------------

    def compile(self) -> str:
        if not self._tokens:
            msg = "empty expression"
            raise ValueError(msg)
        cypher = self._or_expr(0)
        token = self._peek()
        if token is not None:
            msg = f"unexpected token {token.text!r}"
            raise ValueError(msg)
        return cypher

    def _or_expr(self, depth: int) -> str:
        parts = [self._and_expr(depth)]
        while self._accept_keyword("OR"):
            parts.append(self._and_expr(depth))
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def _and_expr(self, depth: int) -> str:
        parts = [self._not_expr(depth)]
        while self._accept_keyword("AND"):
            parts.append(self._not_expr(depth))
        return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"

    def _not_expr(self, depth: int) -> str:
        if depth > _MAX_EXPRESSION_DEPTH:
            msg = f"expression nested deeper than {_MAX_EXPRESSION_DEPTH}"
            raise ValueError(msg)
        if self._accept_keyword("NOT"):
            return "NOT " + self._not_expr(depth + 1)
        return self._primary(depth)

    def _primary(self, depth: int) -> str:
        token = self._peek()
        if token is not None and token.kind == "lparen":
            self._pos += 1
            inner = self._or_expr(depth + 1)
            closing = self._next()
            if closing.kind != "rparen":
                msg = f"expected ')', found {closing.text!r}"
                raise ValueError(msg)
            return "(" + inner + ")"

        left = self._operand()
        if self._accept_keyword("IS"):
            negated = self._accept_keyword("NOT")
            self._expect_keyword("NULL")
            return f"{left.cypher} IS NOT NULL" if negated else f"{left.cypher} IS NULL"
        if left.is_node:
            msg = f"a bound node ({left.cypher!r}) may only be tested with IS [NOT] NULL"
            raise ValueError(msg)
        operator = self._next()
        if operator.kind != "op":
            msg = f"expected a comparison operator, found {operator.text!r}"
            raise ValueError(msg)
        right = self._operand()
        if right.is_node:
            msg = f"a bound node ({right.cypher!r}) may only be tested with IS [NOT] NULL"
            raise ValueError(msg)
        return f"{left.cypher} {_CMP_OPERATORS[operator.text]} {right.cypher}"

    def _operand(self) -> _Operand:
        token = self._next()
        if token.kind == "param":
            name = token.text.removeprefix("$").removeprefix("query.")
            return _Operand("$" + sanitize_label(name), False)
        if token.kind == "string":
            return _Operand(self._bind(token.text[1:-1]), False)
        if token.kind == "number":
            value: LiteralOperand = float(token.text) if "." in token.text else int(token.text)
            return _Operand(self._bind(value), False)
        if token.kind == "keyword" and token.text in ("TRUE", "FALSE"):
            return _Operand(self._bind(token.text == "TRUE"), False)
        if token.kind == "keyword" and token.text == "NULL":
            msg = "a bare null literal is not an operand; use IS NULL / IS NOT NULL"
            raise ValueError(msg)
        if token.kind == "ident":
            head, _, prop = token.text.partition(".")
            if head == "query":
                if not prop:
                    msg = "'query' is not a bound node; reference query parameters as $name"
                    raise ValueError(msg)
                return _Operand("$" + sanitize_label(prop), False)
            variable = self._variables.get(head)
            if variable is None:
                allowed = ", ".join(sorted(self._variables)) or "none"
                msg = f"unknown reference {token.text!r}; bound nodes here: {allowed}"
                raise ValueError(msg)
            if prop:
                return _Operand(f"{variable}.{sanitize_label(prop)}", False)
            return _Operand(variable, True)
        msg = f"unexpected token {token.text!r}"
        raise ValueError(msg)


# ── Traversal pattern grammar (CEG-009a) ───────────────────────────────────
#
# One hop from the bound candidate to a named target node, in either
# direction, with an optional label and an optional property map whose values
# are parameters or literals. Anything else — a second hop, a `query` node,
# an anonymous target, a literal that is not a map value — is rejected: the
# query entity is never a bound node in the match statement, so a pattern that
# names it can never execute.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TRAVERSAL_PATTERN_RE = re.compile(
    rf"""^\(\s*candidate\s*\)\s*
        (?:
            -\s*\[\s*:\s*(?P<edge_out>{_IDENT})\s*\]\s*->
          | <-\s*\[\s*:\s*(?P<edge_in>{_IDENT})\s*\]\s*-
        )\s*
        \(\s*(?P<var>{_IDENT})\s*(?::\s*(?P<label>{_IDENT}))?\s*
            (?:\{{(?P<props>[^{{}}]*)\}})?\s*\)$""",
    re.VERBOSE,
)
_TRAVERSAL_PROP_RE = re.compile(
    rf"""^\s*(?P<key>{_IDENT})\s*:\s*
        (?:(?P<param>\$(?:query\.)?{_IDENT})|(?P<string>'[^']*')|(?P<number>-?\d+(?:\.\d+)?)|(?P<bool>true|false))
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)
_TRAVERSAL_GRAMMAR_HINT = (
    "accepted forms: '(candidate)-[:EDGE]->(v:Label)', '(candidate)<-[:EDGE]-(v:Label)', "
    "optionally with a property map '(v:Label {prop: $param})'; one hop, and 'query' is never "
    "a bound node — the query entity travels as parameters"
)


class GateCompiler:
    """
    Compiles GateSpec objects from a domain spec into Cypher WHERE clause fragments.

    Each gate type maps to a specific Cypher predicate pattern:
        boolean   → candidate.prop = $param
        threshold → candidate.prop >= $param (or <=, configurable)
        range     → $param_min <= candidate.prop <= $param_max  (or candidate range check)
        enum      → candidate.prop IN $param_list
        exclusion → NOT (candidate)-[:EXCLUDED_FROM]->(query)
        composite → (gate1 AND gate2) OR gate3
        self_range→ candidate.min_prop <= $param AND $param <= candidate.max_prop
        freshness → candidate.prop >= datetime() - duration({days: $days})
        temporal_range → candidate.prop >= $start AND candidate.prop <= $end
        traversal → EXISTS { MATCH (candidate)-[:EDGE]->(t:Target) WHERE t.prop = $param }

    A gate whose `queryparam` is a scalar (`85.0`, `true`) compiles against a
    generated parameter instead of a parameter *name*; the value is exposed on
    `last_query_params` and must be merged into the execute_query parameters
    together with the resolved query (CEG-009b). A gate with `strictwhen`
    filters only while that condition holds (CEG-009c).

    Usage:
        compiler = GateCompiler(domain_spec)
        where_clause = compiler.compile_all_gates("buyer_to_seller")
        parameters = {**resolved_query, **compiler.last_query_params}
        single = compiler.compile(gate_spec)
    """

    def __init__(self, domain_spec: DomainSpec):
        self.domain_spec = domain_spec
        self._gates: list[GateSpec] = domain_spec.gates if domain_spec.gates else []
        self._compliance = getattr(domain_spec, "compliance", None)
        # C-009: literal operands bound during compilation, keyed by generated name.
        self._query_params: dict[str, Any] = {}
        # CEG-009c: composite gates currently being expanded (cycle guard).
        self._composite_stack: list[str] = []

    # ── W1-03: Gate validation (seL4-inspired) ──────────────

    def validate_gates(
        self,
        resolved_params: dict[str, object] | None = None,
    ) -> list[str]:
        """W1-03: Pre-compilation gate validation.

        Checks:
        (a) Every required gate field has a corresponding parameter in the resolved set.
        (b) No gate uses an operator incompatible with the field's declared type.
        (c) Post-compilation: if a resolved parameter is None, log WARNING and
            reject if STRICT_NULL_GATES is enabled.

        A literal `queryparam` is bound by the compiler itself and therefore
        never looked up in the resolved set.

        Returns list of warning messages (empty = all clear).
        """
        from engine.config.settings import settings

        warnings: list[str] = []
        # Build a property type index from ontology
        prop_types: dict[str, str] = {}
        for node in self.domain_spec.ontology.nodes:
            for prop in node.properties:
                prop_types[prop.name] = prop.type.value

        # Operator-type compatibility matrix
        numeric_operators = frozenset({">=", "<=", ">", "<"})
        numeric_types = frozenset({"int", "float", "datetime", "date", "duration"})

        for gate in self._gates:
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

        The fragment carries the gate's null semantics and, when the gate
        declares `strictwhen`, its conditional-strictness wrapper. Composite
        gates compile their subgates through this method, so both apply to a
        subgate exactly as they would to the gate on its own.
        """
        handler = self._get_handler(gate.type)
        raw_predicate = handler(gate)
        wrapped = self._wrap_null_semantics(gate, raw_predicate)
        return self._apply_strictwhen(gate, wrapped)

    def compile_all_gates(
        self,
        match_direction: str,
        role: str | None = None,
    ) -> str:
        """
        Compile all gates into a combined WHERE clause.

        Args:
            match_direction: Current match direction (e.g., "buyer_to_seller")
            role: Facility role for exemption checking

        Returns:
            Combined Cypher WHERE clause (without the WHERE keyword)
        """
        self._query_params = {}
        fragments: list[str] = []

        for gate in self._gates:
            # Direction filter
            if gate.matchdirections and match_direction not in gate.matchdirections:
                continue

            # Role exemption
            if role and gate.roleexempt and role in gate.roleexempt:
                logger.debug(f"Gate {gate.name} exempted for role={role}")
                continue

            fragment = self.compile(gate)
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
        hard_fragments: list[str] = []

        for gate in self._gates:
            if gate.matchdirections and match_direction not in gate.matchdirections:
                continue
            if role and gate.roleexempt and role in gate.roleexempt:
                continue

            if math.isclose(gate.relaxedpenalty, 0.0, abs_tol=1e-9):
                fragment = self.compile(gate)
                if fragment:
                    hard_fragments.append(f"({fragment})")

        if not hard_fragments:
            return "true"

        return AND_JOINER.join(hard_fragments)

    def compile_traversal(self, gate: GateSpec) -> str:
        """Compile a traversal gate's predicate (no null wrapper, no strictwhen).

        The one traversal implementation in the engine: `all_gates.TraversalGate`
        delegates here so a spec's `pattern`/`condition` is parsed by this
        grammar wherever it is compiled (CEG-009a).
        """
        return self._compile_traversal(gate)

    # ── Parameter binding (C-009 / CEG-009b) ────────────────

    def _bind(self, gate: GateSpec, suffix: str, value: LiteralOperand | str | list[Any]) -> str:
        """Register ``value`` under a generated name and return its ``$`` reference."""
        key = bind_param_key(gate.name, suffix)
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
        if gate.queryparam is not None:
            return f"candidate.{prop} = {self._operand(gate)}"
        return f"candidate.{prop} = true"

    def _compile_threshold(self, gate: GateSpec) -> str:
        """
        Threshold gate: candidate.prop >= $param (default).
        Supports operator override via gate.operator: >=, <=, >, <, =
        """
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        op = _OPERATORS[gate.operator or ">="]
        return f"candidate.{prop} {op} {self._operand(gate)}"

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
            return f"candidate.{base_prop} >= ${lo} AND candidate.{base_prop} <= ${hi}"

        min_prop = sanitize_label(gate.candidateprop_min) if gate.candidateprop_min else f"min_{base_prop}"
        max_prop = sanitize_label(gate.candidateprop_max) if gate.candidateprop_max else f"max_{base_prop}"
        param = self._operand(gate)

        parts = []
        parts.append(f"(candidate.{min_prop} IS NULL OR candidate.{min_prop} <= {param})")
        parts.append(f"(candidate.{max_prop} IS NULL OR {param} <= candidate.{max_prop})")
        return AND_JOINER.join(parts)

    def _compile_enum(self, gate: GateSpec) -> str:
        """
        Enum gate: candidate.prop IN $param_list
        Or inverse: $param IN candidate.prop_list
        """
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
        param = self._operand(gate)
        if gate.invertible:
            return f"{param} IN candidate.{prop}"
        return f"candidate.{prop} IN {param}"

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
        edge_type = sanitize_label(gate.edgetype) if gate.edgetype else "EXCLUDED_FROM"
        target_prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "entity_id"
        # edge_type and target_prop are sanitized via sanitize_label() above;
        # values only ever flow through $ parameters.
        exclusion_edge = "(candidate)-[:" + edge_type + "]->"
        if gate.queryparam is not None:
            param = self._operand(gate)
            return (
                "NOT EXISTS { MATCH "
                + exclusion_edge
                + "(excluded) WHERE excluded."
                + target_prop
                + f" = {param} "
                + "}"
            )
        return "NOT EXISTS { MATCH " + exclusion_edge + "() }"

    def _compile_composite(self, gate: GateSpec) -> str:
        """
        Composite gate: combines sub-gates with AND/OR.
        gate.subgates is a list of gate names (strings), gate.logic is 'AND' or 'OR'.

        A subgate that is not declared, an empty subgate list, and a composite
        that reaches itself through its subgates are all spec defects and raise
        (CEG-009c); the previous behaviour — warn and compile what is left —
        silently widened the gate.
        """
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
                sub_fragments.append(f"({self.compile(sub_gate_spec)})")
        finally:
            self._composite_stack.pop()

        return combinator.join(sub_fragments)

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

        return (
            f"(candidate.{min_prop} IS NULL OR candidate.{min_prop} <= {param}) AND "
            f"(candidate.{max_prop} IS NULL OR {param} <= candidate.{max_prop})"
        )

    def _compile_freshness(self, gate: GateSpec) -> str:
        """
        Freshness gate: candidate.prop >= datetime() - duration({days: N})
        Ensures data recency (e.g., rate sheets updated within 24h).
        """
        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "updated_at"
        duration_field = "days"
        duration_value = int(gate.maxagedays or 1)
        return f"candidate.{prop} >= datetime() - duration({{{duration_field}: {duration_value}}})"

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
            return f"candidate.{cand_start} <= ${end_param} AND candidate.{cand_end} >= ${start_param}"

        prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "timestamp"
        return f"candidate.{prop} >= ${start_param} AND candidate.{prop} <= ${end_param}"

    def _compile_traversal(self, gate: GateSpec) -> str:
        """
        Traversal gate: requires an edge path to exist.
        Pattern: EXISTS { MATCH (candidate)-[:EDGE_TYPE]->(t:Target) WHERE t.prop = $param }

        Two declared shapes (CEG-009a):

        * structured — `edgetype` (required), optional `tonode`, and optionally
          `candidateprop` + `queryparam`, where `candidateprop` names the TARGET
          node's property (the related taxonomy node), not a property of the
          candidate;
        * `pattern` (+ optional `condition`) — parsed against the one-hop
          grammar in this module and re-emitted; never interpolated.

        There is no default relationship type: a gate with neither shape raises.
        The former `RELATES_TO` fallback produced a hard filter on an edge no
        ontology declares, which rejected every candidate.

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

        pattern = _declared(gate, "pattern")
        if pattern is not None:
            return self._compile_traversal_pattern(gate, pattern, _declared(gate, "condition"))

        edge_name = _declared(gate, "edgetype")
        if edge_name is None:
            msg = (
                f"Gate '{gate.name}': a traversal gate needs `edgetype` (with optional `tonode`, "
                "`candidateprop`, `queryparam`) or a `pattern`; there is no default relationship type"
            )
            raise ValueError(msg)
        edge_type = sanitize_label(edge_name)
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
            prop = sanitize_label(gate.candidateprop)
            param = self._operand(gate)
            target_filter = f" WHERE t.{prop} = {param}"

        # Concatenated like the exclusion gate above: edge_type / target_label /
        # prop are sanitize_label()-validated and values only ever flow through
        # $ parameters (C-009; tools/contract_scanner.py SEC-001).
        return "EXISTS { MATCH (candidate)-[:" + edge_type + "]->(t" + label_clause + ")" + target_filter + " }"

    def _compile_traversal_pattern(self, gate: GateSpec, pattern: str, condition: str | None) -> str:
        """Compile a spec-authored `pattern` / `condition` pair against the traversal grammar."""
        match = _TRAVERSAL_PATTERN_RE.match(pattern.strip())
        if match is None:
            msg = f"Gate '{gate.name}': traversal pattern {pattern!r} is not accepted; {_TRAVERSAL_GRAMMAR_HINT}"
            raise ValueError(msg)

        variable = match.group("var")
        if variable in ("candidate", "query"):
            msg = f"Gate '{gate.name}': traversal pattern may not name its target node {variable!r}"
            raise ValueError(msg)
        variable = sanitize_label(variable)
        label = match.group("label")
        label_clause = ":" + sanitize_label(label) if label else ""

        bound = 0

        def bind(value: LiteralOperand | str) -> str:
            nonlocal bound
            ref = self._bind(gate, f"pattern_{bound}", value)
            bound += 1
            return ref

        map_clause = self._compile_pattern_props(gate, match.group("props"), bind)

        edge_out = match.group("edge_out")
        edge = sanitize_label(edge_out or match.group("edge_in"))
        if edge_out:
            path = "(candidate)-[:" + edge + "]->(" + variable + label_clause + map_clause + ")"
        else:
            path = "(candidate)<-[:" + edge + "]-(" + variable + label_clause + map_clause + ")"

        where_clause = ""
        if condition is not None:
            try:
                compiled = _ConditionCompiler(
                    condition,
                    variables={"candidate": "candidate", variable: variable},
                    bind=bind,
                ).compile()
            except ValueError as exc:
                msg = f"Gate '{gate.name}': traversal condition {condition!r} is not accepted: {exc}"
                raise ValueError(msg) from exc
            where_clause = " WHERE " + compiled

        # path / where_clause are assembled above from sanitize_label()-validated
        # identifiers, allow-listed operators and $ parameter references only.
        return "EXISTS { MATCH " + path + where_clause + " }"

    @staticmethod
    def _compile_pattern_props(
        gate: GateSpec,
        props: str | None,
        bind: Callable[[LiteralOperand | str], str],
    ) -> str:
        """Compile a pattern's `{prop: value, …}` map; values are parameters or bound literals."""
        if props is None:
            return ""
        if not props.strip():
            msg = f"Gate '{gate.name}': traversal pattern has an empty property map"
            raise ValueError(msg)
        map_entries: list[str] = []
        for raw_entry in props.split(","):
            entry = _TRAVERSAL_PROP_RE.match(raw_entry)
            if entry is None:
                msg = (
                    f"Gate '{gate.name}': traversal pattern property {raw_entry.strip()!r} is not accepted; "
                    "each entry is 'prop: $param' or 'prop: <literal>'"
                )
                raise ValueError(msg)
            key = sanitize_label(entry.group("key"))
            if entry.group("param"):
                name = entry.group("param").removeprefix("$").removeprefix("query.")
                value_ref = "$" + sanitize_label(name)
            elif entry.group("string"):
                value_ref = bind(entry.group("string")[1:-1])
            elif entry.group("number"):
                number = entry.group("number")
                value_ref = bind(float(number) if "." in number else int(number))
            else:
                value_ref = bind(entry.group("bool").lower() == "true")
            map_entries.append(f"{key}: {value_ref}")
        return " {" + ", ".join(map_entries) + "}"

    # ── Conditional strictness (CEG-009c) ───────────────────

    def _apply_strictwhen(self, gate: GateSpec, wrapped: str) -> str:
        """Make a gate filter only while its `strictwhen` condition holds.

            (NOT coalesce(<condition>, false) OR (<gate>))

        The condition is compiled from the spec-authored expression grammar;
        `$query.x` / `query.x` / `$x` reference query parameters and literals
        are bound as generated parameters. A condition that evaluates to false
        or to NULL (an unsupplied parameter) leaves the gate open; only a true
        condition enforces it.
        """
        condition = _declared(gate, "strictwhen")
        if condition is None:
            return wrapped

        bound = 0

        def bind(value: LiteralOperand | str) -> str:
            nonlocal bound
            ref = self._bind(gate, f"strictwhen_{bound}", value)
            bound += 1
            return ref

        try:
            compiled = _ConditionCompiler(condition, variables={"candidate": "candidate"}, bind=bind).compile()
        except ValueError as exc:
            msg = f"Gate '{gate.name}': strictwhen {condition!r} is not accepted: {exc}"
            raise ValueError(msg) from exc
        return f"(NOT coalesce({compiled}, false) OR ({wrapped}))"

    # ── Null Semantics ─────────────────────────────────────

    def _wrap_null_semantics(self, gate: GateSpec, predicate: str) -> str:
        """
        Wrap predicate with NULL handling based on gate's null behavior.

        NullBehavior.PASS   → (candidate.prop IS NULL OR <predicate>)
        NullBehavior.FAIL   → (candidate.prop IS NOT NULL AND <predicate>)

        A literal `queryparam` is never NULL, so only a declared parameter name
        takes part in the null check.
        """
        null_behavior = gate.nullbehavior
        if null_behavior is None or null_behavior == NullBehavior.PASS:
            null_behavior = NullHandler.DEFAULT_BEHAVIORS.get(gate.type, NullBehavior.PASS)

        # For a traversal gate, candidateprop names the related TARGET node's
        # property, which the EXISTS subquery already tests. Wrapping it as
        # `candidate.<prop>` would make the gate pass or fail on whether the
        # candidate happens to carry a same-named property — a hidden coupling
        # between the candidate's shape and its taxonomy (Stage 2, P4). The
        # legacy wrap is kept behind the same flag as the legacy predicate.
        from engine.config.settings import settings

        is_traversal = gate.type == GateType.TRAVERSAL
        skip_candidate_prop = is_traversal and settings.traversal_gate_exists_subquery
        candidate_prop: str | None = None
        if gate.candidateprop and not skip_candidate_prop:
            candidate_prop = f"candidate.{sanitize_label(gate.candidateprop)}"

        named = self._named_param(gate)
        # A pattern-form traversal gate references its parameters inside the
        # subquery; which of them are required is the pattern's business.
        if is_traversal and _declared(gate, "pattern") is not None:
            named = None

        return NullHandler.wrap_gate_with_null_logic(
            gate_type=gate.type,
            null_behavior=null_behavior,
            gate_cypher=predicate,
            candidate_prop=candidate_prop,
            query_param=f"${_param_name(named)}" if named else None,
        )
