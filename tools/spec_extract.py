"""
--- L9_META ---
l9_schema: 1
origin: l9-template
engine: graph
layer: [audit]
tags: [L9_TEMPLATE, audit, spec-coverage]
owner: platform
status: active
--- /L9_META ---

tools/spec_extract.py
L9_TEMPLATE: true

Extracts the features the graph-cognitive-engine spec YAML declares, then
reports, per feature, whether the engine implements it and whether a test
exercises it.

Feature sources (every counted feature is traceable to exactly one):
    graph-cognitive-engine-spec-v1.1.0.yaml  — the spec's structured sections:
        gatetypes[].name                     → category ``gates``
        scoringcomputationtypes[].name       → category ``scoring``
        gdsjobs.algorithms[].name and every
        distinct gdsjobs.*_jobs[].algorithm  → category ``gds_algorithm``
        endpoints.universal[].path (/v1/<x>) → category ``action_handler``
    ENGINE_CONTRACT_FEATURES (below)         — engine contracts the spec
        predates, each with its declaring document and reason
    tools/research/top5_leverage_patterns_detailed.json
                                             — researched leverage patterns
        (the spec's ``traceability`` section cites this file), category
        ``research_pattern``

A name the spec only mentions in prose is not a feature: there is no
"detected in spec text" path. Spec sections that carry domain-pack
vocabulary (``node_labels``, ``edge_registry``, ``query_templates``) are not
engine features — the engine is domain-neutral
(tests/architecture/test_core_vocabulary_guard.py) — and are not counted.

Status rule (evidence, not file counts):
    IMPLEMENTED  the feature's implementation is present in its owning code
                 AND at least one test under tests/ references it
    PARTIAL      the implementation is present but no test references it
    MISSING      no implementation is present

Implementation evidence, per category (read with ``ast``; nothing is imported):
    gates            the spec name is a ``GateType`` member value
                     (engine/config/schema.py) whose member is a key of the
                     gate registry dict (engine/gates/registry.py)
    scoring          the spec name is a ``ComputationType`` member value whose
                     member the scoring assembler references
                     (engine/scoring/assembler.py)
    gds_algorithm    a string literal the GDS scheduler dispatches on
                     (``algo``/``job_spec.algorithm`` compared to it, a ``case``
                     pattern, or a dict key mapped to a ``_run_*`` method in
                     engine/gds/scheduler.py) equals the spec algorithm
    action_handler   the action is a key of ``ACTION_HANDLERS``
                     (engine/handlers.py), or, for an endpoint declared in
                     ENDPOINT_REALIZATIONS, its host handler dispatches the
                     declared ``subaction`` literal
    research_pattern a token the pattern's ``engine_mapping`` declares occurs in
                     one of its declared ``search_files`` (the mapping is
                     authored in tools/research; it is not checked semantically)
Names are compared normalised: lower-case, letters and digits only, so the
spec's ``temporal_recency`` is the engine's ``temporalrecency``.

Test evidence: a ``tests/**/*.py`` file references the implementation —
``GateType.<MEMBER>`` or the registered gate class, ``ComputationType.<MEMBER>``
or the quoted computation value, the quoted GDS dispatch literal, the handler
function name (the quoted subaction for a realized endpoint), or (research)
a declared token. Tests of this audit tool (files
that mention ``spec_extract``) are not evidence for any feature.

An owning file that is absent means no implementation (MISSING). An owning file
that is present but unparseable, or no longer has the anchor the probe reads
(the enum, the registry dict, the dispatch, ``ACTION_HANDLERS``), is unresolved:
the extractor exits 2 rather than report a status it cannot establish.

Usage:
    python tools/spec_extract.py                          # default spec path
    python tools/spec_extract.py --spec path/to/spec.yaml # custom spec path
    python tools/spec_extract.py --fail-on MISSING        # exit 1 if any MISSING

Outputs:
    artifacts/spec_checklist.json    — extracted features from spec
    artifacts/coverage_matrix.json   — IMPLEMENTED / PARTIAL / MISSING per feature
    artifacts/coverage_report.md     — human-readable markdown report
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.path_policy import (
    PathPolicyError,
    resolve_existing_file_under_root,
    resolve_root,
    resolve_under_root,
    write_text_no_follow,
)

try:
    import yaml
except ImportError:
    yaml = None


L9_TEMPLATE_TAG = "L9_TEMPLATE"
RESEARCH_DIR = "tools/research"
RESEARCH_PATTERNS_FILE = "top5_leverage_patterns_detailed.json"
RESEARCH_CATEGORY = "research_pattern"
COVERAGE_MATRIX_JSON = "coverage_matrix.json"

GATES_CATEGORY = "gates"
SCORING_CATEGORY = "scoring"
GDS_CATEGORY = "gds_algorithm"
ACTION_CATEGORY = "action_handler"

SCHEMA_FILE = "engine/config/schema.py"
GATE_REGISTRY_FILE = "engine/gates/registry.py"
ASSEMBLER_FILE = "engine/scoring/assembler.py"
GDS_SCHEDULER_FILE = "engine/gds/scheduler.py"
HANDLERS_FILE = "engine/handlers.py"
TESTS_DIR = "tests"
AUDIT_TOOL_MARKER = "spec_extract"

STATUS_DEFINITIONS = (
    "IMPLEMENTED = implementation present in its owning code AND referenced by a test under tests/; "
    "PARTIAL = implementation present, no test references it; "
    "MISSING = no implementation present. "
    "Evidence rules per category: tools/spec_extract.py module docstring."
)

# Engine contracts the spec does not list. Each entry is
# (category, name, declaring document, reason). Nothing else outside the spec
# file and the research file is counted.
ENGINE_CONTRACT_FEATURES: tuple[tuple[str, str, str, str], ...] = (
    (
        ACTION_CATEGORY,
        "healthcheck",
        "AGENTS.md Project Structure (engine/handlers.py action handlers)",
        "chassis liveness action the engine declares; the spec's endpoints section predates it",
    ),
    (
        ACTION_CATEGORY,
        "enrich",
        "AGENTS.md Project Structure (engine/handlers.py action handlers)",
        "enrichment action the engine declares; the spec's endpoints section predates it",
    ),
)


# Spec endpoints the engine serves as a subaction of another action rather than
# as an action of their own: {endpoint action: (host action, subaction, reason)}.
# A declared compatibility, verified against the host handler's dispatch.
ENDPOINT_REALIZATIONS: dict[str, tuple[str, str, str]] = {
    "domains": (
        "admin",
        "list_domains",
        "the engine lists domain packs through admin subaction list_domains; the chassis owns the HTTP route",
    ),
}


class Status(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


class EvidenceUnresolvedError(RuntimeError):
    """An owning file exists but the probe cannot read the anchor it relies on."""


@dataclass
class SpecFeature:
    category: str
    name: str
    spec_reference: str
    search_tokens: list[str] = field(default_factory=list)
    search_files: list[str] = field(default_factory=list)
    status: str = "MISSING"
    evidence_files: list[str] = field(default_factory=list)
    evidence_lines: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)


def load_yaml_file(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required: bash tools/deps/install_locked.sh tools")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_name(name: str) -> str:
    """Lower-case, letters and digits only: ``temporal_recency`` == ``temporalrecency``."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _named_entries(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, list):
        return []
    return [item for item in section if isinstance(item, dict) and isinstance(item.get("name"), str)]


