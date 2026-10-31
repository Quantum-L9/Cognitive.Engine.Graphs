#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [tools, security]
# tags: [cypher, lint, C-009, injection]
# owner: platform
# status: active
# --- /L9_META ---
"""Scan engine/**/*.py for unparameterized Cypher interpolations (C-009).

The scanner parses each module with :mod:`ast` and classifies **every**
interpolation of every f-string that is not a diagnostic message. There is no
keyword precondition: a fragment such as ``NOT EXISTS((query)-[:{edge}]->(c))``
is analysed whether or not it happens to contain ``MATCH`` or ``WHERE``, and a
triple-quoted multi-line query is analysed as one unit.

Each ``{expr}`` is classified by the literal text immediately before it:

=====================  ==========================  ==================================
literal before          role                        verdict
=====================  ==========================  ==================================
``'`` or ``"``          quoted data value           FAIL — pass it as a ``$parameter``
``LIMIT`` / ``SKIP``    pagination bound            FAIL — pass it as a ``$parameter``
``:``                   label / relationship type   OK only when the expression is *validated*
``.``                   property name               OK only when the expression is *validated*
``$``                   parameter **name**          OK only when the expression is *validated*
`````                   back-quoted identifier      OK only when the expression is *validated*
anything else           compiled fragment           FAIL only when the expression is a *raw
                                                    domain-spec value*
=====================  ==========================  ==================================

An expression is *validated* when it is a call to ``sanitize_label`` /
``sanitize_database_name`` / ``cypher_number``, a string literal, a name
bound from a validated expression in the same lexical scope (its function,
an enclosing function, or the module — never a sibling function), a ``self``
attribute assigned from one anywhere in the module, a same-module function
whose own ``return`` statements (nested defs excluded) are all validated, an
f-string / ``str.join`` / comprehension built only from validated parts, or a
loop variable over a validated collection.

An expression is a *raw domain-spec value* when it reads an attribute (or
``.get``) off ``spec``, ``gate``, ``job_spec``, ``dim``, ``metadata`` and
friends without passing through a validator — the shape that turns untrusted
YAML into Cypher.

``int(...)`` / ``float(...)`` casts and lookups in a literal allow-list
(``_OPERATORS[gate.operator]``) are validated too: neither can carry Cypher.

Which f-strings are Cypher is decided by context, not by a short keyword list:
the literal text (with each interpolation replaced by a placeholder) contains a
Cypher clause or structural shape (``-[:``, ``]->``, ``candidate.``, ``$x``),
**or** the f-string flows to a query sink (``execute_query``, ``cypher=``),
**or** it is built inside a compiler / assembler function, assigned to a
``cypher``/``clause``/``fragment``-named variable, or appended to a
``*_exprs`` / ``clauses`` / ``cases`` list. A bare ``f"{prop} {op} {param}"``
fragment inside ``compile()`` is therefore scanned even though it contains no
keyword at all.

Diagnostic f-strings (inside ``raise``, exception constructors, logger calls,
``warnings.append``, ``reason=`` / ``detail=`` keywords and assignments to
``msg``-like names) are not Cypher and are skipped by AST context, never by
line regex.

The same classification applies to Cypher built by string concatenation
(``"MATCH (n:" + label + ")"``), ``str.format`` and ``%`` formatting: each
operand / argument is classified by the literal text before its placeholder.
A ``str.format`` field's ``.attr`` / ``[key]`` access (``{0.threshold}``) is
classified as the accessed value, and a mapping placeholder (``%(key)s`` over a
runtime mapping, ``{key}`` over ``**mapping``) as ``mapping[key]``.

Name facts are flow-aware: the bindings that reach a use are the last
binding that dominates it in its block plus any later (conditional) ones, and
a name with any reaching raw binding is raw even if another reaching binding
is validated (raw wins on conflict). A same-module function certifies only a
direct call (``helper(x)``) or a ``self.helper(x)`` method call, never an
attribute call on another object (``spec.get(...)`` is not ``def get``).

An assignment to a ``msg``-like name is skipped only when its text is not
query-shaped (no ``(n:X)`` / ``-[`` / ``]->`` structure and fewer than two
clause keywords).

A finding may be waived only with an explicit, reasoned trailing comment on
the interpolation's line — ``# cypher-lint: allow <reason>``. Waivers are
printed on every run so they are never silent, and a marker without a reason
is ignored.
"""

