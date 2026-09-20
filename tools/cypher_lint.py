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

A finding may be waived only with an explicit, reasoned trailing comment on
the interpolation's line — ``# cypher-lint: allow <reason>``. Waivers are
printed on every run so they are never silent, and a marker without a reason
is ignored.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SANITIZERS = frozenset({"sanitize_label", "sanitize_database_name", "cypher_number"})
NUMERIC_CASTS = frozenset({"int", "float"})

# Names whose attributes are domain-spec / payload data, never Cypher-safe.
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


class _Scope:
    """Validated / raw name facts for one lexical scope (module or function)."""

    def __init__(self, node: ast.AST, parent: _Scope | None) -> None:
        self.node = node
        self.parent = parent
        self.safe_names: set[str] = set()
        self.raw_names: set[str] = set()
        self.assignments: list[tuple[ast.expr, ast.expr]] = []
        self.loops: list[tuple[ast.expr, ast.expr]] = []

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
    module-wide. Assignment order inside one scope is not tracked.
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
                self.scope_of(node).assignments.extend((target, node.value) for target in node.targets)
            elif (isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None) or isinstance(
                node, ast.NamedExpr
            ):
                self.scope_of(node).assignments.append((node.target, node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self.scope_of(node).loops.append((node.target, node.iter))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._functions.append(node)
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
        changed = True
        while changed:
            changed = False
            for scope in self._scopes.values():
                for target, value in scope.assignments:
                    if self.is_safe(value, scope):
                        changed |= self._mark(target, scope.safe_names, self.safe_attrs)
                    if self.is_raw(value, scope):
                        changed |= self._mark(target, scope.raw_names, self.raw_attrs)
                for target, iterable in scope.loops:
                    if self.is_safe(iterable, scope):
                        changed |= self._mark(target, scope.safe_names, self.safe_attrs)
                    if self.is_raw(iterable, scope):
                        changed |= self._mark(target, scope.raw_names, self.raw_attrs)
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

    @staticmethod
    def _mark(target: ast.expr, names: set[str], attrs: set[str]) -> bool:
        if isinstance(target, ast.Name):
            if target.id in names:
                return False
            names.add(target.id)
            return True
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            if target.attr in attrs:
                return False
            attrs.add(target.attr)
            return True
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(_ModuleFacts._mark(elt, names, attrs) for elt in target.elts)
        return False

    @staticmethod
    def _name_in(name: str, scope: _Scope, attr: str) -> bool:
        return any(name in getattr(s, attr) for s in scope.chain())

    # -- validated? --

    def is_safe(self, expr: ast.expr, scope: _Scope) -> bool:
        if isinstance(expr, ast.Constant):
            return isinstance(expr.value, str)
        if isinstance(expr, ast.Name):
            return self._name_in(expr.id, scope, "safe_names")
        if isinstance(expr, ast.Attribute):
            return isinstance(expr.value, ast.Name) and expr.value.id == "self" and expr.attr in self.safe_attrs
        if isinstance(expr, ast.Call):
            name = _call_name(expr)
            if name in SANITIZERS or name in self.safe_funcs:
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
            return self._name_in(expr.id, scope, "raw_names")
        if isinstance(expr, ast.Attribute):
            root = _attribute_root(expr)
            if root in RAW_ROOTS:
                return True
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return expr.attr in self.raw_attrs
            return _attribute_chain_has_raw_segment(expr)
        if isinstance(expr, ast.Call):
            name = _call_name(expr)
            if name in SANITIZERS or name in self.safe_funcs or name in NUMERIC_CASTS:
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


def _is_message_context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
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
            if any(isinstance(t, ast.Name) and t.id in MESSAGE_NAMES for t in targets):
                return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return False
        child = parent
    return False


def _is_cypher_candidate(node: ast.JoinedStr, parents: dict[ast.AST, ast.AST]) -> bool:
    """Does this f-string build Cypher? Decided by its text *or* by where it flows."""
    text = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "X" for v in node.values)
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


def scan_source(source: str, *, rel_path: str) -> list[Finding]:
    """Scan one module's source text."""
    tree = ast.parse(source, filename=rel_path)
    facts = _ModuleFacts(tree)
    parents = facts.parents
    lines = source.splitlines()

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        if isinstance(parents.get(node), ast.FormattedValue):
            continue  # nested format spec of another f-string
        if _is_message_context(node, parents) or not _is_cypher_candidate(node, parents):
            continue
        scope = facts.scope_of(node)
        before = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                before = value.value
                continue
            if not isinstance(value, ast.FormattedValue):
                continue
            kind = _classify(before, value.value, facts, scope)
            before = ""
            if kind is None:
                continue
            line_no = getattr(value.value, "lineno", node.lineno)
            line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            findings.append(
                Finding(
                    rel_path=rel_path,
                    line_no=line_no,
                    pattern=line.strip(),
                    kind=kind,
                    expression=ast.unparse(value.value),
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
