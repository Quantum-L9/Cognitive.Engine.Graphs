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

Extracts required features from the graph-cognitive-engine spec YAML,
then scans engine/ code to produce a coverage matrix.

Two input sources:
    graph-cognitive-engine-spec-v1.1.0.yaml  — contracted spec features
    tools/research/                          — researched leverage patterns

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
import json
import re
import sys
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
GATES_GLOB = "engine/gates/**/*.py"
SCORING_GLOB = "engine/scoring/**/*.py"
ENGINE_GLOB = "engine/**/*.py"
DOMAINS_GLOB = "tests/fixtures/design_packs/**/*.yaml"
COVERAGE_MATRIX_JSON = "coverage_matrix.json"


class Status(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


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


def load_yaml_file(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required: bash tools/deps/install_locked.sh tools")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_get(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d


def extract_gate_features(spec: dict) -> list[SpecFeature]:
    features = []
    gates_section = deep_get(spec, "gates", default={})

    if isinstance(gates_section, dict):
        gate_types = gates_section.get("types", gates_section.get("gate_types", {}))
        if isinstance(gate_types, dict):
            for gate_name, gate_def in gate_types.items():
                features.append(
                    SpecFeature(
                        category="gates",
                        name=gate_name,
                        spec_reference=f"gates.types.{gate_name}",
                        search_tokens=[
                            gate_name,
                            gate_name.replace("_", ""),
                            f"class {gate_name.title().replace('_', '')}Gate",
                            f'"{gate_name}"',
                            f"'{gate_name}'",
                            f"GateType.{gate_name.upper()}",
                        ],
                        search_files=[GATES_GLOB],
                    )
                )
        elif isinstance(gate_types, list):
            for item in gate_types:
                name = item if isinstance(item, str) else item.get("type", item.get("name", str(item)))
                features.append(
                    SpecFeature(
                        category="gates",
                        name=str(name),
                        spec_reference=f"gates.types.{name}",
                        search_tokens=[
                            str(name),
                            str(name).replace("_", ""),
                            f"class {str(name).title().replace('_', '')}Gate",
                            f'"{name}"',
                            f"GateType.{str(name).upper()}",
                        ],
                        search_files=[GATES_GLOB],
                    )
                )

    if not features:
        all_text = json.dumps(spec)
        known_gates = [
            "range",
            "threshold",
            "boolean",
            "composite",
            "enum_map",
            "exclusion",
            "self_range",
            "freshness",
            "temporal_range",
            "traversal",
            "multi_range",
            "weighted_enum",
            "geo_radius",
            "set_intersection",
        ]
        for g in known_gates:
            if g in all_text or g.replace("_", "") in all_text:
                features.append(
                    SpecFeature(
                        category="gates",
                        name=g,
                        spec_reference="gates (detected in spec text)",
                        search_tokens=[g, g.replace("_", ""), f'"{g}"', f"GateType.{g.upper()}"],
                        search_files=[GATES_GLOB],
                    )
                )

    return features


def extract_scoring_features(spec: dict) -> list[SpecFeature]:
    features = []
    scoring = deep_get(spec, "scoring", default={})

    if isinstance(scoring, dict):
        dims = scoring.get("dimensions", scoring.get("computation_types", scoring.get("types", {})))
        if isinstance(dims, (dict, list)):
            items = dims.items() if isinstance(dims, dict) else enumerate(dims)
            for key, val in items:
                non_str_name = val.get("type", val.get("name", str(key))) if isinstance(val, dict) else str(val)
                name = val if isinstance(val, str) else non_str_name
                features.append(
                    SpecFeature(
                        category="scoring",
                        name=str(name),
                        spec_reference=f"scoring.{name}",
                        search_tokens=[
                            str(name),
                            str(name).replace("_", ""),
                            f'"{name}"',
                            f"ScoringType.{str(name).upper()}",
                        ],
                        search_files=[SCORING_GLOB],
                    )
                )

    if not features:
        all_text = json.dumps(spec)
        known_scoring = [
            "geo_decay",
            "log_normalized",
            "community_match",
            "inverse_linear",
            "candidate_property",
            "custom_cypher",
            "temporal_decay",
            "outcome_weighted",
            "set_overlap",
        ]
        for s in known_scoring:
            if s in all_text or s.replace("_", "") in all_text:
                features.append(
                    SpecFeature(
                        category="scoring",
                        name=s,
                        spec_reference="scoring (detected in spec text)",
                        search_tokens=[s, s.replace("_", ""), f'"{s}"'],
                        search_files=[SCORING_GLOB],
                    )
                )

    return features


def extract_ontology_features(spec: dict) -> list[SpecFeature]:
    features = []
    ontology = deep_get(spec, "ontology", default={})

    for section_key, category_label in [("nodes", "ontology_node"), ("edges", "ontology_edge")]:
        items = ontology.get(section_key, [])
        if isinstance(items, list):
            for item in items:
                name = (
                    item if isinstance(item, str) else item.get("label", item.get("type", item.get("name", str(item))))
                )
                features.append(
                    SpecFeature(
                        category=category_label,
                        name=str(name),
                        spec_reference=f"ontology.{section_key}.{name}",
                        search_tokens=[f'"{name}"', f"'{name}'", str(name)],
                        search_files=[ENGINE_GLOB, DOMAINS_GLOB],
                    )
                )
        elif isinstance(items, dict):
            for name in items:
                features.append(
                    SpecFeature(
                        category=category_label,
                        name=str(name),
                        spec_reference=f"ontology.{section_key}.{name}",
                        search_tokens=[f'"{name}"', f"'{name}'", str(name)],
                        search_files=[ENGINE_GLOB, DOMAINS_GLOB],
                    )
                )

    return features


def extract_v11_additions(spec: dict) -> list[SpecFeature]:
    v11_nodes = ["TransactionOutcome", "SignalEvent"]
    v11_edges = ["RESULTED_IN", "RESOLVED_FROM"]
    v11_endpoints = ["outcomes", "resolve"]
    v11_scoring = ["temporal_decay", "outcome_weighted"]

    features = []
    for node in v11_nodes:
        features.append(
            SpecFeature(
                category="v1.1_node",
                name=node,
                spec_reference=f"v1.1 addition: {node} node",
                search_tokens=[node, f'"{node}"', f"'{node}'"],
                search_files=[ENGINE_GLOB, DOMAINS_GLOB],
            )
        )
    for edge in v11_edges:
        features.append(
            SpecFeature(
                category="v1.1_edge",
                name=edge,
                spec_reference=f"v1.1 addition: {edge} edge",
                search_tokens=[edge, f'"{edge}"', f"'{edge}'"],
                search_files=[ENGINE_GLOB, DOMAINS_GLOB],
            )
        )
    for ep in v11_endpoints:
        features.append(
            SpecFeature(
                category="v1.1_action",
                name=ep,
                spec_reference=f"v1.1 addition: {ep} action/endpoint",
                search_tokens=[f"handle_{ep}", f'"{ep}"', f"'{ep}'", ep],
                search_files=["engine/handlers.py", ENGINE_GLOB],
            )
        )
    for sc in v11_scoring:
        features.append(
            SpecFeature(
                category="v1.1_scoring",
                name=sc,
                spec_reference=f"v1.1 addition: {sc} scoring type",
                search_tokens=[sc, sc.replace("_", ""), f'"{sc}"'],
                search_files=[SCORING_GLOB],
            )
        )

    return features


def extract_action_features(spec: dict) -> list[SpecFeature]:
    actions = ["match", "sync", "admin", "query", "enrich", "healthcheck"]
    features = []
    for action in actions:
        features.append(
            SpecFeature(
                category="action_handler",
                name=action,
                spec_reference=f"chassis action: {action}",
                search_tokens=[f"handle_{action}", f'"{action}"', f'register_handler("{action}"'],
                search_files=["engine/handlers.py"],
            )
        )
    return features


def extract_gds_features(spec: dict) -> list[SpecFeature]:
    gds = deep_get(spec, "gds_jobs", default=deep_get(spec, "gds", default={}))
    features = []

    known_algos = [
        "louvain",
        "cooccurrence",
        "reinforcement",
        "temporal_recency",
        "similarity",
        "pagerank",
        "label_propagation",
    ]

    if isinstance(gds, dict):
        jobs = gds.get("jobs", gds.get("algorithms", []))
        if isinstance(jobs, list):
            for job in jobs:
                name = job if isinstance(job, str) else job.get("algorithm", job.get("name", str(job)))
                features.append(
                    SpecFeature(
                        category="gds_algorithm",
                        name=str(name),
                        spec_reference=f"gds_jobs.{name}",
                        search_tokens=[str(name), f"_run_{name}", f'"{name}"'],
                        search_files=["engine/gds/**/*.py"],
                    )
                )

    if not features:
        all_text = json.dumps(spec)
        for algo in known_algos:
            if algo in all_text:
                features.append(
                    SpecFeature(
                        category="gds_algorithm",
                        name=algo,
                        spec_reference="gds (detected in spec text)",
                        search_tokens=[algo, f"_run_{algo}", f'"{algo}"'],
                        search_files=["engine/gds/**/*.py"],
                    )
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
    """Every feature the coverage report counts: the spec's, then the research patterns, each once."""
    features: list[SpecFeature] = []
    features += extract_gate_features(spec)
    features += extract_scoring_features(spec)
    features += extract_ontology_features(spec)
    features += extract_v11_additions(spec)
    features += extract_action_features(spec)
    features += extract_gds_features(spec)
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


def scan_codebase(root: Path, features: list[SpecFeature]) -> None:
    py_cache: dict[str, str] = {}
    yaml_cache: dict[str, str] = {}

    for py_file in root.rglob("*.py"):
        if ".venv" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        try:
            safe_py_file = resolve_under_root(py_file, root, option="discovered Python file")
            py_cache[str(py_file.relative_to(root))] = safe_py_file.read_text(encoding="utf-8", errors="replace")
        except (OSError, PathPolicyError):
            # Unreadable file (permissions, transient IO): skip it and keep
            # scanning the rest of the tree.
            pass

    for yaml_file in root.rglob("*.yaml"):
        if ".venv" in yaml_file.parts:
            continue
        try:
            safe_yaml_file = resolve_under_root(yaml_file, root, option="discovered YAML file")
            yaml_cache[str(yaml_file.relative_to(root))] = safe_yaml_file.read_text(encoding="utf-8", errors="replace")
        except (OSError, PathPolicyError):
            # Unreadable file (permissions, transient IO): skip it and keep
            # scanning the rest of the tree.
            pass

    all_files = {**py_cache, **yaml_cache}

    for feature in features:
        matched_files = []
        matched_lines = []

        # Scope = union of the files matched by the declared search_files. An empty
        # scoped set means MISSING; it never widens to the whole tree.
        search_scope = all_files
        if feature.search_files:
            search_scope = {
                fpath: content
                for fpath, content in all_files.items()
                if any(matches_search_file(fpath, glob_pat) for glob_pat in feature.search_files)
            }

        for fpath, content in search_scope.items():
            for token in feature.search_tokens:
                if token.lower() in content.lower():
                    matched_files.append(fpath)
                    for i, line in enumerate(content.splitlines(), 1):
                        if token.lower() in line.lower():
                            matched_lines.append(f"{fpath}:{i}")
                    break

        feature.evidence_files = sorted(set(matched_files))
        feature.evidence_lines = matched_lines[:10]

        if len(feature.evidence_files) >= 2:
            feature.status = Status.IMPLEMENTED.value
        elif len(feature.evidence_files) == 1:
            feature.status = Status.PARTIAL.value
        else:
            feature.status = Status.MISSING.value


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
    write_text_no_follow(out_dir, COVERAGE_MATRIX_JSON, json.dumps(matrix, indent=2))


def write_coverage_report(out_dir: Path, features: list[SpecFeature], matrix: dict, root: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# L9 Spec Coverage Report")
    lines.append(f"\n- Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"- Template tag: `{L9_TEMPLATE_TAG}`")
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
            if f.evidence_files:
                lines.append(f"- Found in: {', '.join(f'`{e}`' for e in f.evidence_files)}")
            if f.evidence_lines:
                lines.append(f"- Lines: {', '.join(f.evidence_lines[:5])}")
            else:
                lines.append("- **No code references found**")
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

    scan_codebase(root, features)

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