def extract_gate_features(spec: dict) -> list[SpecFeature]:
    return [
        SpecFeature(category=GATES_CATEGORY, name=item["name"], spec_reference=f"gatetypes[{item['name']}]")
        for item in _named_entries(spec.get("gatetypes"))
    ]


def extract_scoring_features(spec: dict) -> list[SpecFeature]:
    return [
        SpecFeature(
            category=SCORING_CATEGORY,
            name=item["name"],
            spec_reference=f"scoringcomputationtypes[{item['name']}]",
        )
        for item in _named_entries(spec.get("scoringcomputationtypes"))
    ]


def extract_gds_features(spec: dict) -> list[SpecFeature]:
    """One feature per distinct algorithm: the registry list, then every job's ``algorithm``."""
    gds = spec.get("gdsjobs")
    if not isinstance(gds, dict):
        return []
    features: list[SpecFeature] = []
    seen: dict[str, SpecFeature] = {}

    def add(name: str, ref: str) -> None:
        key = normalize_name(name)
        if not key:
            return
        if key in seen:
            if ref not in seen[key].spec_reference:
                seen[key].spec_reference += f"; {ref}"
            return
        feature = SpecFeature(category=GDS_CATEGORY, name=name, spec_reference=ref)
        seen[key] = feature
        features.append(feature)

    for item in _named_entries(gds.get("algorithms")):
        add(item["name"], f"gdsjobs.algorithms[{item['name']}]")
    for section, jobs in gds.items():
        if section == "algorithms":
            continue
        for job in _named_entries(jobs):
            algorithm = job.get("algorithm")
            if isinstance(algorithm, str):
                add(algorithm, f"gdsjobs.{section}[{job['name']}].algorithm")
    return features


