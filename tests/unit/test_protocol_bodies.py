"""Protocol method bodies must stay lint-clean on every in-repo gate (CEG#267)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "engine"


def _protocol_methods(path: Path) -> list[tuple[str, str, ast.AST]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            (isinstance(base, ast.Name) and base.id == "Protocol")
            or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
            for base in node.bases
        ):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((path.as_posix(), f"{node.name}.{item.name}", item))
    return found


def _engine_protocol_methods() -> list[tuple[str, str, ast.AST]]:
    rows: list[tuple[str, str, ast.AST]] = []
    for path in sorted(ENGINE.rglob("*.py")):
        rows.extend(_protocol_methods(path))
    return rows


@pytest.mark.unit
def test_engine_has_protocol_methods() -> None:
    assert _engine_protocol_methods(), "expected at least one engine Protocol method"


@pytest.mark.unit
def test_protocol_methods_have_no_executable_stub() -> None:
    failures: list[str] = []
    for rel, qualname, fn in _engine_protocol_methods():
        executable = []
        for stmt in fn.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            executable.append(stmt)
        for stmt in executable:
            if isinstance(stmt, ast.Pass):
                failures.append(f"{rel}:{qualname} uses pass (ruff PIE790)")
            elif isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
                func = stmt.exc.func
                name = func.id if isinstance(func, ast.Name) else ""
                if name == "NotImplementedError":
                    failures.append(f"{rel}:{qualname} raises NotImplementedError (STUB-001)")
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                failures.append(f"{rel}:{qualname} uses ... (github-code-quality no-op)")
    assert failures == [], "\n".join(failures)
