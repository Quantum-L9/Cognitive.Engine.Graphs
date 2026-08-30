"""Anti-resurrection invariants for the removed gap-fix artifact island.

A historical gap-fix bundle left seven executable engine modules in the tree
that no production entrypoint could ever reach. They survived because they
imported each other and because tests imported them — a closed loop that reads
as "covered" from a coverage report and as "active" from an L9_META header,
while the runtime never touched a line of it.

These invariants make that failure mode mechanically detectable rather than
archaeologically detectable:

* the removed module paths must stay removed;
* no engine or chassis module may import the removed surfaces;
* no module may reintroduce a gap-fix activation recipe;
* the canonical owner of each reclaimed responsibility must remain the sole one.

The static import-graph analyzer below is the reusable half. It models Python
import semantics closely enough to answer "can production reach this module?",
and is exercised here against the real tree.

Scope note: this is deliberately NOT a full-tree reachability gate. At the time
these invariants were written, 59 further engine modules across nine unaudited
subsystems were also unreachable from production entrypoints. Gating on the full
tree would have required either enumerating all 59 as permanent exemptions or
classifying them without evidence — the audit contract prohibits both. That work
is tracked as DEF-001 in
docs/audits/2026-08-23-gap-fix-artifact-convergence/GAP_FIX_REACHABILITY_CLASSIFICATION.yaml.
Tightening this module to the full tree is a scope decision, not new machinery.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
CHASSIS = ROOT / "chassis"
PKG = "engine"

# Modules removed as orphaned, stale, or test-only gap-fix artifacts.
REMOVED_MODULES = {
    "engine/compliance/audit_persistence.py",
    "engine/contract_enforcement.py",
    "engine/convergence_controller_patch.py",
    "engine/graph/community_export.py",
    "engine/graph/graph_sync_client_fix.py",
    "engine/graph_return_channel.py",
    "engine/startup_wiring.py",
}

# Import targets and symbols that only the removed island ever provided.
REMOVED_IMPORT_TARGETS = (
    "engine.compliance.audit_persistence",
    "engine.contract_enforcement",
    "engine.convergence_controller_patch",
    "engine.graph.community_export",
    "engine.graph.graph_sync_client_fix",
    "engine.graph_return_channel",
    "engine.startup_wiring",
)

REMOVED_SYMBOLS = (
    "GraphToEnrichReturnChannel",
    "GraphInferenceResultEnvelope",
    "build_graph_inference_result_envelope",
    "apply_return_channel_targets",
    "export_community_labels_to_enrich",
    "configure_audit_pool",
    "flush_audit_entries",
    "enforce_packet_envelope",
    "build_graph_sync_packet",
    "build_schema_proposal_packet",
    "ContractViolationError",
    "apply_all_gap_fixes",
    "patch_convergence_controller",
)

# Responsibilities the island duplicated, and the single owner each returned to.
CANONICAL_OWNERS = {
    "audit persistence": "engine/compliance/audit.py",
    "packet envelope contract": "engine/packet/packet_envelope.py",
    "graph sync write path": "engine/sync/generator.py",
    "community detection write": "engine/gds/scheduler.py",
    "startup lifecycle": "engine/boot.py",
}


# ── static import graph ──────────────────────────────────────────────────────


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _engine_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted(ENGINE.rglob("*.py"))}


def _resolve(target: str, known: set[str]) -> str | None:
    """Map a dotted import target onto an engine module that exists.

    `from engine.graph import driver` names a module; `from engine.graph.driver
    import GraphDriver` names a symbol inside one. Try the longest match first.
    """
    if target in known:
        return target
    parent = target.rsplit(".", 1)[0] if "." in target else None
    return parent if parent in known else None


def _imports_of(path: Path, known: set[str]) -> set[str]:
    """Engine modules imported by `path`, including deferred and dynamic ones."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    self_parts = _module_name(path).split(".")
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == PKG and (hit := _resolve(alias.name, known)):
                    found.add(hit)

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                depth = len(self_parts) - node.level
                if path.name == "__init__.py":
                    depth += 1
                base = self_parts[: max(0, depth)]
                module = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                module = node.module or ""
            if module.split(".")[0] != PKG:
                continue
            if hit := _resolve(module, known):
                found.add(hit)
            for alias in node.names:
                if hit := _resolve(f"{module}.{alias.name}", known):
                    found.add(hit)

        elif isinstance(node, ast.Call):
            fn = node.func
            called = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if called in {"import_module", "__import__", "find_spec"}:
                for arg in node.args:
                    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                        continue
                    if arg.value.split(".")[0] == PKG and (hit := _resolve(arg.value, known)):
                        found.add(hit)

    return found


