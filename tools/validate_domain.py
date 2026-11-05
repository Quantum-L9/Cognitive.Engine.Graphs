#!/usr/bin/env python3
"""Native CEG authoring validation with predecessor-compatible default/strict CLI.

Default CLI checks cross-references, the global weight ceiling, gate compilation,
traversal warnings and prohibited factors. --strict escalates warnings to exit 1.
--complete/--receipt select full native operator admission, not authoring policy.
No YAML bytes, settings or graph semantics are changed by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config.loader import DomainNotFoundError, DomainSpecError, validate_authoring_file, validate_native_file
from tools.path_policy import PathPolicyError, resolve_root, resolve_under_root


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate native CEG authoring or complete operator admission")
    parser.add_argument("spec_path", type=Path)
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
    args = parser.parse_args(argv)
    try:
        root = resolve_root(args.root)
        spec_path = resolve_under_root(args.spec_path, root, option="spec_path")
        receipt_path = resolve_under_root(args.receipt, root, option="--receipt") if args.receipt is not None else None
    except PathPolicyError as exc:
        parser.error(str(exc))

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