from __future__ import annotations

import ast
import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path

SANITIZERS = frozenset({"sanitize_label", "sanitize_database_name", "cypher_number"})
NUMERIC_CASTS = frozenset({"int", "float"})

# Names whose attributes / items are domain-spec / payload data, never Cypher-safe.
RAW_ROOTS = frozenset(
    {
        "spec",
        "gate",
        "gate_spec",
        "job_spec",
        "dim",
        "dimension",
        "domain_spec",
        "metadata",
        "payload",
        "config",
        "params",
        "_spec",
        "_domain_spec",
        "_causal_spec",
        "causal_spec",
        "scoring_spec",
    }
)

LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
MESSAGE_NAMES = frozenset(
    {"msg", "message", "detail", "reason", "hint", "description", "warning", "note", "text", "summary", "error"}
)
MESSAGE_LISTS = frozenset(
    {"warnings", "errors", "messages", "issues", "problems", "findings", "violations", "reasons", "notes"}
)
MESSAGE_KWARGS = MESSAGE_NAMES | frozenset({"resource", "title", "gate_impact", "crm_field_name"})

_LIMIT_SKIP_RE = re.compile(r"\b(?:LIMIT|SKIP)$")
_WAIVER_RE = re.compile(r"#\s*cypher-lint:\s*allow\b\s*(?P<reason>.*)$")

# Cypher-likeness of the literal text, interpolations replaced by "X".
_CYPHER_TEXT_RE = re.compile(
    r"\b(?:MATCH|MERGE|CREATE|DELETE|DETACH|SET|REMOVE|WHERE|WITH|RETURN|CALL|YIELD|UNWIND|LIMIT|SKIP"
    r"|ORDER BY|UNION|FOREACH|EXISTS|OPTIONAL|CASE|WHEN|THEN|ELSE|END|AND|OR|NOT|XOR|IS NULL|IS NOT NULL"
    r"|DISTINCT|coalesce|toFloat|toString|toInteger|datetime|duration|point)\b"
    r"|-\[|\]->|<-\[|\bcandidate\.|\$query\.|\$X\b|\(\s*\w*\s*:\s*X"
)
_FRAGMENT_FUNC_RE = re.compile(
    r"compile|assemble|cypher|query|clause|predicate|fragment|expr|where|_ref$|_pattern|render|generate",
    re.IGNORECASE,
)
_FRAGMENT_NAME_RE = re.compile(
    r"cypher|query|clause|fragment|predicate|expr|pattern|statement|stmt|where|match|filter|case",
    re.IGNORECASE,
)
_FRAGMENT_LIST_RE = re.compile(
    r"exprs|clauses|parts|cases|statements|fragments|predicates|conditions|filters|lines", re.IGNORECASE
)
# A message-named assignment is still scanned when its text is query-shaped.
_QUERY_STRUCTURE_RE = re.compile(r"\(\s*\w*\s*:\s*X|-\[|\]->|<-\[")
_CLAUSE_KEYWORD_RE = re.compile(
    r"\b(?:OPTIONAL MATCH|MATCH|MERGE|CREATE|DETACH DELETE|DELETE|SET|REMOVE|WHERE|WITH|RETURN|CALL|YIELD"
    r"|UNWIND|ORDER BY|FOREACH|UNION)\b"
)
_PERCENT_SPEC_RE = re.compile(
    r"%(?:\((?P<key>[^)]*)\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?(?P<conv>[diouxXeEfFgGcrsa%])"
)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
SINK_CALLS = frozenset({"execute_query", "execute_write", "run", "_raw_execute_query", "_raw_execute_write"})
SINK_KWARGS = frozenset({"cypher", "query", "statement"})


@dataclass(frozen=True)
class Finding:
    rel_path: str
    line_no: int
    pattern: str
    kind: str
    expression: str = ""
    waiver: str | None = None

    @property
    def blocking(self) -> bool:
        return self.waiver is None


# ── module facts ────────────────────────────────────────────────────────────


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_STOP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(root: ast.AST):
    """Yield the nodes of ``root``'s own body, not descending into nested scopes."""
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_STOP):
            stack.extend(ast.iter_child_nodes(node))


@dataclass(frozen=True, eq=False)
class _Binding:
    """One binding of a name: the statement, the bound value and where it takes effect."""

    scope: _Scope
    stmt: ast.AST
    value: ast.expr
    kind: str  # "assign" | "loop" | "comp" | "walrus"
    effect: tuple[int, int]


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _target_names(elt)]
    return []


