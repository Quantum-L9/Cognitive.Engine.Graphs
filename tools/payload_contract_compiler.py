#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [audit]
tags: [contracts, compiler, validator, plasticos]
owner: engine-team
status: active
--- /L9_META ---

CEG payload contract compiler validator (TASK-034 / ADR-109).

Validates live PlasticOS payload schemas and fixtures against native models.
Emits a deterministic digest report. Does not create a parallel domain cartridge
or tensor runtime — DomainPackLoader remains the sole domain authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
PAYLOADS = ROOT / "contracts" / "payloads"
EXAMPLES = PAYLOADS / "examples"
NEGATIVES = PAYLOADS / "negative_examples"
DEFAULT_REPORT = ROOT / "artifacts" / "payload-contract-compiler-report.json"

SCHEMA_TO_MODEL: dict[str, str] = {
    "match-request.schema.yaml": "MatchRequest",
    "match-response.schema.yaml": "MatchResponse",
    "improvement-proposal.schema.yaml": "ImprovementProposal",
    "sync-projection.schema.yaml": "SyncProjection",
    "canonical-projection.schema.yaml": "CanonicalProjection",
    "outcome-feedback.schema.yaml": "OutcomeFeedback",
}

SEMANTIC_NEGATIVES = frozenset(
    {
        "match-response-ineligible-ranked.json",
        "improvement-proposal-direct-mutation.json",
    }
)

FORBIDDEN_TOKENS = (
    "PacketEnvelope",
    "packet.schema",
    "legacy_request",
    "peer_url_dispatch",
    "DomainSpecLoader",
)


def _sha_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_obj(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"schema root must be object: {path}")
    return data


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _models() -> dict[str, type]:
    from engine.models.payloads import (
        CanonicalProjection,
        ImprovementProposal,
        MatchRequest,
        MatchResponse,
        OutcomeFeedback,
        SyncProjection,
    )

    return {
        "MatchRequest": MatchRequest,
        "MatchResponse": MatchResponse,
        "ImprovementProposal": ImprovementProposal,
        "SyncProjection": SyncProjection,
        "CanonicalProjection": CanonicalProjection,
        "OutcomeFeedback": OutcomeFeedback,
    }


def validate_schemas() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(PAYLOADS.glob("*.schema.yaml")):
        schema = _load_yaml(path)
        entry: dict[str, Any] = {
            "path": str(path.relative_to(ROOT)),
            "id": schema.get("$id"),
            "digest": _sha_file(path),
        }
        try:
            Draft202012Validator.check_schema(schema)
            entry["check_schema"] = "PASS"
        except Exception as exc:
            entry["check_schema"] = "FAIL"
            entry["error"] = str(exc)
        text = path.read_text(encoding="utf-8")
        hits = [token for token in FORBIDDEN_TOKENS if token in text]
        props = set((schema.get("properties") or {}).keys())
        from engine.models.payloads import FORBIDDEN_TRANSPORT_FIELDS

        transport_hits = sorted(props & set(FORBIDDEN_TRANSPORT_FIELDS))
        entry["forbidden_token_hits"] = hits
        entry["transport_property_hits"] = transport_hits
        entry["status"] = "PASS" if entry["check_schema"] == "PASS" and not hits and not transport_hits else "FAIL"
        results.append(entry)
    return results


def validate_fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    models = _models()
    positives: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []

    example_aliases = {
        "sync-projection-tombstone": "sync-projection.schema.yaml",
    }

    for path in sorted(EXAMPLES.glob("*.json")):
        stem = path.name.replace(".json", "")
        schema_name = example_aliases.get(stem, f"{stem}.schema.yaml")
        model_name = SCHEMA_TO_MODEL.get(schema_name)
        entry: dict[str, Any] = {"file": str(path.relative_to(ROOT)), "schema": schema_name}
        if model_name is None:
            entry["status"] = "FAIL"
            entry["error"] = "no model mapping"
            positives.append(entry)
            continue
        try:
            models[model_name].model_validate(_load_json(path))
            entry["status"] = "PASS"
            entry["model"] = model_name
        except ValidationError as exc:
            entry["status"] = "FAIL"
            entry["error"] = str(exc)[:400]
        positives.append(entry)

    for path in sorted(NEGATIVES.glob("*.json")):
        entry = {"file": str(path.relative_to(ROOT))}
        payload = _load_json(path)
        model = None
        for schema_name, model_name in SCHEMA_TO_MODEL.items():
            prefix = schema_name.replace(".schema.yaml", "")
            if path.name.startswith(prefix):
                model = models[model_name]
                entry["model"] = model_name
                break
        if model is None:
            entry["status"] = "FAIL"
            entry["error"] = "no model mapping"
            negatives.append(entry)
            continue
        try:
            model.model_validate(payload)
            entry["status"] = "FAIL"
            entry["detail"] = "incorrectly_accepted"
        except ValidationError:
            entry["status"] = "PASS"
            entry["detail"] = "rejected_as_expected"
            if path.name in SEMANTIC_NEGATIVES:
                entry["enforcement"] = "owner_semantic"
        negatives.append(entry)

    return positives, negatives


def validate_domain_authority() -> dict[str, Any]:
    """DomainPackLoader must load plasticos; no parallel cartridge authority."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader()
    domain = loader.load_domain("plasticos")
    dumped = domain.model_dump()
    directions = list(
        (dumped.get("queryschema") or {}).get("matchdirections")
        or (dumped.get("query_schema") or {}).get("match_directions")
        or []
    )
    return {
        "loader": "DomainPackLoader",
        "domain_id": "plasticos",
        "spec_path": "domains/plasticos/spec.yaml",
        "loaded": True,
        "match_directions": directions,
        "parallel_cartridge_forbidden": True,
        "status": "PASS" if directions else "FAIL",
    }


def compile_report(*, write_path: Path | None) -> dict[str, Any]:
    schema_results = validate_schemas()
    positives, negatives = validate_fixtures()
    domain = validate_domain_authority()
    report: dict[str, Any] = {
        "schema": "l9.ceg.payload_contract_compiler.v1",
        "task_id": "TASK-034",
        "authority": "DomainPackLoader + contracts/payloads (no parallel cartridge)",
        "schemas": schema_results,
        "positives": positives,
        "negatives": negatives,
        "domain_authority": domain,
    }
    ok = (
        all(r["status"] == "PASS" for r in schema_results)
        and all(r["status"] == "PASS" for r in positives)
        and all(r["status"] == "PASS" for r in negatives)
        and domain["status"] == "PASS"
    )
    report["result"] = "PASS" if ok else "FAIL"
    report["digest"] = _sha_obj({k: v for k, v in report.items() if k != "digest"})
    if write_path is not None:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CEG payload contract compiler validator")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Write JSON report to this path (default: artifacts/payload-contract-compiler-report.json)",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Do not write report file",
    )
    args = parser.parse_args(argv)
    report = compile_report(write_path=None if args.stdout_only else args.report)
    print(json.dumps({"result": report["result"], "digest": report["digest"]}, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
