"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-authoring, predecessor-parity]
owner: engine-team
status: active
--- /L9_META ---

F-001 authoring CLI compatibility; native runtime admission remains distinct."""

# ruff: noqa: S603 — current interpreter and repository-owned CLIs only.

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config import loader as owner
from tests.unit.test_domain_pack_loader import _minimal_spec_yaml
from tools.validate_domain import validate_spec

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

PINNED_REVISION = "f8d933d83a11244a2ada79c7f0d0dea74fbbe6ce"

PINNED_SOURCE_SHA256 = "3587b3f3dd9997bfe56bab323d0d7332b222796f39ac9842d4483ad76bde550f"

PINNED_GOLDEN = REPO / "tests/fixtures/native_authoring_predecessor_f8d933.json"

AUTHORING_ENV = {
    "L9_ENV": "dev",
    "UNVALIDATED_DOMAIN_PACKS_ENABLED": "true",
    "DORMANT_DOMAIN_PACKS_ENABLED": "true",
    "DOMAIN_STRICT_VALIDATION": "true",
    "SCORING_ADMISSION_CHECK_ENABLED": "false",
    "ADMISSION_CHECKS_FULL_MATCH": "false",
}


def _source(tmp_path: Path, case: str = "good") -> Path:
    raw = yaml.safe_load(_minimal_spec_yaml("acme"))
    raw["ontology"]["edges"] = [
        {
            "type": "ASKS",
            "from": "Ask",
            "to": "Thing",
            "direction": "DIRECTED",
            "category": "capability",
            "managedby": "sync",
        }
    ]
    raw["traversal"]["steps"] = [
        {"name": "ask", "pattern": "(query:Ask)-[:ASKS]->(candidate:Thing)", "matchdirections": ["d1"]}
    ]
    if case == "traversal":
        raw["traversal"]["steps"][0]["pattern"] = "(query:Ask)-[:MISSING]->(candidate:Thing)"
    elif case == "cross-reference":
        raw["gates"] = [{"name": "edge", "type": "traversal", "edgetype": "MISSING"}]
    elif case == "edge-source":
        raw["ontology"]["edges"][0]["from"] = "Missing"
    elif case == "edge-target":
        raw["ontology"]["edges"][0]["to"] = "Missing"
    elif case == "candidate":
        raw["matchentities"]["candidate"][0]["label"] = "Missing"
    elif case == "gate":
        raw["gates"] = [{"name": "broken", "type": "composite", "subgates": ["missing"]}]
    elif case == "compliance":
        raw["gates"] = [{"name": "blocked", "type": "boolean", "candidateprop": "thing_id"}]
        raw["compliance"] = {"prohibitedfactors": {"enabled": True, "blockedfields": ["thing_id"]}}
    path = tmp_path / "acme" / "spec.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _cli(path: Path, *flags: str, script: Path | None = None) -> tuple[int, dict[str, Any]]:
    env = os.environ.copy()
    # Pin ordinary local diagnostic defaults; only runtime bypasses are enabled.
    # The predecessor authoring checks must not be bypassed by these switches.
    env.update(PYTHONPATH=str(REPO), **AUTHORING_ENV)
    root_flags = (
        [] if script is not None else ["--root", str(path.parent.parent if path.name == "spec.yaml" else path.parent)]
    )
    result = subprocess.run(
        [sys.executable, str(script or REPO / "tools/validate_domain.py"), str(path), "--json", *root_flags, *flags],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert "Traceback" not in result.stderr
    body = json.loads(result.stdout)
    assert body["exit_code"] == result.returncode
    return result.returncode, body


@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_actual_authoring_cli_runs_all_mandatory_checks(tmp_path: Path, flags: tuple[str, ...]) -> None:
    code, body = _cli(_source(tmp_path), *flags)
    assert code == 0
    assert {item["check"] for item in body["results"]} >= {
        "W1-01-cross-ref",
        "W1-03-gate-compile",
        "W1-04-traversal",
        "W3-02-prohibited-factors",
    }


@pytest.mark.parametrize(("flags", "expected"), [((), 2), (("--strict",), 1)])
def test_actual_authoring_cli_warning_exit_and_strict_escalation(
    tmp_path: Path, flags: tuple[str, ...], expected: int
) -> None:
    code, body = _cli(_source(tmp_path, "traversal"), *flags)
    assert code == expected
    assert any(item["check"] == "W1-04-traversal" and item["status"] == "WARN" for item in body["results"])


@pytest.mark.parametrize("case", ["cross-reference", "edge-source", "edge-target", "candidate", "gate", "compliance"])
@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_actual_authoring_cli_failures_cannot_use_runtime_bypasses(
    tmp_path: Path, case: str, flags: tuple[str, ...]
) -> None:
    code, body = _cli(_source(tmp_path, case), *flags)
    assert code == 1
    assert any(item["status"] == "FAIL" for item in body["results"])
    named_checks = {
        "cross-reference": "W1-01-gate-edge",
        "gate": "W1-03-gate-compile",
        "compliance": "W3-02-prohibited-factors",
    }
    if case in named_checks:
        assert any(item["check"] == named_checks[case] and item["status"] == "FAIL" for item in body["results"])


@pytest.mark.parametrize(("tenant", "expected"), [("plasticos", 1), ("freight-matching", 0)])
@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_actual_fixture_cli_matches_pinned_default_and_strict_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenant: str, expected: int, flags: tuple[str, ...]
) -> None:
    # Hosted shallow checkouts need no Git executable or historical object.
    monkeypatch.setenv("PATH", str(tmp_path / "no-git"))
    golden = json.loads(PINNED_GOLDEN.read_text(encoding="utf-8"))
    assert golden["schema"] == "ceg.native-authoring-predecessor/v1"
    assert golden["environment"] == AUTHORING_ENV
    pinned = golden["source"]
    assert pinned["revision"] == PINNED_REVISION
    assert pinned["path"] == "tools/validate_domain.py"
    assert pinned["git_blob_sha1"] == "52a7f503431dbebe8dfa32ad46552b0ec628b6d7"
    pinned_data = pinned["content"].encode("utf-8")
    assert hashlib.sha256(pinned_data).hexdigest() == pinned["sha256"] == PINNED_SOURCE_SHA256
    script = tmp_path / "pinned-validate.py"
    script.write_bytes(pinned_data)
    fixture = golden["fixtures"][tenant]
    assert fixture["path"] == f"domains/{tenant}/spec.yaml"
    verdict = fixture["verdicts"]["strict" if flags else "default"]
    assert verdict["flags"] == list(flags)
    path = REPO / "domains" / tenant / "spec.yaml"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == fixture["sha256"]
    pinned_code, pinned_body = _cli(path, *flags, script=script)
    current_code, current_body = _cli(path, *flags)
    assert pinned_code == current_code == expected == verdict["exit_code"]
    assert [(r["check"], r["status"]) for r in current_body["results"]] == [
        (r["check"], r["status"]) for r in pinned_body["results"]
    ]
    assert [[r["check"], r["status"]] for r in pinned_body["results"]] == verdict["checks"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_runtime_per_direction_api_remains_unchanged() -> None:
    path = REPO / "domains/plasticos/spec.yaml"
    assert owner.validate_native_file(path, "plasticos").domain.version == "1.3.0"
    assert validate_spec(path)[1] == 0
    assert validate_spec(path, complete=True)[1] == 0


def test_programmatic_strict_meaningfully_escalates_warning(tmp_path: Path) -> None:
    path = _source(tmp_path, "traversal")
    results, code = validate_spec(path, strict=True)
    assert code == 1
    assert any(result["status"] == "WARN" for result in results)


@pytest.mark.parametrize("case", ["good", "traversal", "gate", "compliance"])
def test_complete_cli_stays_fail_closed(tmp_path: Path, case: str) -> None:
    code, body = _cli(_source(tmp_path, case), "--complete")
    assert code == (0 if case == "good" else 1)
    assert body["results"][0]["status"] == ("PASS" if case == "good" else "FAIL")


def test_actual_complete_cli_binds_reviewed_receipt(tmp_path: Path) -> None:
    path = _source(tmp_path)
    data = path.read_bytes().replace(b"version: 0.0.1", b"version: 0.0.1-reviewed")
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    body = {
        "schema": "ceg.domain-approval/v1",
        "tenant_id": "acme",
        "version": "0.0.1-reviewed",
        "sha256": digest,
        "reviewed_input_sha256": digest,
        "reviewed_by": "TEST ONLY Reviewer",
        "approval_ref": "test-fixture:no-real-approval",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "previous_sha256": None,
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(body), encoding="utf-8")
    flags = ("--complete", "--require-reviewed", "--receipt", str(receipt))
    assert _cli(path, *flags)[0] == 0
    body["reviewed_input_sha256"] = "0" * 64
    receipt.write_text(json.dumps(body), encoding="utf-8")
    assert _cli(path, *flags)[0] == 1
    receipt.unlink()
    assert _cli(path, *flags)[0] == 1


def test_cross_reference_policy_independent_of_runtime_schema_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner.settings, "domain_strict_validation", False)
    monkeypatch.setattr(owner.settings, "unvalidated_domain_packs_enabled", True)
    path = _source(tmp_path, "edge-source")
    results, code = validate_spec(path, strict=True)
    assert code == 1
    assert any(result["check"] == "W1-01-edge-source" and result["status"] == "FAIL" for result in results)


def test_authoring_cli_text_warning_is_not_reported_clean(tmp_path: Path) -> None:
    path = _source(tmp_path, "traversal")
    result = subprocess.run(
        [sys.executable, str(REPO / "tools/validate_domain.py"), str(path), "--root", str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Result: WARNINGS (non-blocking)" in result.stdout
    assert "ALL CHECKS PASSED" not in result.stdout


@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_authoring_cli_without_traversal_remains_clean(tmp_path: Path, flags: tuple[str, ...]) -> None:
    path = _source(tmp_path)
    raw = yaml.safe_load(path.read_bytes())
    raw["traversal"] = None
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    code, body = _cli(path, *flags)
    assert code == 0
    assert any(
        item["check"] == "W1-04-traversal" and item["message"] == "No traversal defined" for item in body["results"]
    )


@pytest.mark.parametrize("data", [b"[bad", b"- item", b"\xff", b"!!python/object:os.system {}", b"domain: {}"])
def test_actual_authoring_cli_hostile_inputs_fail_closed(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "input.yaml"
    path.write_bytes(data)
    assert _cli(path)[0] == 1


def test_actual_authoring_cli_missing_input_fails_closed(tmp_path: Path) -> None:
    assert _cli(tmp_path / "missing.yaml")[0] == 1


def test_actual_complete_review_requirement_refuses_unreviewed_input(tmp_path: Path) -> None:
    assert _cli(_source(tmp_path), "--complete", "--require-reviewed")[0] == 1


@pytest.mark.parametrize("option", ["spec_path", "--receipt"])
@pytest.mark.parametrize("escape", ["absolute", "parent", "symlink"])
def test_native_cli_root_confines_spec_and_receipt_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], option: str, escape: str
) -> None:
    from tools import validate_domain

    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("untrusted input", encoding="utf-8")
    if escape == "absolute":
        escaped = str(outside)
    elif escape == "parent":
        escaped = "../outside.yaml"
    else:
        (root / "linked.yaml").symlink_to(outside)
        escaped = "linked.yaml"

    def forbidden_admission(*args: Any, **kwargs: Any) -> tuple[list[dict], int]:
        pytest.fail("root escape must be rejected before native admission")

    monkeypatch.setattr(validate_domain, "validate_spec", forbidden_admission)
    args = [escaped if option == "spec_path" else "spec.yaml", "--root", str(root)]
    if option == "--receipt":
        args.extend(["--receipt", escaped])
    with pytest.raises(SystemExit) as raised:
        validate_domain.main(args)
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert f"{option} must stay under --root" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_native_cli_relative_spec_and_receipt_resolve_from_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tools import validate_domain

    root = tmp_path / "allowed"
    root.mkdir()
    calls = []

    def admission(spec_path: Path, **kwargs: Any) -> tuple[list[dict], int]:
        calls.append((spec_path, kwargs))
        return [], 0

    monkeypatch.setattr(validate_domain, "validate_spec", admission)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        validate_domain.main(
            ["acme/spec.yaml", "--root", str(root), "--receipt", "receipt.json", "--complete", "--json"]
        )
    assert raised.value.code == 0
    assert calls == [
        (
            root / "acme/spec.yaml",
            {
                "strict": False,
                "tenant_id": None,
                "complete": True,
                "require_reviewed": False,
                "receipt_path": root / "receipt.json",
                "authoring": True,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["spec_path"] == str(root / "acme/spec.yaml")


def test_native_cli_default_root_rejects_external_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tools import validate_domain

    monkeypatch.chdir(REPO)
    with pytest.raises(SystemExit) as raised:
        validate_domain.main([str(tmp_path / "outside.yaml")])
    assert raised.value.code == 2
    assert "spec_path must stay under --root" in capsys.readouterr().err


def test_native_cli_requires_existing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import validate_domain

    with pytest.raises(SystemExit) as raised:
        validate_domain.main(["spec.yaml", "--root", str(tmp_path / "missing")])
    assert raised.value.code == 2
    assert "--root must be an existing directory" in capsys.readouterr().err