def _end(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "end_lineno", None) or 0, getattr(node, "end_col_offset", None) or 0)


class _Scope:
    """Name bindings and attribute assignments for one lexical scope (module or function)."""

    def __init__(self, node: ast.AST, parent: _Scope | None) -> None:
        self.node = node
        self.parent = parent
        self.bindings: dict[str, list[_Binding]] = {}
        self.assignments: list[tuple[ast.expr, ast.expr]] = []
        self.loops: list[tuple[ast.expr, ast.expr]] = []

    def bind(self, binding: _Binding, target: ast.expr) -> None:
        for name in _target_names(target):
            self.bindings.setdefault(name, []).append(binding)

    def chain(self):
        scope: _Scope | None = self
        while scope is not None:
            yield scope
            scope = scope.parent


class _ModuleFacts:
    """Which names, ``self`` attributes and functions of a module are validated / raw.

    Name facts are lexically scoped: a binding made inside one function is
    visible in that function and its nested functions, never in a sibling —
    so ``label = sanitize_label(x)`` in one method cannot certify a bare
    ``{label}`` in another. ``self`` attributes are object state and stay
    module-wide. Name facts are flow-aware (see :meth:`_reaching`); ``self``
    attribute facts are not.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.safe_attrs: set[str] = set()
        self.raw_attrs: set[str] = set()
        self.safe_funcs: set[str] = set()
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self._scopes: dict[ast.AST, _Scope] = {tree: _Scope(tree, None)}
        for node in ast.walk(tree):
            if isinstance(node, _SCOPE_NODES):
                self._scopes[node] = _Scope(node, None)
        for node, scope in self._scopes.items():
            if node is not tree:
                # A nested function's parent scope is the function that
                # contains it (closures see enclosing bindings), else the
                # scope that contains the parent node.
                parent_node = self.parents[node]
                scope.parent = self._scopes.get(parent_node) or self.scope_of(parent_node)
        self._functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                scope = self.scope_of(node)
                scope.assignments.extend((target, node.value) for target in node.targets)
                binding = _Binding(scope, node, node.value, "assign", _end(node))
                for target in node.targets:
                    scope.bind(binding, target)
            elif (isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None) or isinstance(
                node, ast.NamedExpr
            ):
                scope = self.scope_of(node)
                scope.assignments.append((node.target, node.value))
                kind = "walrus" if isinstance(node, ast.NamedExpr) else "assign"
                scope.bind(_Binding(scope, node, node.value, kind, _end(node)), node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                scope = self.scope_of(node)
                scope.loops.append((node.target, node.iter))
                kind = "comp" if isinstance(node, ast.comprehension) else "loop"
                scope.bind(_Binding(scope, node, node.iter, kind, _end(node.iter)), node.target)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._functions.append(node)
        self._generation = 0
        self._name_memo: dict[int, tuple[int, tuple[bool, bool]]] = {}
        self._in_progress: set[int] = set()
        self._cycle_hit = False
        self._fixpoint()

    def scope_of(self, node: ast.AST) -> _Scope:
        """The lexical scope a node's bindings belong to (its nearest enclosing function, else module)."""
        current: ast.AST | None = node
        while current is not None:
            if current in self._scopes and current is not node:
                return self._scopes[current]
            current = self.parents.get(current)
        return next(iter(self._scopes.values()))

    def _fixpoint(self) -> None:
        """Grow ``self`` attribute and helper-function facts to a fixpoint.

        Name facts are derived on demand from the reaching bindings; they are
        memoised per generation, and the generation advances whenever an
        attribute or function fact changes.
        """
        changed = True
        while changed:
            changed = False
            for scope in self._scopes.values():
                for target, value in [*scope.assignments, *scope.loops]:
                    if self.is_safe(value, scope):
                        changed |= self._mark(target, self.safe_attrs)
                    if self.is_raw(value, scope):
                        changed |= self._mark(target, self.raw_attrs)
            for fn in self._functions:
                if fn.name in self.safe_funcs:
                    continue
                # Only the function's own returns count — a validated return
                # inside a nested helper says nothing about the outer function.
                returns = [n.value for n in _own_nodes(fn) if isinstance(n, ast.Return) and n.value is not None]
                fn_scope = self._scopes[fn]
                if returns and all(self.is_safe(r, fn_scope) for r in returns):
                    self.safe_funcs.add(fn.name)
                    changed = True
            if changed:
                self._generation += 1

    @staticmethod
    def _mark(target: ast.expr, attrs: set[str]) -> bool:
        """Record a ``self.<attr>`` target (names are resolved by flow, not marked)."""
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            if target.attr in attrs:
                return False
            attrs.add(target.attr)
            return True
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(_ModuleFacts._mark(elt, attrs) for elt in target.elts)
        return False

    # -- flow-aware name facts --

    def _ancestors(self, node: ast.AST):
        current = self.parents.get(node)
        while current is not None:
            yield current
            current = self.parents.get(current)

    def _dominates(self, binding: _Binding, use: ast.AST) -> bool:
        """Does *binding* execute unconditionally before *use* (same block, earlier statement)?"""
        stmt = binding.stmt
        if binding.kind in {"comp", "walrus"}:
            return False
        if binding.kind == "loop":
            body = getattr(stmt, "body", [])
            return any(anc in body for anc in self._ancestors(use)) if body else False
        container = self.parents.get(stmt)
        if container is None:
            return False
        for field in ("body", "orelse", "finalbody"):
            block = getattr(container, field, None)
            if isinstance(block, list) and any(item is stmt for item in block):
                index = next(i for i, item in enumerate(block) if item is stmt)
                later = block[index + 1 :]
                return any(any(anc is item for item in later) for anc in self._ancestors(use))
        return False

    def _reaching(self, use: ast.Name, use_scope: _Scope, def_scope: _Scope) -> list[_Binding]:
        """The bindings of ``use.id`` that may reach *use*."""
        binds = def_scope.bindings[use.id]
        # Comprehension targets bind only inside their own comprehension.
        for anc in self._ancestors(use):
            if isinstance(anc, _COMPREHENSIONS):
                own = [b for b in binds if b.kind == "comp" and any(b.stmt is g for g in anc.generators)]
                if own:
                    return own
        binds = [b for b in binds if b.kind != "comp"]
        if def_scope is not use_scope:
            return binds  # closure / module global: no flow information
        pos = (use.lineno, use.col_offset)
        preceding = [b for b in binds if b.effect <= pos]
        if not preceding:
            return binds  # loop-carried or bound later: every binding may reach
        dominating = [b for b in preceding if self._dominates(b, use)]
        if dominating:
            last = max(dominating, key=lambda b: b.effect)
            return [b for b in preceding if b.effect >= last.effect]
        return preceding

    def _name_facts(self, use: ast.Name, scope: _Scope) -> tuple[bool, bool]:
        """(validated, raw) for a name use. Raw wins: a name with any reaching raw binding is not validated."""
        key = id(use)
        cached = self._name_memo.get(key)
        if cached is not None and cached[0] == self._generation:
            return cached[1]
        if key in self._in_progress:
            self._cycle_hit = True
            return (False, False)
        def_scope = next((s for s in scope.chain() if use.id in s.bindings), None)
        if def_scope is None:
            return (False, False)
        outer_cycle = self._cycle_hit
        self._cycle_hit = False
        self._in_progress.add(key)
        try:
            reaching = self._reaching(use, scope, def_scope)
            raw = any(self.is_raw(b.value, b.scope) for b in reaching)
            safe = not raw and any(self.is_safe(b.value, b.scope) for b in reaching)
        finally:
            self._in_progress.discard(key)
        result = (safe, raw)
        if not self._cycle_hit:
            self._name_memo[key] = (self._generation, result)
        self._cycle_hit = outer_cycle or self._cycle_hit
        return result

    @staticmethod
    def _is_local_call(call: ast.Call) -> bool:
        """A call that can resolve to a same-module function: ``helper(x)`` or ``self.helper(x)``."""
        func = call.func
        if isinstance(func, ast.Name):
            return True
        return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self"

    # -- validated? --

    def is_safe(self, expr: ast.expr, scope: _Scope) -> bool:
        if isinstance(expr, ast.Constant):
            return isinstance(expr.value, str)
        if isinstance(expr, ast.Name):
            return self._name_facts(expr, scope)[0]
        if isinstance(expr, ast.Attribute):
            return isinstance(expr.value, ast.Name) and expr.value.id == "self" and expr.attr in self.safe_attrs
        if isinstance(expr, ast.Call):
            name = _call_name(expr)
            if name in SANITIZERS or (name in self.safe_funcs and self._is_local_call(expr)):
                return True
            if name in NUMERIC_CASTS and isinstance(expr.func, ast.Name):
                return True  # a number cannot carry Cypher
            # ", ".join(<validated iterable>)
            if (
                isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "join"
                and isinstance(expr.func.value, ast.Constant)
                and len(expr.args) == 1
            ):
                return self.is_safe(expr.args[0], scope)
            return False
        if isinstance(expr, ast.JoinedStr):
            return all(self.is_safe(v.value, scope) for v in expr.values if isinstance(v, ast.FormattedValue))
        if isinstance(expr, ast.FormattedValue):
            return self.is_safe(expr.value, scope)
        if isinstance(expr, ast.BoolOp):
            return all(self.is_safe(v, scope) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return self.is_safe(expr.body, scope) and self.is_safe(expr.orelse, scope)
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self.is_safe(expr.elt, scope)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return bool(expr.elts) and all(self.is_safe(e, scope) for e in expr.elts)
        if isinstance(expr, ast.Dict):
            # a literal allow-list: `_OPERATORS[gate.operator]` raises on anything else
            return bool(expr.values) and all(v is not None and self.is_safe(v, scope) for v in expr.values)
        if isinstance(expr, ast.Subscript):
            return self.is_safe(expr.value, scope)
        return False

    # -- raw domain-spec value? --

    def is_raw(self, expr: ast.expr, scope: _Scope) -> bool:
        if isinstance(expr, ast.Name):
            return self._name_facts(expr, scope)[1]
        if isinstance(expr, ast.Attribute):
            root = _attribute_root(expr)
            if root in RAW_ROOTS:
                return True
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return expr.attr in self.raw_attrs
            return _attribute_chain_has_raw_segment(expr)
        if isinstance(expr, ast.Call):
            name = _call_name(expr)
            if name in SANITIZERS or name in NUMERIC_CASTS or (name in self.safe_funcs and self._is_local_call(expr)):
                return False
            if isinstance(expr.func, ast.Attribute) and expr.func.attr in {"get", "pop", "strip", "lower", "upper"}:
                return self.is_raw(expr.func.value, scope)
            if isinstance(expr.func, ast.Name) and expr.func.id == "str":
                return bool(expr.args) and self.is_raw(expr.args[0], scope)
            return False
        if isinstance(expr, ast.BoolOp):
            return any(self.is_raw(v, scope) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return self.is_raw(expr.body, scope) or self.is_raw(expr.orelse, scope)
        if isinstance(expr, ast.Subscript):
            if isinstance(expr.value, ast.Name) and expr.value.id in RAW_ROOTS:
                return True
            return self.is_raw(expr.value, scope)
        if isinstance(expr, ast.JoinedStr):
            return any(self.is_raw(v.value, scope) for v in expr.values if isinstance(v, ast.FormattedValue))
        return False


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _attribute_root(expr: ast.Attribute) -> str:
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _attribute_chain_has_raw_segment(expr: ast.Attribute) -> bool:
    """``self.spec.edgetype`` / ``self.domain_spec.domain.id`` → the chain passes through a raw segment."""
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        if node.attr in RAW_ROOTS:
            return True
        node = node.value
    return False


# ── message context ─────────────────────────────────────────────────────────


def _looks_like_query(text: str) -> bool:
    """Query-shaped literal text: a node/relationship pattern or two distinct clause keywords."""
    if _QUERY_STRUCTURE_RE.search(text):
        return True
    return len(set(_CLAUSE_KEYWORD_RE.findall(text))) >= 2


def _is_diagnostic_use(use: ast.Name, parents: dict[ast.AST, ast.AST]) -> bool:
    """A read of a message name that only reaches ``raise`` / an exception or a logger call."""
    parent = parents.get(use)
    if isinstance(parent, ast.Raise):
        return True
    if isinstance(parent, ast.Call) and use in parent.args:
        name = _call_name(parent)
        return name.endswith(("Error", "Exception", "Warning")) or name in LOG_METHODS
    return False


def _only_diagnostic_uses(assign: ast.AST, target: str, parents: dict[ast.AST, ast.AST]) -> bool:
    """Every read of ``target`` in the enclosing function is diagnostic (and there is at least one)."""
    scope: ast.AST | None = assign
    while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        scope = parents.get(scope)
    if scope is None:
        return False
    uses = [n for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == target and isinstance(n.ctx, ast.Load)]
    return bool(uses) and all(_is_diagnostic_use(u, parents) for u in uses)


def _is_message_context(node: ast.AST, parents: dict[ast.AST, ast.AST], text: str = "") -> bool:
    """Diagnostic text, not Cypher: skip by AST context."""
    child: ast.AST = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.Raise):
            return True
        if isinstance(parent, ast.keyword) and parent.arg in MESSAGE_KWARGS:
            return True
        if isinstance(parent, ast.Dict):
            for key, value in zip(parent.keys, parent.values, strict=True):
                if value is child and isinstance(key, ast.Constant) and key.value in MESSAGE_NAMES:
                    return True
        if isinstance(parent, ast.Call):
            name = _call_name(parent)
            if name in SANITIZERS and child in parent.args:
                return True  # sanitize_label(f"_prior_{dim.name}") — validated by the call around it
            if name in LOG_METHODS or name in {"print", "warn"}:
                return True
            if name.endswith(("Error", "Exception", "Warning")):
                return True
            if (
                name in {"append", "extend"}
                and isinstance(parent.func, ast.Attribute)
                and isinstance(parent.func.value, ast.Name)
                and parent.func.value.id in MESSAGE_LISTS
            ):
                return True
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            names = [t.id for t in targets if isinstance(t, ast.Name) and t.id in MESSAGE_NAMES]
            if names:
                # A message-like name does not hide a query-shaped string, unless every
                # read of it only reaches an exception or a logger (it is never executed).
                return not _looks_like_query(text) or all(_only_diagnostic_uses(parent, n, parents) for n in names)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return False
        child = parent
    return False


def _is_cypher_candidate(node: ast.AST, parents: dict[ast.AST, ast.AST], text: str) -> bool:
    """Does this string template build Cypher? Decided by its text *or* by where it flows."""
    if _CYPHER_TEXT_RE.search(text):
        return True
    child: ast.AST = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.keyword) and parent.arg in SINK_KWARGS:
            return True
        if isinstance(parent, ast.Call):
            name = _call_name(parent)
            if name in SINK_CALLS:
                return True
            if (
                name in {"append", "extend"}
                and isinstance(parent.func, ast.Attribute)
                and isinstance(parent.func.value, ast.Name)
                and _FRAGMENT_LIST_RE.search(parent.func.value.id)
            ):
                return True
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            if any(
                (isinstance(t, ast.Name) and _FRAGMENT_NAME_RE.search(t.id))
                or (isinstance(t, ast.Attribute) and _FRAGMENT_NAME_RE.search(t.attr))
                for t in targets
            ):
                return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return bool(_FRAGMENT_FUNC_RE.search(parent.name))
        if isinstance(parent, ast.Module):
            return False
        child = parent
    return False


