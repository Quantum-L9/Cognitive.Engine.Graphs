"""
--- L9_META ---
l9_schema: 1
origin: l9-template
engine: graph
layer: [audit]
tags: [L9_TEMPLATE, audit, compliance]
owner: platform
status: active
--- /L9_META ---
"""

import ast
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None


L9_TEMPLATE_TAG = "L9_TEMPLATE"


@dataclass
class Finding:
    severity: str
    rule_id: str
    file: str
    line_start: int | None
    line_end: int | None
    issue: str
    evidence: str
    fix: str


SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML not installed. Install pyyaml to run audit.")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# Bounded per-run caches. The audit is a single-pass, read-only scan, so the
# same source file and the same include/exclude glob set are resolved once and
# reused across every rule that references them (many rules share
# `engine/**/*.py`). This removes redundant disk reads and directory walks
# without changing findings — read_text is pure for a given path within a run.
@lru_cache(maxsize=8192)
def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@lru_cache(maxsize=2048)
def _list_files_cached(
    root: Path,
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
) -> tuple[Path, ...]:
    # Path.glob implements "**" as "this directory and every subdirectory", so
    # "engine/**/*.py" selects every module under engine/ at any depth. Include
    # and exclude patterns resolve the same way.
    included: set[Path] = set()
    for pat in include_globs:
        included |= set(root.glob(pat))
    excluded: set[Path] = set()
    for pat in exclude_globs:
        excluded |= set(root.glob(pat))
    files = [p for p in included if p.is_file() and p not in excluded]
    return tuple(sorted(files))


def list_files(root: Path, include_globs: list[str], exclude_globs: list[str]) -> list[Path]:
    return list(_list_files_cached(root, tuple(include_globs), tuple(exclude_globs)))


def snippet_with_lines(text: str, line_no: int, context: int = 3) -> tuple[int, int, str]:
    lines = text.splitlines()
    start = max(1, line_no - context)
    end = min(len(lines), line_no + context)
    block = []
    for i in range(start, end + 1):
        prefix = ">>" if i == line_no else "  "
        block.append(f"{prefix} {i:4d}: {lines[i - 1]}")
    return start, end, "\n".join(block)


def find_all_lines(text: str, needle: str) -> list[int]:
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines, start=1):
        if needle in line:
            hits.append(i)
    return hits


def find_regex_lines(text: str, pattern: str) -> list[int]:
    rx = re.compile(pattern, re.MULTILINE | re.DOTALL)
    hits = []
    for m in rx.finditer(text):
        idx = text[: m.start()].count("\n") + 1
        hits.append(idx)
    return hits


def _cypher_lint() -> Any:
    """tools/cypher_lint.py, the C-009 owner, whether this runs as a script or as tools.audit_engine."""
    try:
        from tools import cypher_lint
    except ImportError:  # run as `python tools/audit_engine.py`: tools/ is sys.path[0]
        import cypher_lint  # type: ignore[import-not-found,no-redef]
    return cypher_lint


def cypher_lint_findings(root: Path, files: list[Path], rule: dict[str, Any]) -> list[Finding]:
    """Blocking tools/cypher_lint.py findings in *files*, as findings of *rule*.

    The label check is cypher_lint's AST provenance analysis (DEC-50 option A),
    so it is evaluated per interpolated expression: it never matches across
    lines, and a sanitize_label() call elsewhere in a file exempts nothing.
    Findings waived with ``# cypher-lint: allow <reason>`` are not reported. A
    module that cannot be parsed is reported, never skipped.
    """
    lint = _cypher_lint()
    findings: list[Finding] = []
    for f in files:
        text = read_text(f)
        rel = f.relative_to(root).as_posix()
        try:
            results = lint.scan_source(text, rel_path=rel)
        except SyntaxError as exc:
            findings.append(
                Finding(
                    severity=rule["severity"],
                    rule_id=rule["id"],
                    file=str(f),
                    line_start=exc.lineno,
                    line_end=exc.lineno,
                    issue=rule["description"],
                    evidence=f"cypher_lint could not parse the module: {exc.msg}",
                    fix="Fix the syntax error so the C-009 label check can run.",
                )
            )
            continue
        for item in results:
            if not item.blocking:
                continue
            s, e, snip = snippet_with_lines(text, item.line_no)
            findings.append(
                Finding(
                    severity=rule["severity"],
                    rule_id=rule["id"],
                    file=str(f),
                    line_start=s,
                    line_end=e,
                    issue=rule["description"],
                    evidence=f"{item.kind}: {{{item.expression}}}\n{snip}",
                    fix="Wrap labels/types with sanitize_label() before Cypher interpolation.",
                )
            )
    return findings


