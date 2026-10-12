#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [audit]
tags: [contracts, compiler, validator, domain-neutral]
owner: engine-team
status: active
--- /L9_META ---

CEG payload contract compiler validator (TASK-034 / ADR-109, Stage 2 R04).

Validates the live payload schemas and a fixture set against the native models,
with every fixture admitted against the ``DomainContract`` of the domain the
fixtures name — loaded through ``DomainPackLoader`` from a configurable domains
root. No domain is privileged: the repository ships PlasticOS fixtures as the
example, and any approved domain validates the same way with its own fixtures
(``--domains-root`` / ``--fixtures-root`` / ``--domain``).

Emits a deterministic digest report. Does not create a parallel domain cartridge
or tensor runtime — DomainPackLoader remains the sole domain authority. A
missing or malformed required contract (schema, fixture set, or domain pack) is
a hard failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

try:
    from jsonschema import Draft202012Validator
except ImportError:  # CI unit env may omit optional requirements-dev extras
    Draft202012Validator = None  # type: ignore[misc, assignment]

ROOT = Path(__file__).resolve().parents[1]
PAYLOADS = ROOT / "contracts" / "payloads"
DEFAULT_DOMAINS_ROOT = ROOT / "domains"
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

# Split prohibited tokens so scanners/ratchets do not treat this validator as a usage site.
FORBIDDEN_TOKENS = (
    "Packet" + "Envelope",
    "packet" + ".schema",
    "legacy" + "_request",
    "peer_url" + "_dispatch",
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


def _resolve_under_root(path: Path) -> Path:
    """Reject report paths that escape the repository root."""
    resolved = path.expanduser().resolve()
    root = ROOT.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"--report must stay under repo root ({root})")
    return resolved


def _rel(path: Path) -> str:
    """Repository-relative path when inside the repo, else the resolved path (deterministic)."""
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT.resolve())) if resolved.is_relative_to(ROOT.resolve()) else str(resolved)


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