def extract_action_features(spec: dict) -> list[SpecFeature]:
    """Spec endpoints ``/v1/<action>[/...]``, then the declared engine-contract actions."""
    features: list[SpecFeature] = []
    seen: set[str] = set()
    endpoints = spec.get("endpoints")
    universal = endpoints.get("universal") if isinstance(endpoints, dict) else None
    for item in universal if isinstance(universal, list) else []:
        path = item.get("path") if isinstance(item, dict) else None
        match = re.match(r"^/v\d+/([A-Za-z0-9_-]+)", path) if isinstance(path, str) else None
        if match is None or match.group(1) in seen:
            continue
        action = match.group(1)
        seen.add(action)
        reference = f"endpoints.universal[{path}]"
        if action in ENDPOINT_REALIZATIONS:
            host, subaction, reason = ENDPOINT_REALIZATIONS[action]
            reference += f" — realized as {host} subaction {subaction}: {reason}"
        features.append(SpecFeature(category=ACTION_CATEGORY, name=action, spec_reference=reference))
    for category, name, source, reason in ENGINE_CONTRACT_FEATURES:
        if category == ACTION_CATEGORY and name not in seen:
            seen.add(name)
            features.append(
                SpecFeature(category=category, name=name, spec_reference=f"engine contract: {source} — {reason}")
            )
    return features


def extract_research_features(root: Path) -> list[SpecFeature]:
    """Turn researched leverage patterns into tracked coverage items.

    Each pattern in the research file declares an `engine_mapping` naming the
    real engine symbols that implement it. Patterns without that mapping are
    skipped and warned about — an unmapped pattern cannot be scanned for, so
    reporting it as MISSING would be indistinguishable from a genuine gap.
    """
    try:
        path = resolve_under_root(root / RESEARCH_DIR / RESEARCH_PATTERNS_FILE, root, option="research patterns")
    except PathPolicyError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return []
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
        return []

    features = []
    for key, pattern in data.items():
        if key.startswith("_") or not isinstance(pattern, dict):
            continue
        mapping = pattern.get("engine_mapping")
        tokens = mapping.get("search_tokens") if isinstance(mapping, dict) else None
        if not tokens:
            print(f"WARNING: research pattern {key} has no engine_mapping.search_tokens — skipped", file=sys.stderr)
            continue

        features.append(
            SpecFeature(
                category=RESEARCH_CATEGORY,
                name=str(pattern.get("pattern_name", key)),
                spec_reference=f"{RESEARCH_DIR}/{RESEARCH_PATTERNS_FILE}#{key}",
                search_tokens=[str(t) for t in tokens],
                search_files=[str(f) for f in mapping.get("search_files", [])],
            )
        )

    return features


def spec_candidates(root: Path) -> list[Path]:
    """Root-level spec YAMLs ``main`` auto-detects from, in a stable order (it takes the first)."""
    return sorted(root.glob("*spec*.yaml")) + sorted(root.glob("*spec*.yml"))


def collect_features(spec: dict, root: Path) -> list[SpecFeature]:
    """Every feature the coverage report counts: the spec's, the engine contracts, the research patterns."""
    features: list[SpecFeature] = []
    features += extract_gate_features(spec)
    features += extract_scoring_features(spec)
    features += extract_gds_features(spec)
    features += extract_action_features(spec)
    features += extract_research_features(root)
    return features


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob to a regex: ``**/`` spans directories, ``*`` stays in one."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_search_file(path: str, pattern: str) -> bool:
    """True when repo-relative *path* is in the scope declared by *pattern*.

    A pattern with glob characters is matched as a glob. A plain path matches
    that file exactly or, as a directory prefix (``engine/gds/`` or
    ``engine/gds``), every file in its subtree.
    """
    path = path.replace("\\", "/")
    if any(ch in pattern for ch in "*?["):
        return _glob_to_regex(pattern).match(path) is not None
    prefix = pattern.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


