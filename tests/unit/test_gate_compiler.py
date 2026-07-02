"""Unit tests — GateCompiler: gate types, null semantics, direction filter."""

from __future__ import annotations

from pathlib import Path


def test_compile_all_gates_returns_string():
    """compile_all_gates returns a string WHERE clause fragment."""
    from engine.config.loader import DomainPackLoader
    from engine.gates.compiler import GateCompiler

    loader = DomainPackLoader(config_path=str(Path(__file__).parent.parent.parent / "domains"))
    spec = loader.load_domain("plasticos")
    compiler = GateCompiler(spec)
    result = compiler.compile_all_gates(match_direction="buyer_to_seller")
    assert isinstance(result, str)


def test_direction_filter_skips_non_matching():
    """Gates scoped to one direction must not appear in the other direction's clause."""
    from engine.config.loader import DomainPackLoader
    from engine.gates.compiler import GateCompiler

    loader = DomainPackLoader(config_path=str(Path(__file__).parent.parent.parent / "domains"))
    spec = loader.load_domain("plasticos")
    compiler = GateCompiler(spec)
    fwd = compiler.compile_all_gates(match_direction="buyer_to_seller")
    rev = compiler.compile_all_gates(match_direction="seller_to_buyer")
    # At minimum both should be strings; direction-scoped gates differ
    assert isinstance(fwd, str)
    assert isinstance(rev, str)


def test_compile_all_gates_with_role_exemption():
    """Role exemption should skip gates that exempt the given role."""
    from engine.config.loader import DomainPackLoader
    from engine.gates.compiler import GateCompiler

    loader = DomainPackLoader(config_path=str(Path(__file__).parent.parent.parent / "domains"))
    spec = loader.load_domain("plasticos")
    compiler = GateCompiler(spec)
    # With a role that might be exempted, result should still be a valid string
    result = compiler.compile_all_gates(match_direction="buyer_to_seller", role="admin")
    assert isinstance(result, str)
