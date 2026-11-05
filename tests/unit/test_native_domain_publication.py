"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-domain, publication]
owner: engine-team
status: active
--- /L9_META ---

Offline native publication lifecycle contracts; no live promotion."""

# ruff: noqa: S603 — current interpreter and repository-owned CLIs only.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine.config import loader as owner
from tests.unit.test_domain_pack_loader import _minimal_spec_yaml
from tools.validate_domain import validate_spec

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def draft(version: str = "1.0.0-reviewed") -> bytes:
    return _minimal_spec_yaml("acme").replace("0.0.1", version).encode()


def source(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "acme" / "spec.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("version", ["1.0.0-seed", "1.0.0-proposed", "1.0.0"])
def test_publisher_refuses_unreviewed_stage(tmp_path: Path, version: str) -> None:
    from tools.publish_domain_pack import publish_pack

    path = source(tmp_path / "input", draft(version))
    with pytest.raises((owner.DomainSpecError, owner.DomainNotFoundError), match="stage"):
        publish_pack(
            path,
            tmp_path / "store",
            tenant_id="acme",
            reviewed_by="Test Reviewer",
            approval_ref="test-only:approval",
            reviewed_input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    assert not (tmp_path / "store" / "acme" / "current.json").exists()


def publish(tmp_path: Path, data: bytes | None = None, **overrides: str) -> dict:
    from tools.publish_domain_pack import publish_pack

    path = source(tmp_path / "input", data or draft())
    args = {
        "tenant_id": "acme",
        "reviewed_by": "Test Reviewer",
        "approval_ref": "test-only:approval",
        "reviewed_input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    args.update(overrides)
    return publish_pack(path, tmp_path / "store", **args)


def test_explicit_empty_reviewed_at_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reviewed_at"):
        publish(tmp_path, reviewed_at="")
    assert not (tmp_path / "store" / "acme" / "current.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("reviewed_by", ""), ("approval_ref", " "), ("reviewed_input_sha256", "0" * 64), ("tenant_id", "other")],
)
def test_publication_requires_exact_operator_evidence(tmp_path: Path, field: str, value: str) -> None:
    with pytest.raises((owner.DomainSpecError, owner.DomainNotFoundError, ValueError)):
        publish(tmp_path, **{field: value})
    assert not (tmp_path / "store" / "acme" / "current.json").exists()


def test_publication_immutable_bytes_receipt_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.publish_domain_pack import rollback_pack

    first = publish(tmp_path)
    sha = first["sha256"]
    revision = tmp_path / "store" / "acme" / "revisions" / sha
    assert (revision / "spec.yaml").read_bytes() == draft()
    receipt = json.loads((revision / "receipt.json").read_text())
    assert receipt["schema"] == "ceg.domain-approval/v1"
    assert receipt["reviewed_input_sha256"] == sha
    assert receipt["previous_sha256"] is None
    old_receipt = (revision / "receipt.json").read_bytes()
    second = publish(tmp_path, draft("1.1.0-reviewed"))
    assert second["sha256"] != sha
    assert (
        json.loads((tmp_path / "store" / "acme" / "revisions" / second["sha256"] / "receipt.json").read_text())[
            "previous_sha256"
        ]
        == sha
    )
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    loader = owner.DomainPackLoader(str(tmp_path / "store"))
    assert loader.load_domain("acme").domain.version == "1.1.0-reviewed"
    assert loader.list_domains() == ["acme"]
    rollback_pack(tmp_path / "store", tenant_id="acme", sha256=sha)
    assert loader.load_domain("acme").domain.version == "1.0.0-reviewed"
    assert (revision / "receipt.json").read_bytes() == old_receipt


def test_idempotence_does_not_rewrite_review_history(tmp_path: Path) -> None:
    pointer = publish(tmp_path)
    path = tmp_path / "store" / "acme" / "revisions" / pointer["sha256"] / "receipt.json"
    original = path.read_bytes()
    assert publish(tmp_path) == pointer
    assert path.read_bytes() == original
    with pytest.raises(owner.DomainSpecError, match="different review evidence"):
        publish(tmp_path, reviewed_by="Different Test Reviewer")
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", ["1.0.0-reviewed", "0.9.0-production"])
def test_stage_and_version_downgrade_requires_explicit_rollback(tmp_path: Path, version: str) -> None:
    first = publish(tmp_path, draft("1.0.0-production"))
    with pytest.raises(owner.DomainSpecError, match="downgrade"):
        publish(tmp_path, draft(version))
    assert json.loads((tmp_path / "store" / "acme" / "current.json").read_text())["sha256"] == first["sha256"]


def test_same_version_changed_bytes_refused(tmp_path: Path) -> None:
    publish(tmp_path)
    with pytest.raises(owner.DomainSpecError, match="new version"):
        publish(tmp_path, draft() + b"# reviewed edit without version bump\n")


def test_tampered_prior_revision_cannot_be_rolled_back(tmp_path: Path) -> None:
    from tools.publish_domain_pack import rollback_pack

    first = publish(tmp_path)
    second = publish(tmp_path, draft("1.1.0-reviewed"))
    path = tmp_path / "store" / "acme" / "revisions" / first["sha256"] / "spec.yaml"
    path.chmod(0o644)
    path.write_bytes(draft() + b"# corruption\n")
    with pytest.raises(owner.DomainSpecError, match="tamper"):
        rollback_pack(tmp_path / "store", tenant_id="acme", sha256=first["sha256"])
    assert json.loads((tmp_path / "store" / "acme" / "current.json").read_text())["sha256"] == second["sha256"]


@pytest.mark.parametrize("tenant", ["plasticos", "freight-matching"])
def test_native_fixture_complete_admission_and_disposable_review_candidate(
    tmp_path: Path, tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.publish_domain_pack import publish_pack

    path = REPO / "domains" / tenant / "spec.yaml"
    data = path.read_bytes()
    spec = owner.validate_native_bytes(data, tenant, complete=True)
    assert owner.admission_defects(spec, complete=True) == []
    assert validate_spec(path, complete=True)[1] == 0
    with pytest.raises(owner.DomainSpecError, match="stage"):
        owner.validate_native_bytes(data, tenant, complete=True, require_reviewed=True)
    # Test-only explicit stage edit in a temporary file, never an actual approval.
    candidate_data = data.replace(
        f"version: {spec.domain.version}".encode(), f"version: {spec.domain.version}-reviewed".encode(), 1
    )
    candidate = tmp_path / f"{tenant}-test-candidate.yaml"
    candidate.write_bytes(candidate_data)
    staged = owner.validate_native_bytes(candidate_data, tenant, complete=True, require_reviewed=True)
    original_meaning = spec.model_dump(exclude={"domain"})
    assert staged.model_dump(exclude={"domain"}) == original_meaning
    pointer = publish_pack(
        candidate,
        tmp_path / "store",
        tenant_id=tenant,
        reviewed_by="TEST ONLY Reviewer",
        approval_ref="test-fixture:no-real-approval",
        reviewed_input_sha256=hashlib.sha256(candidate_data).hexdigest(),
    )
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    admitted = owner.DomainPackLoader(str(tmp_path / "store")).load_domain(tenant)
    assert admitted.model_dump(exclude={"domain"}) == original_meaning
    assert pointer["sha256"] != hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


def test_operator_cli_requires_explicit_review_and_never_tracebacks(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = source(tmp_path / "input", draft())
    command = [
        sys.executable,
        str(REPO / "tools" / "publish_domain_pack.py"),
        "publish",
        str(path),
        "--root",
        str(tmp_path / "store"),
        "--tenant-id",
        "acme",
    ]
    absent = subprocess.run(command, capture_output=True, text=True, check=False)
    assert absent.returncode == 2
    assert "--reviewed-by" in absent.stderr
    wrong_digest = subprocess.run(
        [
            *command,
            "--reviewed-by",
            "CLI TEST Reviewer",
            "--approval-ref",
            "test-only:cli",
            "--reviewed-input-sha256",
            "0" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong_digest.returncode == 1
    assert json.loads(wrong_digest.stderr)["status"] == "refused"
    assert "Traceback" not in wrong_digest.stderr
    approved = subprocess.run(
        [
            *command,
            "--reviewed-by",
            "CLI TEST Reviewer",
            "--approval-ref",
            "test-only:cli",
            "--reviewed-input-sha256",
            hashlib.sha256(draft()).hexdigest(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert approved.returncode == 0
    pointer = json.loads(approved.stdout)
    spec_path = tmp_path / "store" / "acme" / "revisions" / pointer["sha256"] / "spec.yaml"
    validated = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "validate_domain.py"),
            str(spec_path),
            "--root",
            str(tmp_path / "store"),
            "--tenant-id",
            "acme",
            "--complete",
            "--require-reviewed",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validated.returncode == 0
    assert json.loads(validated.stdout)["results"][0]["status"] == "PASS"
