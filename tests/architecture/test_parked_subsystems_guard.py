"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tests, architecture]
tags: [tests, architecture, parked, kge, hoprag]
owner: engine-team
status: active
--- /L9_META ---

CEG-KGE-PARK architecture guard (operator decision 2026-11-14).

HopRAG and CompoundE3D are parked. No runtime module may import from
``engine.hoprag`` or from the parked ``engine.kge`` modules, so the parking
cannot silently regress by a new caller wiring the code back in. The one
sanctioned door is ``engine.kge.parked``, which imports no parked module at
load time and refuses before it would. See docs/PARKED_SUBSYSTEMS.md.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Runtime surfaces: the handlers, the boot lifecycle, the HTTP chassis, scoring
# and the GDS scheduler.
RUNTIME_FILES = ("engine/handlers.py", "engine/boot.py")
RUNTIME_DIRS = ("chassis", "engine/scoring", "engine/gds")

PARKED_PACKAGE_PREFIXES = ("engine.hoprag",)
PARKED_KGE_MODULES = frozenset(
    {
        "engine.kge.compound_e3d",
        "engine.kge.beam_search",
        "engine.kge.pareto_ensemble",
        "engine.kge.cross_dimensional_ensemble",
    }
)
# Names ``engine.kge`` re-exports from the parked modules (lazy, PEP 562).
PARKED_KGE_EXPORTS = frozenset(
    {
        "BeamSearchConfig",
        "BeamSearchEngine",
        "CascadeVariant",
        "CompoundE3D",
        "CompoundE3DConfig",
        "CrossDimensionalEnsemble",
        "CrossDimensionalResult",
        "DimensionalScore",
        "ParetoEnsembleController",
        "ParetoEnsembleResult",
    }
)


def _runtime_sources() -> Iterator[Path]:
    for rel in RUNTIME_FILES:
        yield ROOT / rel
    for rel in RUNTIME_DIRS:
        yield from sorted((ROOT / rel).rglob("*.py"))


def _is_parked_module(name: str) -> bool:
    if name in PARKED_KGE_MODULES:
        return True
    return any(name == p or name.startswith(p + ".") for p in PARKED_PACKAGE_PREFIXES)


def _parked_imports(path: Path) -> list[str]:
    """Every import in ``path`` (module level or lazy) that reaches parked code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [f"import {a.name}" for a in node.names if _is_parked_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_parked_module(node.module):
                hits.append(f"from {node.module} import ...")
            elif node.module == "engine":
                hits += [f"from engine import {a.name}" for a in node.names if a.name == "hoprag"]
            elif node.module == "engine.kge":
                hits += [
                    f"from engine.kge import {a.name}"
                    for a in node.names
                    if a.name in PARKED_KGE_EXPORTS or "engine.kge." + a.name in PARKED_KGE_MODULES
                ]
    return hits


def test_runtime_surfaces_exist() -> None:
    sources = list(_runtime_sources())
    assert all(p.is_file() for p in sources)
    # An empty scan would pass vacuously; the guard must see the real surfaces.
    assert any(p.name == "handlers.py" for p in sources)
    assert len(sources) > len(RUNTIME_FILES)


def test_no_runtime_module_imports_parked_code() -> None:
    offenders = {p.relative_to(ROOT).as_posix(): hits for p in _runtime_sources() if (hits := _parked_imports(p))}
    assert offenders == {}, (
        "Runtime modules import parked HopRAG/CompoundE3D code (CEG-KGE-PARK, "
        f"docs/PARKED_SUBSYSTEMS.md): {offenders}. Go through engine.kge.parked."
    )


def test_guard_detects_a_parked_import(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from engine.kge.compound_e3d import CompoundE3D\n"
        "def f():\n"
        "    from engine.hoprag.indexer import GraphIndexBuilder\n"
        "    from engine.kge import BeamSearchEngine\n"
        "    from engine.kge.parked import require_unparked\n",
        encoding="utf-8",
    )
    assert _parked_imports(probe) == [
        "from engine.kge.compound_e3d import ...",
        "from engine.hoprag.indexer import ...",
        "from engine.kge import BeamSearchEngine",
    ]


def test_parked_registry_matches_package_exports() -> None:
    import engine.kge as kge_pkg

    parked_exports = {name for name, module in kge_pkg._EXPORTS.items() if module in PARKED_KGE_MODULES}
    assert parked_exports == PARKED_KGE_EXPORTS