# ── classification ──────────────────────────────────────────────────────────


def _classify(before: str, expr: ast.expr, facts: _ModuleFacts, scope: _Scope) -> str | None:
    """Return the failure kind for one interpolation, or None when it is acceptable."""
    tail = before[-1:] if before else ""
    if tail in {"'", '"'}:
        return "quoted value interpolation — pass the value as a $parameter"
    if tail == "`":
        if not facts.is_safe(expr, scope):
            return "back-quoted identifier is not validated (sanitize_database_name / sanitize_label)"
        return None
    if tail == "$":
        if not facts.is_safe(expr, scope):
            return "parameter name is not validated — derive it from sanitize_label() or a literal"
        return None
    if tail == ":":
        if not facts.is_safe(expr, scope):
            return "label / relationship type is not validated (sanitize_label)"
        return None
    if tail == "." and not before.endswith(".."):
        if not facts.is_safe(expr, scope):
            return "property name is not validated (sanitize_label)"
        return None
    if _LIMIT_SKIP_RE.search(before.rstrip()):
        return "LIMIT / SKIP bound interpolated — pass it as a $parameter"
    if facts.is_safe(expr, scope):
        return None
    if facts.is_raw(expr, scope):
        return "raw domain-spec value interpolated into Cypher — sanitize_label() it or pass it as a $parameter"
    return None