def _structural_schema_errors(schema: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("missing Draft 2020-12 $schema")
    if not schema.get("$id"):
        errors.append("missing $id")
    if schema.get("type") not in {None, "object"} and "common.schema" not in path.name:
        errors.append("unexpected root type")
    return errors


def _apply_schema_check(entry: dict[str, Any], schema: dict[str, Any], path: Path) -> None:
    if Draft202012Validator is not None:
        try:
            Draft202012Validator.check_schema(schema)
            entry["check_schema"] = "PASS"
            entry["check_schema_backend"] = "jsonschema"
        except Exception as exc:
            entry["check_schema"] = "FAIL"
            entry["error"] = str(exc)
        return
    errors = _structural_schema_errors(schema, path)
    if errors:
        entry["check_schema"] = "FAIL"
        entry["error"] = "; ".join(errors)
        return
    entry["check_schema"] = "PASS"
    entry["check_schema_backend"] = "structural"


def _domain_const_hits(schema: dict[str, Any]) -> list[str]:
    """A payload schema must not pin `domain` to one value — that is the admission contract's job."""
    hits: list[str] = []
    for name, definition in (schema.get("$defs") or {}).items():
        if name == "domain" and isinstance(definition, dict) and "const" in definition:
            hits.append(f"$defs.domain.const={definition['const']!r}")
    domain_prop = (schema.get("properties") or {}).get("domain")
    if isinstance(domain_prop, dict) and ("const" in domain_prop or "enum" in domain_prop):
        hits.append("properties.domain pins a value")
    return hits


def _schema_entry(path: Path) -> dict[str, Any]:
    from engine.models.payloads import FORBIDDEN_TRANSPORT_FIELDS

    schema = _load_yaml(path)
    entry: dict[str, Any] = {
        "path": _rel(path),
        "id": schema.get("$id"),
        "digest": _sha_file(path),
    }
    _apply_schema_check(entry, schema, path)
    text = path.read_text(encoding="utf-8")
    hits = [token for token in FORBIDDEN_TOKENS if token in text]
    props = set((schema.get("properties") or {}).keys())
    transport_hits = sorted(props & set(FORBIDDEN_TRANSPORT_FIELDS))
    domain_hits = _domain_const_hits(schema)
    entry["forbidden_token_hits"] = hits
    entry["transport_property_hits"] = transport_hits
    entry["domain_pin_hits"] = domain_hits
    entry["status"] = (
        "PASS" if entry["check_schema"] == "PASS" and not hits and not transport_hits and not domain_hits else "FAIL"
    )
    return entry


def validate_schemas(schemas_root: Path = PAYLOADS) -> list[dict[str, Any]]:
    return [_schema_entry(path) for path in sorted(schemas_root.glob("*.schema.yaml"))]


def _positive_entry(path: Path, models: dict[str, type], aliases: dict[str, str], contract: Any) -> dict[str, Any]:
    stem = path.name.replace(".json", "")
    schema_name = aliases.get(stem, f"{stem}.schema.yaml")
    model_name = SCHEMA_TO_MODEL.get(schema_name)
    entry: dict[str, Any] = {"file": _rel(path), "schema": schema_name}
    if model_name is None:
        entry["status"] = "FAIL"
        entry["error"] = "no model mapping"
        return entry
    try:
        models[model_name].model_validate(_load_json(path), context=contract.context())
        entry["status"] = "PASS"
        entry["model"] = model_name
    except ValidationError as exc:
        entry["status"] = "FAIL"
        entry["error"] = str(exc)[:400]
    return entry


def _model_for_negative(path: Path, models: dict[str, type]) -> tuple[type | None, str | None]:
    for schema_name, model_name in SCHEMA_TO_MODEL.items():
        prefix = schema_name.replace(".schema.yaml", "")
        if path.name.startswith(prefix):
            return models[model_name], model_name
    return None, None


def _negative_entry(path: Path, models: dict[str, type], contract: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"file": _rel(path)}
    model, model_name = _model_for_negative(path, models)
    if model is None:
        entry["status"] = "FAIL"
        entry["error"] = "no model mapping"
        return entry
    entry["model"] = model_name
    try:
        model.model_validate(_load_json(path), context=contract.context())
        entry["status"] = "FAIL"
        entry["detail"] = "incorrectly_accepted"
    except ValidationError:
        entry["status"] = "PASS"
        entry["detail"] = "rejected_as_expected"
        if path.name in SEMANTIC_NEGATIVES:
            entry["enforcement"] = "owner_semantic"
    return entry


def fixture_domain(fixtures_root: Path) -> str:
    """The single domain every positive fixture names; a hard failure otherwise."""
    examples = sorted((fixtures_root / "examples").glob("*.json"))
    if not examples:
        raise ValueError(f"no positive fixtures under {fixtures_root / 'examples'}")
    domains = {str(_load_json(path).get("domain")) for path in examples}
    if len(domains) != 1:
        raise ValueError(f"positive fixtures name more than one domain: {sorted(domains)}")
    return domains.pop()


def validate_domain_authority(domains_root: Path, domain_id: str) -> tuple[dict[str, Any], Any]:
    """DomainPackLoader must admit the fixtures' domain; no parallel cartridge authority.

    Returns the report entry and the admission contract (``None`` on failure).
    """
    from engine.config.loader import DomainNotFoundError, DomainPackLoader, DomainSpecError
    from engine.models.payloads import DomainContract

    entry: dict[str, Any] = {
        "loader": "DomainPackLoader",
        "domain_id": domain_id,
        "config_path": _rel(domains_root),
        "spec_path": _rel(domains_root / domain_id / "spec.yaml"),
        "parallel_cartridge_forbidden": True,
    }
    try:
        spec = DomainPackLoader(config_path=str(domains_root)).load_domain(domain_id)
    except (DomainNotFoundError, DomainSpecError) as exc:
        entry.update({"loaded": False, "match_directions": [], "status": "FAIL", "error": str(exc)[:400]})
        return entry, None
    contract = DomainContract.from_spec(spec)
    entry.update(
        {
            "loaded": True,
            "domain_version": contract.domain_version,
            "match_directions": sorted(contract.match_directions),
            "entity_types": sorted(contract.entity_types),
            "source_systems": sorted(contract.source_systems),
            "outcome_types": sorted(contract.outcome_types),
            "status": "PASS" if contract.match_directions else "FAIL",
        }
    )
    return entry, contract


def validate_fixtures(fixtures_root: Path, contract: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    models = _models()
    aliases = {"sync-projection-tombstone": "sync-projection.schema.yaml"}
    examples = fixtures_root / "examples"
    negatives_dir = fixtures_root / "negative_examples"
    positives = [_positive_entry(path, models, aliases, contract) for path in sorted(examples.glob("*.json"))]
    negatives = [_negative_entry(path, models, contract) for path in sorted(negatives_dir.glob("*.json"))]
    return positives, negatives


def _write_report(write_path: Path, report: dict[str, Any]) -> None:
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def compile_report(
    *,
    write_path: Path | None,
    domains_root: Path = DEFAULT_DOMAINS_ROOT,
    fixtures_root: Path = PAYLOADS,
    domain_id: str | None = None,
) -> dict[str, Any]:
    schema_results = validate_schemas()
    resolved_domain = domain_id or fixture_domain(fixtures_root)
    domain, contract = validate_domain_authority(domains_root, resolved_domain)
    if contract is not None:
        positives, negatives = validate_fixtures(fixtures_root, contract)
    else:
        positives, negatives = [], []
    report: dict[str, Any] = {
        "schema": "l9.ceg.payload_contract_compiler.v1",
        "task_id": "TASK-034",
        "authority": "DomainPackLoader + contracts/payloads (no parallel cartridge)",
        "domains_root": _rel(domains_root),
        "fixtures_root": _rel(fixtures_root),
        "schemas": schema_results,
        "positives": positives,
        "negatives": negatives,
        "domain_authority": domain,
    }
    ok = (
        bool(schema_results)
        and bool(positives)
        and bool(negatives)
        and all(r["status"] == "PASS" for r in schema_results)
        and all(r["status"] == "PASS" for r in positives)
        and all(r["status"] == "PASS" for r in negatives)
        and domain["status"] == "PASS"
    )
    report["result"] = "PASS" if ok else "FAIL"
    report["digest"] = _sha_obj({k: v for k, v in report.items() if k != "digest"})
    if write_path is not None:
        _write_report(write_path, report)
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
    parser.add_argument(
        "--domains-root",
        type=Path,
        default=DEFAULT_DOMAINS_ROOT,
        help="Domain packs root DomainPackLoader reads (default: domains/)",
    )
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=PAYLOADS,
        help="Directory holding examples/ and negative_examples/ for the domain under test (default: contracts/payloads)",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Domain id to admit fixtures against (default: the domain every positive fixture names)",
    )
    args = parser.parse_args(argv)
    write_path = None if args.stdout_only else _resolve_under_root(args.report)
    report = compile_report(
        write_path=write_path,
        domains_root=args.domains_root,
        fixtures_root=args.fixtures_root,
        domain_id=args.domain,
    )
    print(json.dumps({"result": report["result"], "digest": report["digest"]}, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