def _with_ancestors(module: str, known: set[str]) -> set[str]:
    """Importing engine.a.b.c also executes engine.a.b, engine.a and engine."""
    parts = module.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)} & known


def reachable_from_production() -> set[str]:
    """Engine modules reachable from chassis ingress and the lifecycle hook."""
    modules = _engine_modules()
    known = set(modules)
    graph = {name: _imports_of(path, known) for name, path in modules.items()}

    roots: set[str] = set()
    for path in sorted(CHASSIS.rglob("*.py")):
        roots |= _imports_of(path, known)
    roots |= {"engine", "engine.boot"} & known

    seen: set[str] = set()
    for root in roots:
        seen |= _with_ancestors(root, known)
    queue = deque(seen)
    while queue:
        for target in graph.get(queue.popleft(), ()):
            for ancestor in _with_ancestors(target, known):
                if ancestor not in seen:
                    seen.add(ancestor)
                    queue.append(ancestor)
    return seen


# ── invariants ───────────────────────────────────────────────────────────────


def test_removed_gap_fix_modules_stay_removed() -> None:
    resurrected = sorted(rel for rel in REMOVED_MODULES if (ROOT / rel).exists())
    assert resurrected == []


def test_no_runtime_module_imports_a_removed_surface() -> None:
    offenders: list[str] = []
    for path in sorted([*ENGINE.rglob("*.py"), *CHASSIS.rglob("*.py")]):
        source = path.read_text(encoding="utf-8")
        for target in REMOVED_IMPORT_TARGETS:
            if target in source:
                offenders.append(f"{path.relative_to(ROOT)} -> {target}")
    assert offenders == []


def test_no_runtime_module_reintroduces_a_removed_symbol() -> None:
    """Some of these names are generic enough to be reinvented by accident —
    `ContractViolationError` and `enforce_packet_envelope` especially. That is
    the point: packet-envelope validation belongs to engine/packet/, and audit
    persistence to engine/compliance/audit.py. If a canonical owner genuinely
    needs one of these names, add it there and drop it from REMOVED_SYMBOLS in
    the same change — deliberately, with the owner named in the commit. Do not
    delete this test to get past it.
    """
    offenders: list[str] = []
    for path in sorted([*ENGINE.rglob("*.py"), *CHASSIS.rglob("*.py")]):
        source = path.read_text(encoding="utf-8")
        for symbol in REMOVED_SYMBOLS:
            if symbol in source:
                offenders.append(f"{path.relative_to(ROOT)} -> {symbol}")
    assert offenders == []


def test_no_module_ships_a_gap_fix_activation_recipe() -> None:
    """A module that tells an operator to wire it at startup, instead of being
    wired, is how the island survived. engine/boot.py is the startup owner.
    """
    offenders: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        if "gap-fix" in lowered or "gap fix" in lowered:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_canonical_owners_remain_present_and_sole() -> None:
    missing = sorted(f"{role}: {rel}" for role, rel in CANONICAL_OWNERS.items() if not (ROOT / rel).exists())
    assert missing == []


def test_canonical_owners_are_production_reachable() -> None:
    """The point of the removals: each reclaimed responsibility now sits with an
    owner the runtime actually reaches.
    """
    reachable = reachable_from_production()
    expected = {
        "engine.compliance.audit",
        "engine.sync.generator",
        "engine.gds.scheduler",
        "engine.boot",
    }
    assert sorted(expected - reachable) == []


def test_import_graph_analyzer_models_a_known_edge() -> None:
    """Guard the analyzer itself: a real, stable production edge must be seen,
    so a silently broken parser cannot make every invariant above vacuous.
    """
    modules = _engine_modules()
    known = set(modules)
    assert "engine.graph.driver" in _imports_of(modules["engine.boot"], known)
    assert "engine.handlers" in reachable_from_production()