# A string template: its literal text (placeholders as "X") and, per
# interpolated operand, the literal text immediately before it.
_Template = tuple[str, list[tuple[str, ast.expr]]]


def _fstring_template(node: ast.JoinedStr) -> _Template:
    text, parts, before = "", [], ""
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            text += value.value
            before = value.value
        elif isinstance(value, ast.FormattedValue):
            text += "X"
            parts.append((before, value.value))
            before = ""
    return text, parts


def _fstring_text(node: ast.JoinedStr) -> str:
    return _fstring_template(node)[0]


def _trailing_literal(node: ast.JoinedStr) -> str:
    last = node.values[-1] if node.values else None
    return last.value if isinstance(last, ast.Constant) and isinstance(last.value, str) else ""


def _flatten_add(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_flatten_add(node.left), *_flatten_add(node.right)]
    return [node]


def _str_value(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _concat_template(node: ast.BinOp) -> _Template | None:
    operands = _flatten_add(node)
    if not any(_str_value(op) is not None or isinstance(op, ast.JoinedStr) for op in operands):
        return None  # numeric or list addition
    text, parts, before = "", [], ""
    for op in operands:
        literal = _str_value(op)
        if literal is not None:
            text += literal
            before = literal
        elif isinstance(op, ast.JoinedStr):
            # Its own interpolations are scanned with the f-string itself.
            text += _fstring_text(op)
            before = _trailing_literal(op)
        else:
            text += "X"
            parts.append((before, op))
            before = ""
    return text, parts


def _item(mapping: ast.expr, key: str | int) -> ast.expr:
    """``mapping[key]`` as an expression node located at *mapping*."""
    node = ast.Subscript(value=mapping, slice=ast.Constant(key), ctx=ast.Load())
    return ast.copy_location(node, mapping)


def _mapping_items(mapping: ast.expr, key: str) -> list[ast.expr]:
    """The expressions a ``%(key)s`` placeholder may interpolate from *mapping*.

    A literal dict yields the value under *key*, or an item of any ``**`` entry
    that may supply or override it; any other operand is a runtime mapping and
    yields ``mapping[key]``.
    """
    if not isinstance(mapping, ast.Dict):
        return [_item(mapping, key)]
    found: list[ast.expr] = []
    for k, v in zip(mapping.keys, mapping.values, strict=True):
        if k is None:
            found.append(_item(v, key))
        elif _str_value(k) == key:
            found = [v]  # a later literal entry overrides everything before it
    return found


_FIELD_ACCESS_RE = re.compile(r"\.(?P<attr>[^.\[]+)|\[(?P<key>[^\]]+)\]")


def _field_access(arg: ast.expr, access: str) -> ast.expr:
    """Apply a format field's ``.attr`` / ``[key]`` chain (``{0.a[b]}``) to its argument."""
    node, pos = arg, 0
    while pos < len(access):
        match = _FIELD_ACCESS_RE.match(access, pos)
        if match is None:
            return _item(node, access[pos:])  # unparseable tail: still an item of the argument
        if match.group("attr") is not None:
            node = ast.copy_location(ast.Attribute(value=node, attr=match.group("attr"), ctx=ast.Load()), arg)
        else:
            key = match.group("key")
            node = _item(node, int(key) if key.isdigit() else key)
        pos = match.end()
    return node


def _percent_template(node: ast.BinOp) -> _Template | None:
    fmt = _str_value(node.left)
    if fmt is None:
        return None
    right = node.right
    positional = right.elts if isinstance(right, ast.Tuple) else None
    text: str = ""
    parts: list[tuple[str, ast.expr]] = []
    pos, index = 0, 0
    for match in _PERCENT_SPEC_RE.finditer(fmt):
        literal = fmt[pos : match.start()]
        text += literal
        pos = match.end()
        if match.group("conv") == "%":
            text += "%"
            continue
        text += "X"
        key = match.group("key")
        args: list[ast.expr]
        if key is not None:
            # A literal dict, or a runtime mapping (`% params`) read as params[key].
            args = _mapping_items(right, key)
        elif positional is not None:
            args = [positional[index]] if index < len(positional) else []
            index += 1
        else:
            args = [right] if index == 0 else []
            index += 1
        parts.extend((literal, arg) for arg in args if not isinstance(arg, ast.Starred))
    text += fmt[pos:]
    return text, parts


def _format_template(node: ast.Call) -> _Template | None:
    func = node.func
    fmt = _str_value(func.value) if isinstance(func, ast.Attribute) and func.attr == "format" else None
    if fmt is None:
        return None
    try:
        fields = list(string.Formatter().parse(fmt))
    except ValueError:
        return None
    star_args = any(isinstance(a, ast.Starred) for a in node.args)
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
    unpacked = [kw.value for kw in node.keywords if kw.arg is None]
    text: str = ""
    parts: list[tuple[str, ast.expr]] = []
    auto = 0
    for literal, field_name, _spec, _conv in fields:
        text += literal
        if field_name is None:
            continue
        text += "X"
        base = re.split(r"[.\[]", field_name, maxsplit=1)[0]
        access = field_name[len(base) :]
        args: list[ast.expr] = []
        if base == "":
            if not star_args and auto < len(node.args):
                args = [node.args[auto]]
            auto += 1
        elif base.isdigit():
            i = int(base)
            if not star_args and i < len(node.args):
                args = [node.args[i]]
        elif base in keywords:
            args = [keywords[base]]
        else:
            # `.format(**params)`: the field reads params[base].
            args = [_item(mapping, base) for mapping in unpacked]
        # `{0.threshold}` / `{0[key]}` interpolate the accessed value, as an f-string would.
        parts.extend((literal, _field_access(arg, access)) for arg in args)
    return text, parts


def _template(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> _Template | None:
    """The string template a node builds, if it is one the scanner classifies."""
    if isinstance(node, ast.JoinedStr):
        if isinstance(parents.get(node), ast.FormattedValue):
            return None  # nested format spec of another f-string
        return _fstring_template(node)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            parent = parents.get(node)
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
                return None  # inner link of a concatenation chain; the top is scanned
            return _concat_template(node)
        if isinstance(node.op, ast.Mod):
            return _percent_template(node)
        return None
    if isinstance(node, ast.Call):
        return _format_template(node)
    return None


def scan_source(source: str, *, rel_path: str) -> list[Finding]:
    """Scan one module's source text."""
    tree = ast.parse(source, filename=rel_path)
    facts = _ModuleFacts(tree)
    parents = facts.parents
    lines = source.splitlines()

    findings: list[Finding] = []
    for node in ast.walk(tree):
        template = _template(node, parents)
        if template is None:
            continue
        text, parts = template
        if not parts:
            continue
        if _is_message_context(node, parents, text) or not _is_cypher_candidate(node, parents, text):
            continue
        scope = facts.scope_of(node)
        for before, expr in parts:
            kind = _classify(before, expr, facts, scope)
            if kind is None:
                continue
            line_no = getattr(expr, "lineno", getattr(node, "lineno", 0))
            line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            findings.append(
                Finding(
                    rel_path=rel_path,
                    line_no=line_no,
                    pattern=line.strip(),
                    kind=kind,
                    expression=ast.unparse(expr),
                    waiver=_waiver(line),
                )
            )
    return findings


def _waiver(line: str) -> str | None:
    """An explicit ``# cypher-lint: allow <reason>`` on the line; a bare marker is not a waiver."""
    match = _WAIVER_RE.search(line)
    if match is None:
        return None
    reason = match.group("reason").strip(" -—:")
    return reason or None


def scan_file(path: Path, *, root: Path) -> list[Finding]:
    return scan_source(path.read_text(encoding="utf-8"), rel_path=path.relative_to(root).as_posix())


def scan_tree(root: Path) -> list[Finding]:
    engine = root / "engine"
    if not engine.is_dir():
        return []
    findings: list[Finding] = []
    for path in sorted(engine.rglob("*.py")):
        if path.is_file():
            findings.extend(scan_file(path, root=root))
    return findings


def render_findings(findings: list[Finding]) -> str:
    blocks = []
    for item in findings:
        head = f"❌ {item.kind}" if item.blocking else f"⚠️ waived ({item.waiver}) — {item.kind}"
        blocks.append(
            f"{head}\nFile: {item.rel_path}:{item.line_no}\nExpression: {{{item.expression}}}\nPattern: {item.pattern}"
        )
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path.cwd()
    findings = scan_tree(root)
    blocking = [f for f in findings if f.blocking]
    waived = [f for f in findings if not f.blocking]
    if waived:
        print(render_findings(waived), end="\n\n")
    if blocking:
        print(render_findings(blocking))
        print(f"\n{len(blocking)} C-009 finding(s), {len(waived)} waived")
        return 1
    print(f"OK: cypher-lint — 0 injection vectors ({len(waived)} waived, listed above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