# ── Implementation probes ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Implementation:
    """Where a feature is implemented and what a test must name to exercise it."""

    lines: tuple[str, ...]
    test_patterns: tuple[re.Pattern[str], ...]


def _word(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


def _quoted(literal: str) -> re.Pattern[str]:
    return re.compile(rf"[\"']{re.escape(literal)}[\"']")


class EngineIndex:
    """Parsed anchors of the owning engine files, read once from the scanned sources."""

    def __init__(self, sources: dict[str, str]) -> None:
        self._sources = sources
        self._trees: dict[str, ast.Module | None] = {}
        self._handler_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def tree(self, rel: str) -> ast.Module | None:
        """The parsed module, or None when the owning file is absent (no implementation)."""
        if rel not in self._trees:
            source = self._sources.get(rel)
            if source is None:
                self._trees[rel] = None
            else:
                try:
                    self._trees[rel] = ast.parse(source, filename=rel)
                except SyntaxError as exc:
                    msg = f"{rel} does not parse ({exc.msg}, line {exc.lineno}); its features cannot be evaluated"
                    raise EvidenceUnresolvedError(msg) from exc
        return self._trees[rel]

    def enum_members(self, class_name: str) -> dict[str, tuple[str, str, int]]:
        """``{normalised value: (MEMBER, value, line)}`` of a ``StrEnum`` in the schema module."""
        module = self.tree(SCHEMA_FILE)
        if module is None:
            return {}
        for node in module.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                members: dict[str, tuple[str, str, int]] = {}
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        value = stmt.value.value
                        members[normalize_name(value)] = (stmt.targets[0].id, value, stmt.lineno)
                return members
        msg = f"{SCHEMA_FILE} has no class {class_name}; the probe that reads it cannot run"
        raise EvidenceUnresolvedError(msg)

    def attribute_refs(self, rel: str, owner: str) -> dict[str, int]:
        """``{MEMBER: first line}`` for every ``owner.MEMBER`` attribute in *rel*."""
        module = self.tree(rel)
        if module is None:
            return {}
        refs: dict[str, int] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == owner:
                refs.setdefault(node.attr, node.lineno)
        return refs

    def gate_registry(self) -> dict[str, tuple[str, int]]:
        """``{GateType member: (registered class, line)}`` from the registry dict literal."""
        module = self.tree(GATE_REGISTRY_FILE)
        if module is None:
            return {}
        registry: dict[str, tuple[str, int]] = {}
        for node in ast.walk(module):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Attribute)
                    and isinstance(key.value, ast.Name)
                    and key.value.id == "GateType"
                    and isinstance(value, ast.Name)
                ):
                    registry[key.attr] = (value.id, key.lineno)
        if not registry:
            msg = f"{GATE_REGISTRY_FILE} has no GateType → gate class dict; gate features cannot be evaluated"
            raise EvidenceUnresolvedError(msg)
        return registry

    def gds_dispatch(self) -> dict[str, tuple[str, int]]:
        """``{normalised literal: (literal, line)}`` the scheduler dispatches an algorithm on."""
        module = self.tree(GDS_SCHEDULER_FILE)
        if module is None:
            return {}
        dispatch: dict[str, tuple[str, int]] = {}

        def is_algorithm(expr: ast.expr) -> bool:
            return (isinstance(expr, ast.Name) and expr.id in {"algo", "algorithm"}) or (
                isinstance(expr, ast.Attribute) and expr.attr == "algorithm"
            )

        def record(expr: ast.expr | None) -> None:
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                dispatch.setdefault(normalize_name(expr.value), (expr.value, expr.lineno))
            elif isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
                for element in expr.elts:
                    record(element)

        for node in ast.walk(module):
            if isinstance(node, ast.Compare) and is_algorithm(node.left):
                if all(isinstance(op, (ast.Eq, ast.In)) for op in node.ops):
                    for comparator in node.comparators:
                        record(comparator)
            elif isinstance(node, ast.Match) and is_algorithm(node.subject):
                for case in node.cases:
                    for sub in ast.walk(case.pattern):
                        if isinstance(sub, ast.MatchValue):
                            record(sub.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    target = value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
                    if isinstance(target, str) and target.startswith("_run_"):
                        record(key)
        if not dispatch:
            msg = f"{GDS_SCHEDULER_FILE} has no algorithm dispatch; GDS features cannot be evaluated"
            raise EvidenceUnresolvedError(msg)
        return dispatch

    def action_handlers(self) -> dict[str, tuple[str, int]]:
        """``{action: (handler function, line)}`` from the ``ACTION_HANDLERS`` dict literal."""
        module = self.tree(HANDLERS_FILE)
        if module is None:
            return {}
        constants: dict[str, str] = {}
        table: ast.Dict | None = None
        for node in module.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    constants[target.id] = value.value
                if target.id == "ACTION_HANDLERS" and isinstance(value, ast.Dict):
                    table = value
        if table is None:
            msg = f"{HANDLERS_FILE} has no ACTION_HANDLERS dict; action features cannot be evaluated"
            raise EvidenceUnresolvedError(msg)
        handlers: dict[str, tuple[str, int]] = {}
        self._handler_functions = {
            node.name: node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for key, value in zip(table.keys, table.values, strict=True):
            action = None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                action = key.value
            elif isinstance(key, ast.Name):
                action = constants.get(key.id)
            if action is not None and isinstance(value, ast.Name) and key is not None:
                handlers[action] = (value.id, key.lineno)
        return handlers

    def subactions(self, handler: str) -> dict[str, int]:
        """``{subaction literal: line}`` the named handler dispatches on (``subaction == "<x>"``)."""
        self.action_handlers()
        function = self._handler_functions.get(handler)
        if function is None:
            return {}
        found: dict[str, int] = {}
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "subaction"
                and all(isinstance(op, ast.Eq) for op in node.ops)
            ):
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        found.setdefault(comparator.value, comparator.lineno)
        return found


def _probe_gate(index: EngineIndex, feature: SpecFeature) -> Implementation | None:
    member = index.enum_members("GateType").get(normalize_name(feature.name))
    if member is None:
        return None
    registered = index.gate_registry().get(member[0])
    if registered is None:
        return None
    gate_class, line = registered
    return Implementation(
        lines=(f"{SCHEMA_FILE}:{member[2]}", f"{GATE_REGISTRY_FILE}:{line}"),
        test_patterns=(_word(f"GateType.{member[0]}"), _word(gate_class)),
    )


def _probe_scoring(index: EngineIndex, feature: SpecFeature) -> Implementation | None:
    member = index.enum_members("ComputationType").get(normalize_name(feature.name))
    if member is None:
        return None
    name, value, enum_line = member
    line = index.attribute_refs(ASSEMBLER_FILE, "ComputationType").get(name)
    if line is None:
        return None
    return Implementation(
        lines=(f"{SCHEMA_FILE}:{enum_line}", f"{ASSEMBLER_FILE}:{line}"),
        test_patterns=(_word(f"ComputationType.{name}"), _quoted(value)),
    )


def _probe_gds(index: EngineIndex, feature: SpecFeature) -> Implementation | None:
    found = index.gds_dispatch().get(normalize_name(feature.name))
    if found is None:
        return None
    literal, line = found
    return Implementation(lines=(f"{GDS_SCHEDULER_FILE}:{line}",), test_patterns=(_quoted(literal),))


def _probe_action(index: EngineIndex, feature: SpecFeature) -> Implementation | None:
    handlers = index.action_handlers()
    found = handlers.get(feature.name)
    if found is not None:
        handler, line = found
        return Implementation(lines=(f"{HANDLERS_FILE}:{line}",), test_patterns=(_word(handler),))
    if feature.name not in ENDPOINT_REALIZATIONS:
        return None
    host, subaction, _reason = ENDPOINT_REALIZATIONS[feature.name]
    host_entry = handlers.get(host)
    if host_entry is None:
        return None
    line = index.subactions(host_entry[0]).get(subaction)
    if line is None:
        return None
    return Implementation(
        lines=(f"{HANDLERS_FILE}:{host_entry[1]}", f"{HANDLERS_FILE}:{line}"),
        test_patterns=(_quoted(subaction),),
    )


PROBES: dict[str, Callable[[EngineIndex, SpecFeature], Implementation | None]] = {
    GATES_CATEGORY: _probe_gate,
    SCORING_CATEGORY: _probe_scoring,
    GDS_CATEGORY: _probe_gds,
    ACTION_CATEGORY: _probe_action,
}


def _read_tree(root: Path, suffix: str, skip: tuple[str, ...]) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob(f"*{suffix}"):
        if any(part in path.parts for part in skip):
            continue
        try:
            safe = resolve_under_root(path, root, option=f"discovered {suffix} file")
            files[path.relative_to(root).as_posix()] = safe.read_text(encoding="utf-8", errors="replace")
        except (OSError, PathPolicyError):
            # Unreadable or escaping file (permissions, symlink out of root):
            # it is not evidence; keep scanning the rest of the tree.
            pass
    return files


def _token_implementation(feature: SpecFeature, all_files: dict[str, str]) -> Implementation | None:
    """Declared-token evidence (research patterns, ad-hoc features): a token in a declared search file.

    Scope = union of the files matched by the declared search_files. An empty
    scoped set means no implementation; it never widens to the whole tree.
    """
    scope = all_files
    if feature.search_files:
        scope = {
            fpath: content
            for fpath, content in all_files.items()
            if any(matches_search_file(fpath, glob_pat) for glob_pat in feature.search_files)
        }
    lines: list[str] = []
    for fpath, content in sorted(scope.items()):
        lowered = content.lower()
        for token in feature.search_tokens:
            needle = token.lower()
            if needle in lowered:
                lines.extend(f"{fpath}:{i}" for i, line in enumerate(content.splitlines(), 1) if needle in line.lower())
                break
    if not lines:
        return None
    patterns = tuple(re.compile(re.escape(t), re.IGNORECASE) for t in feature.search_tokens)
    return Implementation(lines=tuple(lines), test_patterns=patterns)


def _executable_source(source: str) -> str:
    """``source`` with non-executable text blanked: comments, docstrings and other
    bare string statements, and ``reason=`` strings (skip / xfail reasons).

    Test evidence is matched against this text, so ``# TODO: test ThresholdGate``,
    a docstring that names a feature or a skip reason never counts as a test
    referencing it. String literals that executable code uses (for example
    ``{"algorithm": "nodeSimilarity"}``) are kept: they are real references.
    Lengths and line structure are preserved. Unparseable source keeps only the
    comment stripping.
    """
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    chars = list(source)

    def blank(start: tuple[int, int], end: tuple[int, int]) -> None:
        a = offsets[start[0] - 1] + start[1]
        b = offsets[end[0] - 1] + end[1]
        for i in range(a, min(b, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                blank(tok.start, tok.end)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # keep the comments found before the tokenizer gave up
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "".join(chars)
    for node in ast.walk(tree):
        target = _non_executable_string(node)
        if target is not None and target.end_lineno is not None and target.end_col_offset is not None:
            blank((target.lineno, target.col_offset), (target.end_lineno, target.end_col_offset))
    return "".join(chars)


def _non_executable_string(node: ast.AST) -> ast.expr | None:
    """A docstring / bare string statement, or a ``reason=`` value; else None."""
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return node.value
    if isinstance(node, ast.keyword) and node.arg == "reason":
        return node.value
    return None


def scan_codebase(root: Path, features: list[SpecFeature]) -> None:
    """Assign each feature its status from implementation evidence and test evidence."""
    py_files = _read_tree(root, ".py", (".venv", "__pycache__"))
    yaml_files = _read_tree(root, ".yaml", (".venv",))
    all_files = {**py_files, **yaml_files}
    index = EngineIndex(py_files)
    test_sources = {
        fpath: _executable_source(content)
        for fpath, content in py_files.items()
        if fpath.startswith(f"{TESTS_DIR}/") and AUDIT_TOOL_MARKER not in content
    }

    for feature in features:
        probe = PROBES.get(feature.category) if not feature.search_tokens else None
        implementation = probe(index, feature) if probe else _token_implementation(feature, all_files)

        if implementation is None:
            feature.evidence_files = []
            feature.evidence_lines = []
            feature.test_files = []
            feature.status = Status.MISSING.value
            continue

        feature.evidence_files = sorted({line.rsplit(":", 1)[0] for line in implementation.lines})
        feature.evidence_lines = list(implementation.lines[:10])
        feature.test_files = sorted(
            fpath
            for fpath, content in test_sources.items()
            if any(pattern.search(content) for pattern in implementation.test_patterns)
        )
        feature.status = Status.IMPLEMENTED.value if feature.test_files else Status.PARTIAL.value


def _artifact_file(out_dir: Path, name: str, root: Path) -> Path:
    """Resolve one artifact file so a child symlink cannot escape --root."""
    return resolve_under_root(out_dir / name, root, option=name)


def _biome_json(value: object, *, width: int = 100) -> str:
    """Emit JSON that Biome 2.5.8 keeps stable (indent 2, width 100, bracket spacing)."""

    def compact(node: object) -> str:
        if node is None or isinstance(node, (bool, int, float, str)):
            return json.dumps(node)
        if isinstance(node, list):
            return "[" + ", ".join(compact(item) for item in node) + "]"
        if isinstance(node, dict):
            if not node:
                return "{}"
            body = ", ".join(f"{json.dumps(key)}: {compact(item)}" for key, item in node.items())
            return "{ " + body + " }"
        raise TypeError(f"unsupported checklist value: {type(node).__name__}")

    def emit(node: object, column: int, level: int) -> str:
        rendered = compact(node)
        if column + len(rendered) <= width or not isinstance(node, (list, dict)):
            return rendered
        pad = "  " * (level + 1)
        close = "  " * level
        if isinstance(node, list):
            inner = ",\n".join(pad + emit(item, (level + 1) * 2, level + 1) for item in node)
            return "[\n" + inner + "\n" + close + "]"
        rows = []
        for key, item in node.items():
            prefix = f"{json.dumps(key)}: "
            rows.append(pad + prefix + emit(item, (level + 1) * 2 + len(prefix), level + 1))
        return "{\n" + ",\n".join(rows) + "\n" + close + "}"

    return emit(value, 0, 0) + "\n"


def write_checklist(out_dir: Path, features: list[SpecFeature], root: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = [asdict(f) for f in features]
    _artifact_file(out_dir, "spec_checklist.json", root)
    write_text_no_follow(out_dir, "spec_checklist.json", _biome_json(data))


def write_coverage_matrix(out_dir: Path, features: list[SpecFeature], root: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    by_category: dict[str, dict[str, int]] = {}
    for f in features:
        cat = f.category
        if cat not in by_category:
            by_category[cat] = {"IMPLEMENTED": 0, "PARTIAL": 0, "MISSING": 0, "total": 0}
        by_category[cat][f.status] += 1
        by_category[cat]["total"] += 1

    totals = {"IMPLEMENTED": 0, "PARTIAL": 0, "MISSING": 0, "total": 0}
    for cat_data in by_category.values():
        for k in totals:
            totals[k] += cat_data[k]

    matrix = {"categories": by_category, "totals": totals, "generated_at": datetime.now(UTC).isoformat()}
    _artifact_file(out_dir, COVERAGE_MATRIX_JSON, root)
    write_text_no_follow(out_dir, COVERAGE_MATRIX_JSON, json.dumps(matrix, indent=2) + "\n")


def write_coverage_report(out_dir: Path, features: list[SpecFeature], matrix: dict, root: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# L9 Spec Coverage Report")
    lines.append(f"\n- Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"- Template tag: `{L9_TEMPLATE_TAG}`")
    lines.append(f"- Status: {STATUS_DEFINITIONS}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Implemented | Partial | Missing | Total |")
    lines.append("|----------|-------------|---------|---------|-------|")

    matrix_path = _artifact_file(out_dir, COVERAGE_MATRIX_JSON, root)
    cats = json.loads(matrix_path.read_text())["categories"]
    for cat, data in cats.items():
        lines.append(f"| {cat} | {data['IMPLEMENTED']} | {data['PARTIAL']} | {data['MISSING']} | {data['total']} |")

    totals = json.loads(matrix_path.read_text())["totals"]
    lines.append(
        f"| **TOTAL** | **{totals['IMPLEMENTED']}** | **{totals['PARTIAL']}** | **{totals['MISSING']}** | **{totals['total']}** |"
    )
    lines.append("")

    for status_filter in [Status.MISSING.value, Status.PARTIAL.value, Status.IMPLEMENTED.value]:
        filtered = [f for f in features if f.status == status_filter]
        if not filtered:
            continue

        icon = {"MISSING": "❌", "PARTIAL": "⚠️", "IMPLEMENTED": "✅"}[status_filter]
        lines.append(f"## {icon} {status_filter}")
        lines.append("")

        for f in filtered:
            lines.append(f"### {f.category} → `{f.name}`")
            lines.append(f"- Spec ref: `{f.spec_reference}`")
            if f.evidence_lines:
                lines.append(f"- Implementation: {', '.join(f.evidence_lines[:5])}")
            else:
                lines.append("- **No implementation found**")
            if f.test_files:
                lines.append(f"- Tests: {', '.join(f'`{t}`' for t in f.test_files[:5])}")
            elif f.evidence_lines:
                lines.append("- **No test references it**")
            lines.append("")

    _artifact_file(out_dir, "coverage_report.md", root)
    write_text_no_follow(out_dir, "coverage_report.md", "\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L9 Spec Coverage Extractor")
    parser.add_argument("--spec", default=None, help="Path to spec YAML (auto-detected if omitted)")
    parser.add_argument("--root", default=".", help="Repo root directory")
    parser.add_argument(
        "--fail-on",
        default="MISSING",
        choices=["MISSING", "PARTIAL", "NONE"],
        help="Exit 1 if any features have this status (or worse)",
    )
    args = parser.parse_args(argv)

    try:
        root = resolve_root(args.root)
        out_dir = resolve_under_root("artifacts", root, option="artifact output directory")
    except PathPolicyError as exc:
        parser.error(str(exc))

    spec_path: Path | None = None
    if args.spec:
        try:
            spec_path = resolve_existing_file_under_root(args.spec, root, option="--spec")
        except PathPolicyError as exc:
            parser.error(str(exc))
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if spec_path is None:
        candidates = spec_candidates(root)
        if candidates:
            try:
                spec_path = resolve_existing_file_under_root(candidates[0], root, option="auto-detected spec")
            except PathPolicyError as exc:
                parser.error(str(exc))
            except OSError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
        else:
            print("ERROR: No spec YAML found. Use --spec path/to/spec.yaml", file=sys.stderr)
            return 2

    print(f"Loading spec: {spec_path}")
    try:
        spec = load_yaml_file(spec_path)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    features = collect_features(spec, root)
    research_count = sum(1 for f in features if f.category == RESEARCH_CATEGORY)

    print(f"Extracted {len(features)} features from spec ({research_count} from {RESEARCH_DIR})")
    print(f"Scanning codebase at {root} ...")

    try:
        scan_codebase(root, features)
    except EvidenceUnresolvedError as exc:
        print(f"ERROR: evidence unresolved: {exc}", file=sys.stderr)
        return 2

    try:
        write_checklist(out_dir, features, root)
        write_coverage_matrix(out_dir, features, root)
        matrix_path = _artifact_file(out_dir, COVERAGE_MATRIX_JSON, root)
        matrix_data = json.loads(matrix_path.read_text())
        write_coverage_report(out_dir, features, matrix_data, root)
    except PathPolicyError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    totals = matrix_data["totals"]
    print(
        f"\nCoverage: {totals['IMPLEMENTED']} implemented, {totals['PARTIAL']} partial, {totals['MISSING']} missing (of {totals['total']})"
    )
    print(f"Report: {out_dir / 'coverage_report.md'}")
    print(f"Matrix: {out_dir / 'coverage_matrix.json'}")
    print(f"Checklist: {out_dir / 'spec_checklist.json'}")

    if args.fail_on == "MISSING" and totals["MISSING"] > 0:
        print(f"\nFAILED: {totals['MISSING']} features are MISSING", file=sys.stderr)
        return 1
    if args.fail_on == "PARTIAL" and (totals["MISSING"] > 0 or totals["PARTIAL"] > 0):
        print(f"\nFAILED: {totals['MISSING']} MISSING + {totals['PARTIAL']} PARTIAL", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
