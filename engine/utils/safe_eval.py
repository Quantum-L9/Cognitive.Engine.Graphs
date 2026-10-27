"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [utils, safe-eval]
owner: engine-team
status: active
--- /L9_META ---

engine/utils/safe_eval.py
AST-whitelist expression evaluator. Replaces eval() for derived parameters
and unit conversion formulas.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

_SAFE_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_SAFE_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "log": math.log,
    "exp": math.exp,
    "sqrt": math.sqrt,
}


_MAX_EVAL_DEPTH = 20
_MAX_POW_EXPONENT = 100
# RU-070 / CEG-COR-062: resource bounds. An integer operand or result may carry
# at most _MAX_INT_BITS bits (~1233 decimal digits; no shipped expression comes
# close), checked BEFORE a power is computed, and round() accepts at most
# _MAX_ROUND_NDIGITS digits either side of the point. Without them
# `(((10**100)**100)**100)**100` and `round(1, -10**8)` did not terminate.
_MAX_INT_BITS = 4096
_MAX_ROUND_NDIGITS = 100


def _check_magnitude(value: Any) -> Any:
    """Raise ValueError when an integer exceeds the bit bound; return the value."""
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > _MAX_INT_BITS:
        msg = f"Integer magnitude exceeds {_MAX_INT_BITS} bits"
        raise ValueError(msg)
    return value


def _is_inf(value: Any) -> bool:
    return isinstance(value, float) and math.isinf(value)


def _guarded_pow(base: Any, exp: Any) -> Any:
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and abs(base) > 1:
        if base.bit_length() * exp > _MAX_INT_BITS:
            msg = f"Power result would exceed {_MAX_INT_BITS} bits"
            raise ValueError(msg)
    return _check_magnitude(operator.pow(base, exp))


def safe_eval(expression: str, context: dict[str, Any]) -> Any:
    """
    Evaluate a simple algebraic expression with variable substitution.
    Only allows arithmetic operations, numeric literals, and whitelisted functions.

    Raises ValueError on disallowed AST node types.
    """
    tree = ast.parse(expression, mode="eval")
    try:
        return _eval_node(tree.body, context)
    except OverflowError as exc:
        # RU-070: float overflow (e.g. exp(1000), 1e308 * 10) is an invalid input.
        msg = f"Numeric overflow in expression: {exc}"
        raise ValueError(msg) from exc


def _eval_node(node: ast.AST, ctx: dict[str, Any], depth: int = 0) -> Any:
    if depth > _MAX_EVAL_DEPTH:
        msg = f"Expression too deeply nested (max depth {_MAX_EVAL_DEPTH})"
        raise ValueError(msg)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            msg = f"Only numeric constants allowed, got {type(node.value).__name__}"
            raise ValueError(msg)
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        if node.id not in ctx:
            msg = f"Unknown variable: {node.id!r}"
            raise ValueError(msg)
        return ctx[node.id]
    if isinstance(node, ast.BinOp):
        bin_op_type = type(node.op)
        if bin_op_type not in _SAFE_BINARY_OPS:
            msg = f"Unsupported operator: {bin_op_type.__name__}"
            raise ValueError(msg)
        if isinstance(node.op, ast.Pow):
            base = _check_magnitude(_eval_node(node.left, ctx, depth + 1))
            exp = _check_magnitude(_eval_node(node.right, ctx, depth + 1))
            if abs(exp) > _MAX_POW_EXPONENT:
                msg = f"Exponent too large: {exp} (max {_MAX_POW_EXPONENT})"
                raise ValueError(msg)
            return _guarded_pow(base, exp)
        bin_op_func = _SAFE_BINARY_OPS[bin_op_type]
        left = _check_magnitude(_eval_node(node.left, ctx, depth + 1))
        right = _check_magnitude(_eval_node(node.right, ctx, depth + 1))
        result = _check_magnitude(bin_op_func(left, right))
        if isinstance(result, float) and math.isinf(result) and not _is_inf(left) and not _is_inf(right):
            # Float arithmetic overflows to inf silently; surface it (RU-070).
            msg = "Numeric overflow in expression: float result is infinite"
            raise ValueError(msg)
        return result
    if isinstance(node, ast.UnaryOp):
        unary_op_type = type(node.op)
        if unary_op_type not in _SAFE_UNARY_OPS:
            msg = f"Unsupported unary operator: {unary_op_type.__name__}"
            raise ValueError(msg)
        unary_op_func = _SAFE_UNARY_OPS[unary_op_type]
        return unary_op_func(_eval_node(node.operand, ctx, depth + 1))
    if isinstance(node, ast.Call):
        return _eval_call(node, ctx, depth)
    msg = f"Disallowed expression node: {type(node).__name__}"
    raise ValueError(msg)


def _eval_call(node: ast.Call, ctx: dict[str, Any], depth: int) -> Any:
    """Evaluate a whitelisted function call (positional arguments only)."""
    func = _eval_node(node.func, ctx, depth + 1)
    if func not in _SAFE_FUNCS.values():
        msg = f"Function not whitelisted: {ast.dump(node.func)}"
        raise ValueError(msg)
    if node.keywords:
        from engine.config.settings import settings

        # RU-076 / CEG-COR-125: keywords were silently dropped
        # (round(2.567, ndigits=2) -> 3). Off: legacy drop.
        if settings.safe_eval_reject_keywords:
            msg = "keyword arguments not allowed"
            raise ValueError(msg)
    args = [_check_magnitude(_eval_node(a, ctx, depth + 1)) for a in node.args]
    if func is round and len(args) >= 2 and isinstance(args[1], (int, float)) and abs(args[1]) > _MAX_ROUND_NDIGITS:
        msg = f"round() ndigits out of range (max {_MAX_ROUND_NDIGITS})"
        raise ValueError(msg)
    return _check_magnitude(func(*args))