def ensure_template_manifest(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    manifest = root / "tools" / "l9_template_manifest.yaml"
    if not manifest.exists():
        findings.append(
            Finding(
                severity="HIGH",
                rule_id="TEMPLATE_MANIFEST_MISSING",
                file=str(manifest),
                line_start=None,
                line_end=None,
                issue="Template manifest missing.",
                evidence="tools/l9_template_manifest.yaml does not exist.",
                fix="Add tools/l9_template_manifest.yaml with L9_TEMPLATE tagging for template files.",
            )
        )
    return findings


# Keys that narrow a rule's required_all / required_any check from the whole
# file to the source of one module-level definition in it. A flow anchor such
# as "handle_match constructs GateCompiler" is a statement about one function,
# not about a file that happens to mention GateCompiler somewhere else.
WITHIN_KEYS: dict[str, tuple[type[ast.AST], ...]] = {
    "within_function": (ast.FunctionDef, ast.AsyncFunctionDef),
    "within_class": (ast.ClassDef,),
}


def rule_within(rule: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(key, name)`` definition a rule's required tokens are scoped to, if any.

    Raises ValueError for a rule that sets more than one scope key, or sets one
    without required_all / required_any (it would narrow nothing).
    """
    keys = [k for k in WITHIN_KEYS if rule.get(k)]
    if not keys:
        return None
    if len(keys) > 1:
        msg = f"rule {rule['id']}: set at most one of {sorted(WITHIN_KEYS)}, got {keys}"
        raise ValueError(msg)
    if not (rule.get("required_all") or rule.get("required_any")):
        msg = f"rule {rule['id']}: {keys[0]} needs required_all or required_any"
        raise ValueError(msg)
    return keys[0], str(rule[keys[0]])


def definition_source(text: str, key: str, name: str) -> tuple[str, int, int] | None:
    """Source, first and last line of the module-level definition *name* of kind *key*.

    Only module-level definitions count: a nested helper of the same name is
    not the lifecycle entrypoint the rule names. Raises SyntaxError when *text*
    does not parse.
    """
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, WITHIN_KEYS[key]) and getattr(node, "name", None) == name:
            segment = ast.get_source_segment(text, node) or ""
            return segment, node.lineno, node.end_lineno or node.lineno
    return None


def required_token_findings(rule: dict[str, Any], f: Path, text: str) -> list[Finding]:
    """required_all / required_any findings for one file, scoped by the rule's within_* key."""
    required_all = rule.get("required_all", [])
    required_any = rule.get("required_any", [])
    if not (required_all or required_any):
        return []

    def finding(evidence: str, fix: str, start: int | None = None, end: int | None = None) -> Finding:
        return Finding(
            severity=rule["severity"],
            rule_id=rule["id"],
            file=str(f),
            line_start=start,
            line_end=end,
            issue=rule["description"],
            evidence=evidence,
            fix=fix,
        )

    scope_text, where, start, end = text, "", None, None
    within = rule_within(rule)
    if within is not None:
        key, name = within
        kind = key.removeprefix("within_")
        try:
            found = definition_source(text, key, name)
        except SyntaxError as exc:
            return [finding(f"Module could not be parsed to locate {kind} {name}: {exc.msg}", "Fix the syntax error.")]
        if found is None:
            return [
                finding(
                    f"Module-level {kind} `{name}` is not defined in this file.",
                    f"Restore `{name}` here or re-point the rule at the module that now owns the flow.",
                )
            ]
        scope_text, start, end = found
        where = f" in {kind} `{name}`"

    findings: list[Finding] = []
    missing = [x for x in required_all if x not in scope_text]
    if missing:
        findings.append(
            finding(
                f"Missing required tokens{where}: {missing}",
                "Implement required flow anchors or algorithms as per engine contract.",
                start,
                end,
            )
        )
    if required_any and not any(x in scope_text for x in required_any):
        findings.append(
            finding(
                f"None of required-any tokens found{where}: {required_any}",
                "Ensure lifecycle entrypoints reference expected components.",
                start,
                end,
            )
        )
    return findings


def audit_rules(root: Path, rules: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    for rule in rules.get("rules", []):
        rule_id = rule["id"]
        severity = rule["severity"]
        desc = rule["description"]

        forbidden = rule.get("file_exists_forbidden", [])
        for rel in forbidden:
            p = root / rel
            if p.exists():
                findings.append(
                    Finding(
                        severity=severity,
                        rule_id=rule_id,
                        file=str(p),
                        line_start=None,
                        line_end=None,
                        issue=desc,
                        evidence=f"{rel} exists.",
                        fix="Delete this directory/file; engines must not implement custom HTTP surface.",
                    )
                )

        include_globs = rule.get("include_globs", [])
        if include_globs:
            exclude_globs = rule.get("exclude_globs", [])
            files = list_files(root, include_globs, exclude_globs)

            patterns = rule.get("patterns", [])
            patterns_regex = rule.get("patterns_regex", [])
            allow_if_contains = rule.get("allow_if_contains", [])

            if rule.get("check") == "cypher_lint":
                findings += cypher_lint_findings(root, files, rule)

            # A required-token rule whose scope matches nothing would pass
            # vacuously: renaming or moving the anchored module must fail the
            # rule, not silently retire it.
            if not files and (rule.get("required_all") or rule.get("required_any")):
                findings.append(
                    Finding(
                        severity=severity,
                        rule_id=rule_id,
                        file=", ".join(include_globs),
                        line_start=None,
                        line_end=None,
                        issue=desc,
                        evidence=f"Rule scope matched no files: include_globs={include_globs}",
                        fix="Restore the anchored module or re-point the rule's include_globs at its new home.",
                    )
                )

            for f in files:
                text = read_text(f)

                findings += required_token_findings(rule, f, text)

                for needle in patterns:
                    for ln in find_all_lines(text, needle):
                        s, e, snip = snippet_with_lines(text, ln)
                        findings.append(
                            Finding(
                                severity=severity,
                                rule_id=rule_id,
                                file=str(f),
                                line_start=s,
                                line_end=e,
                                issue=desc,
                                evidence=snip,
                                fix="Remove banned import/logic; rely on chassis for HTTP/tenancy/auth.",
                            )
                        )

                for rx_pat in patterns_regex:
                    hit_lines = find_regex_lines(text, rx_pat)
                    for ln in hit_lines:
                        if allow_if_contains and any(a in text for a in allow_if_contains):
                            continue
                        s, e, snip = snippet_with_lines(text, ln)
                        findings.append(
                            Finding(
                                severity=severity,
                                rule_id=rule_id,
                                file=str(f),
                                line_start=s,
                                line_end=e,
                                issue=desc,
                                evidence=snip,
                                fix="Wrap labels/types with sanitize_label() before Cypher interpolation.",
                            )
                        )

    return findings


def group_findings(findings: list[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
    for f in findings:
        grouped.setdefault(f.severity, []).append(f)
    for sev in grouped:
        grouped[sev] = sorted(grouped[sev], key=lambda x: (x.file, x.line_start or 0))
    return grouped


def write_report(root: Path, meta: dict[str, Any], grouped: dict[str, list[Finding]]) -> None:
    out_dir = root / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "audit_report.md"

    lines: list[str] = []
    lines.append("# L9 Engine Audit Report\n")
    lines.append(f"- Generated: {meta['generated_at']}")
    lines.append(f"- Repo root: `{meta['repo_root']}`")
    lines.append(f"- Template tag: `{L9_TEMPLATE_TAG}`")
    lines.append("")

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        lines.append(f"## {sev}")
        if not grouped.get(sev):
            lines.append("No findings.")
            lines.append("")
            continue

        for f in grouped[sev]:
            loc = ""
            if f.line_start is not None and f.line_end is not None:
                loc = f" (lines {f.line_start}-{f.line_end})"
            lines.append(f"### {f.rule_id}")
            lines.append(f"- File: `{f.file}`{loc}")
            lines.append(f"- Issue: {f.issue}")
            lines.append(f"- Fix: {f.fix}")
            lines.append("")
            lines.append("```")
            lines.append(f.evidence)
            lines.append("```")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    root = Path().resolve()
    meta = {"generated_at": now_iso(), "repo_root": str(root)}

    findings = []
    findings += ensure_template_manifest(root)

    rules_path = root / "tools" / "audit_rules.yaml"
    if not rules_path.exists():
        print("ERROR: tools/audit_rules.yaml missing", file=sys.stderr)
        return 2

    rules = load_yaml(rules_path)
    try:
        findings += audit_rules(root, rules)
    except ValueError as exc:  # a malformed rule is an infrastructure error, not a clean audit
        print(f"ERROR: tools/audit_rules.yaml: {exc}", file=sys.stderr)
        return 2

    grouped = group_findings(findings)
    write_report(root, meta, grouped)
    # Severity counts live in audit_report.md (parsed by the harness). This tool
    # deliberately does NOT write artifacts/coverage_matrix.json: that file is the
    # spec-coverage matrix owned by tools/spec_extract.py. Writing an audit
    # severity summary to the same name collided with spec_extract's output and
    # only appeared harmless because the harness ran the two steps serially.

    critical = len(grouped.get("CRITICAL", []))
    high = len(grouped.get("HIGH", []))

    # --- Spec coverage (if spec exists) ---
    import subprocess

    spec_candidates = list(root.glob("*spec*.yaml")) + list(root.glob("*spec*.yml"))
    if spec_candidates:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "spec_extract.py"),
                "--spec",
                str(spec_candidates[0]),
                "--root",
                str(root),
                "--fail-on",
                "NONE",
            ],  # don't double-fail; audit_engine owns exit code
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"spec_extract warning: {result.stderr}", file=sys.stderr)

    if critical > 0:
        return 1
    if high > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
