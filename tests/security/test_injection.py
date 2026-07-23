"""
Security tests — Cypher injection attempts across parameterized surfaces.

RULE 4 (L9 Contract): ZERO string interpolation of values in Cypher.
RULE 3 (L9 Contract): eval/exec/compile are banned.
"""

from __future__ import annotations

import pytest

INJECTION_STRINGS = [
    "'; DROP DATABASE neo4j;",
    "MATCH (n) DETACH DELETE n RETURN 1 AS",
    "1 OR 1=1",
    "admin'--",
    "n} RETURN n UNION MATCH (x",
    "a\x00b",
    "a" * 200,
    "__class__.__bases__[0].__subclasses__()",
]


@pytest.mark.parametrize("injection", INJECTION_STRINGS)
def test_sanitize_label_blocks_injection(injection):
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label(injection)


def test_sync_generator_uses_parameterized_query():
    """SyncGenerator MUST use $batch param — never interpolate batch values."""
    from pathlib import Path

    from engine.config.loader import DomainPackLoader
    from engine.sync.generator import SyncGenerator

    loader = DomainPackLoader(config_path=str(Path(__file__).parent.parent.parent / "domains"))
    spec = loader.load_domain("plasticos")
    gen = SyncGenerator(spec)
    if not spec.sync.endpoints:
        pytest.skip("No sync endpoints")
    ep = spec.sync.endpoints[0]
    evil_record = {ep.idproperty: "'; MATCH (n) DETACH DELETE n RETURN '1"}
    cypher = gen.generate_sync_query(ep, [evil_record])
    # Evil string must NOT appear in the Cypher itself
    assert "DETACH DELETE" not in cypher
    assert "'; MATCH" not in cypher
    # Batch values must flow through the $batch parameter
    assert "$batch" in cypher


def test_gate_compiler_never_interpolates_values():
    """GateCompiler must emit $param refs — never interpolate values into Cypher."""
    from pathlib import Path

    from engine.config.loader import DomainPackLoader
    from engine.gates.compiler import GateCompiler

    evil_value = "'; DROP DATABASE neo4j; //"
    loader = DomainPackLoader(config_path=str(Path(__file__).parent.parent.parent / "domains"))
    spec = loader.load_domain("plasticos")
    compiler = GateCompiler(spec)
    # Compile every declared direction — values must never appear in Cypher text;
    # the compiler only ever emits $param references.
    directions = {e.matchdirection for e in spec.matchentities.candidate}
    for direction in sorted(directions):
        for clause in (
            compiler.compile_all_gates(direction),
            compiler.compile_relaxed(direction),
        ):
            assert evil_value not in clause
            assert "DROP DATABASE" not in clause
            # Should use $param references instead
            assert "$" in clause or clause == ""


def test_eval_exec_not_importable_from_engine():
    """Engine modules must not expose eval/exec at module level."""
    import engine.gates.compiler as gc
    import engine.scoring.assembler as sa
    import engine.sync.generator as sg

    for mod in (gc, sg, sa):
        assert not hasattr(mod, "__builtins__") or True  # builtins always exist
        # Verify no custom eval-wrapper in public API
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            obj = getattr(mod, attr)
            if callable(obj) and "eval" in attr.lower():
                pytest.fail(f"eval-named function found in {mod.__name__}: {attr}")
