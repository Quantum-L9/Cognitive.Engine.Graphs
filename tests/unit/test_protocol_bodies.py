"""Protocol method bodies must be a docstring and nothing else (CEG#267).

`docs/contracts/BANNED_PATTERNS.md` — "typing.Protocol method bodies": a
Protocol method is a structural signature, never executed, and the only body
that is valid Python *and* clean on every in-repo gate is a docstring alone.
The check therefore strips an optional leading docstring and requires the
remaining body to be empty; **any** statement fails, with its location.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "engine"


def _is_protocol_class(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def protocol_body_violations(tree: ast.AST, path: str) -> list[str]:
    """Every statement after the optional docstring of every Protocol method, located."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_protocol_class(node):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = item.body[1:] if item.body and _is_docstring(item.body[0]) else list(item.body)
            violations.extend(
                f"{path}:{stmt.lineno}: {node.name}.{item.name} body must be a docstring only, "
                f"found {type(stmt).__name__} ({ast.unparse(stmt)})"
                for stmt in body
            )
    return violations


def _engine_protocol_methods() -> list[str]:
    found: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_protocol_class(node):
                found.extend(
                    f"{node.name}.{item.name}"
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
    return found


@pytest.mark.unit
def test_engine_has_protocol_methods() -> None:
    assert _engine_protocol_methods(), "expected at least one engine Protocol method"


@pytest.mark.unit
def test_engine_protocol_methods_are_docstring_only() -> None:
    failures: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(protocol_body_violations(tree, path.as_posix()))
    assert failures == [], "\n".join(failures)


# ── the checker itself must discriminate (F284-2) ───────────────────────────

_PROTOCOL = "from typing import Protocol\n\nclass P(Protocol):\n    def m(self) -> int:\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        '        """Doc."""\n',
        '        """Doc.\n\n        Multi-line.\n        """\n',
    ],
    ids=["docstring", "multiline-docstring"],
)
def test_docstring_only_bodies_pass(body: str) -> None:
    assert protocol_body_violations(ast.parse(_PROTOCOL + body), "p.py") == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('        """Doc."""\n        ...\n', "Expr (...)"),
        ('        """Doc."""\n        pass\n', "Pass (pass)"),
        ('        """Doc."""\n        raise NotImplementedError\n', "Raise (raise NotImplementedError)"),
        ('        """Doc."""\n        raise NotImplementedError("x")\n', "Raise (raise NotImplementedError('x'))"),
        ('        """Doc."""\n        raise RuntimeError("x")\n', "Raise (raise RuntimeError('x'))"),
        ('        """Doc."""\n        return None\n', "Return (return None)"),
        ('        """Doc."""\n        return 0\n', "Return (return 0)"),
        ('        """Doc."""\n        x = 1\n', "Assign (x = 1)"),
        ('        """Doc."""\n        self.x: int = 1\n', "AnnAssign (self.x: int = 1)"),
        ('        """Doc."""\n        print("x")\n', "Expr (print('x'))"),
        ('        """Doc."""\n        1 + 1\n', "Expr (1 + 1)"),
        ('        """Doc."""\n        if True:\n            pass\n', "If (if True:"),
        ("        ...\n", "Expr (...)"),
        ("        pass\n", "Pass (pass)"),
        ('        """Doc."""\n        """Second string is a statement."""\n', "Expr ('Second string is a statement.')"),
    ],
    ids=[
        "ellipsis",
        "pass",
        "raise-bare",
        "raise-not-implemented",
        "raise-other",
        "return-none",
        "return-value",
        "assign",
        "ann-assign",
        "call",
        "expression",
        "compound",
        "ellipsis-no-docstring",
        "pass-no-docstring",
        "second-string",
    ],
)
def test_any_non_docstring_statement_fails_with_location(body: str, expected: str) -> None:
    violations = protocol_body_violations(ast.parse(_PROTOCOL + body), "p.py")
    assert len(violations) == 1
    assert violations[0].startswith("p.py:")
    assert "P.m body must be a docstring only" in violations[0]
    assert expected in violations[0]


@pytest.mark.unit
def test_every_offending_statement_is_reported() -> None:
    body = '        """Doc."""\n        x = 1\n        return x\n'
    violations = protocol_body_violations(ast.parse(_PROTOCOL + body), "p.py")
    assert [v.split(": ", 1)[0] for v in violations] == ["p.py:6", "p.py:7"]


@pytest.mark.unit
def test_non_protocol_classes_are_not_checked() -> None:
    src = "class Base:\n    def m(self) -> int:\n        raise NotImplementedError\n"
    assert protocol_body_violations(ast.parse(src), "b.py") == []
