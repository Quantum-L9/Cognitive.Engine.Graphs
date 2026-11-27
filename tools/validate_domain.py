#!/usr/bin/env python3
"""Native CEG authoring validation with predecessor-compatible default/strict CLI.

Default CLI checks cross-references, the global weight ceiling, gate compilation,
traversal warnings and prohibited factors. --strict escalates warnings to exit 1.
--complete/--receipt select full native operator admission, not authoring policy.
--list-rules prints the admission rules a JSON Schema cannot express
(docs/contracts/data/models/domain-admission-rules.json) and validates nothing.
--dry-run validates one spec.yaml, or every <pack>/spec.yaml under a pack root,
with scoring_spec_strict_validation_enabled forced on for the run (FEATURE_GATES
§16, "Scoring spec strict validation"), so an operator sees which published
packs that flag would refuse before deploying it. It loads specs only; nothing
is served, written or provisioned, and the process settings are restored.
No YAML bytes, settings or graph semantics are changed by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config.loader import (
    DomainNotFoundError,
    DomainSpecError,
    _parse_native_bytes,
    validate_authoring_file,
    validate_native_file,
)
from tools.path_policy import PathPolicyError, resolve_root, resolve_under_root

RULES_PATH = Path(__file__).resolve().parent.parent / "docs/contracts/data/models/domain-admission-rules.json"


def admission_rules() -> dict[str, Any]:
    """The committed catalogue of non-schema admission rules, parsed, never recomputed."""
    catalogue: dict[str, Any] = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    return catalogue


def _print_rules(json_output: bool) -> None:
    catalogue = admission_rules()
    if json_output:
        print(json.dumps(catalogue, indent=2, ensure_ascii=False))
        return
    for rule in catalogue["rules"]:
        print(f"{rule['id']} [{rule['stage']}] {rule['statement']}")
    print(f"{len(catalogue['rules'])} rules ({catalogue['schema']})")


def validate_spec(
    spec_path: Path,
    strict: bool = False,
    *,
    tenant_id: str | None = None,
    complete: bool = False,
    require_reviewed: bool = False,
    receipt_path: Path | None = None,
    authoring: bool = False,
) -> tuple[list[dict], int]:
    """Return a deterministic loader-owner verdict, never an uncaught YAML error.

    Programmatic defaults retain native runtime parity (per-direction weights).
    The CLI selects `authoring=True`; `strict=True` also selects authoring and
    escalates warnings. Explicit complete/receipt admission takes precedence:
    those native operator checks already fail closed on traversal warnings.
    For folder-shaped spec.yaml, the parent name is the tenant lookup key;
    arbitrary input files derive the tenant from domain.id unless supplied.
    """
    expected = tenant_id
    if expected is None and spec_path.name == "spec.yaml":
        expected = spec_path.parent.name
    try:
        if (authoring or strict) and not complete and receipt_path is None:
            return validate_authoring_file(spec_path, expected, strict=strict, require_reviewed=require_reviewed)
        spec = validate_native_file(
            spec_path,
            expected,
            complete=complete,
            require_reviewed=require_reviewed,
            receipt_path=receipt_path,
        )
    except (DomainSpecError, DomainNotFoundError) as exc:
        return [{"check": "native-admission", "status": "FAIL", "message": str(exc)}], 1
    return [
        {
            "check": "native-admission",
            "status": "PASS",
            "message": f"Domain '{spec.domain.id}' admitted ({spec.domain.version})",
        }
    ], 0


def _dry_run_targets(path: Path) -> list[Path]:
    """The spec files a dry run checks: the file itself, or each ``<pack>/spec.yaml`` under a root."""
    if path.is_dir():
        return sorted(child for child in path.glob("*/spec.yaml") if child.is_file())
    return [path]


def _dry_run_one(target: Path) -> dict:
    """The schema verdict for one spec: the loader's bounded parse and schema, not admission withholding.

    Dormant / unvalidated posture is a serving decision, not a structural
    defect, so it is not reported here; ``validate_spec`` remains the full check.
    """
    tenant = target.parent.name if target.name == "spec.yaml" else None
    verdict = {"check": "strict-scoring-dry-run", "spec_path": str(target)}
    try:
        spec = _parse_native_bytes(target.read_bytes(), tenant)
    except (DomainSpecError, DomainNotFoundError, OSError) as exc:
        return {**verdict, "status": "REFUSED", "message": str(exc)}
    return {**verdict, "status": "PASS", "message": f"Domain '{spec.domain.id}' loads under strict scoring validation"}


def dry_run(path: Path) -> tuple[list[dict], int]:
    """Validate packs as the strict scoring schema would load them, serving nothing.

    ``scoring_spec_strict_validation_enabled`` is forced on for the duration and
    restored afterwards, so the verdict is the one the flag will give at deploy
    whatever the current environment sets. Exit code 1 when any pack is refused
    (or when ``path`` names no spec), else 0.
    """
    from engine.config.settings import settings

    targets = _dry_run_targets(path)
    if not targets:
        verdict = {"check": "strict-scoring-dry-run", "status": "FAIL", "spec_path": str(path)}
        return [{**verdict, "message": f"no spec.yaml under {path}"}], 1
    previous = settings.scoring_spec_strict_validation_enabled
    settings.scoring_spec_strict_validation_enabled = True
    try:
        results: list[dict] = []
        for target in targets:
            results.append(_dry_run_one(target))
    finally:
        settings.scoring_spec_strict_validation_enabled = previous
    code = 1 if any(result["status"] != "PASS" for result in results) else 0
    return results, code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate native CEG authoring or complete operator admission")
    parser.add_argument("spec_path", type=Path, nargs="?")
    parser.add_argument("--tenant-id", help="Expected tenant identity")
    parser.add_argument("--strict", action="store_true", help="Escalate authoring warnings to failures (exit 1)")
    parser.add_argument(
        "--complete", action="store_true", help="Full native traversal/scoring admission; no withholding overrides"
    )
    parser.add_argument("--require-reviewed", action="store_true", help="Require reviewed/production version suffix")
    parser.add_argument("--receipt", type=Path, help="Verify native approval receipt and exact reviewed bytes")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--root",
        default=".",
        help="Allowed root for the domain specification and receipt (default: current directory)",
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="Print the non-schema admission rule catalogue (with --json: the catalogue file) and exit 0",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Check one spec.yaml or every <pack>/spec.yaml under a pack root with strict scoring "
            "validation forced on; loads only, serves nothing (exit 1 if any pack is refused)"
        ),
    )
    args = parser.parse_args(argv)
    if args.list_rules:
        _print_rules(args.json_output)
        sys.exit(0)
    if args.spec_path is None:
        parser.error("the following arguments are required: spec_path")
    try:
        root = resolve_root(args.root)
        spec_path = resolve_under_root(args.spec_path, root, option="spec_path")
        receipt_path = resolve_under_root(args.receipt, root, option="--receipt") if args.receipt is not None else None
    except PathPolicyError as exc:
        parser.error(str(exc))

    if args.dry_run:
        results, code = dry_run(spec_path)
        if args.json_output:
            print(json.dumps({"results": results, "exit_code": code, "spec_path": str(spec_path)}, indent=2))
        else:
            for result in results:
                print(f"[{result['status']}] {result['spec_path']}: {result['message']}")
            print("Result: NO PACK REFUSED" if code == 0 else "Result: PACK(S) REFUSED")
        sys.exit(code)

    results, code = validate_spec(
        spec_path,
        strict=args.strict,
        tenant_id=args.tenant_id,
        complete=args.complete,
        require_reviewed=args.require_reviewed,
        receipt_path=receipt_path,
        authoring=True,
    )
    if args.json_output:
        print(json.dumps({"results": results, "exit_code": code, "spec_path": str(spec_path)}, indent=2))
    else:
        for result in results:
            print(f"[{result['status']}] {result['check']}: {result['message']}")
        if code == 0:
            print("Result: ALL CHECKS PASSED")
        elif code == 2:
            print("Result: WARNINGS (non-blocking)")
        else:
            print("Result: FAILURES DETECTED")
    sys.exit(code)


if __name__ == "__main__":
    main()
