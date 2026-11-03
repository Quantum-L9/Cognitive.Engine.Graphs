"""Native owner parity and operator publication contracts (no live promotion)."""

# ruff: noqa: S603 — subprocess tests execute only sys.executable and repository-owned CLIs.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from engine.config import loader as owner
from engine.config.settings import Settings
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


@pytest.mark.parametrize("data", [b"[bad", b"- item", b"\xff", b"!!python/object:os.system {}", b"domain: {}"])
def test_hostile_yaml_cli_fails_without_traceback(tmp_path: Path, data: bytes) -> None:
    path = source(tmp_path, data)
    results, code = validate_spec(path)
    assert code == 1
    assert results[0]["status"] == "FAIL"
    with pytest.raises((owner.DomainSpecError, owner.DomainNotFoundError)):
        owner.DomainPackLoader(str(tmp_path)).load_domain("acme")


def test_cli_loader_same_withholding_reason(tmp_path: Path) -> None:
    path = source(tmp_path, draft().replace(b"name: acme", b"name: acme\n  activation: dormant"))
    with pytest.raises(owner.DomainNotFoundError) as err:
        owner.DomainPackLoader(str(tmp_path)).load_domain("acme")
    results, code = validate_spec(path)
    assert code == 1
    assert results[0]["message"] == str(err.value)


def test_cli_uses_per_direction_weight_semantics(tmp_path: Path) -> None:
    # PlasticOS is a multi-direction fixture; summing all directions is incorrect.
    path = REPO / "domains" / "plasticos" / "spec.yaml"
    assert owner.DomainPackLoader(str(REPO / "domains")).load_domain("plasticos").domain.version == "1.3.0"
    assert validate_spec(path)[1] == 0


@pytest.mark.parametrize("full", [False, True])
def test_traversal_gate_parity_respects_existing_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full: bool
) -> None:
    data = yaml.safe_load(draft())
    data["traversal"]["steps"] = [{"pattern": "(candidate)-[:MISSING]->(x:Thing)", "matchdirections": ["d1"]}]
    path = source(tmp_path, yaml.safe_dump(data).encode())
    monkeypatch.setattr(owner.settings, "admission_checks_full_match", full)
    try:
        owner.DomainPackLoader(str(tmp_path)).load_domain("acme")
    except (owner.DomainSpecError, owner.DomainNotFoundError) as err:
        reason = str(err)
        results, code = validate_spec(path)
        assert code == 1
        assert results[0]["message"] == reason
    else:
        assert validate_spec(path)[1] == 0


def test_approval_defaults_are_off() -> None:
    assert Settings(_env_file=None).domain_pack_receipts_enabled is False
    assert Settings(_env_file=None).domain_production_stage_required is False


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    path = source(tmp_path, draft() + b"gates: []\n")
    assert validate_spec(path)[1] == 1


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


@pytest.mark.parametrize(
    "target",
    [
        "pointer-tenant",
        "pointer-sha",
        "receipt-tenant",
        "receipt-sha",
        "receipt-version",
        "receipt-review",
        "bytes",
        "symlink",
    ],
)
def test_integrity_tamper_no_cache_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    published = publish(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    loader = owner.DomainPackLoader(str(tmp_path / "store"))
    loader.load_domain("acme")
    folder = tmp_path / "store" / "acme"
    revision = folder / "revisions" / published["sha256"]
    path = folder / "current.json" if target.startswith("pointer") else revision / "receipt.json"
    revision.chmod(0o755)
    path.chmod(0o644)
    if target == "bytes":
        (revision / "spec.yaml").chmod(0o644)
        (revision / "spec.yaml").write_bytes(draft() + b"# tamper\n")
    elif target == "symlink":
        path.unlink()
        path.symlink_to(source(tmp_path / "elsewhere", draft()))
    else:
        value = json.loads(path.read_text())
        key = {
            "pointer-tenant": "tenant_id",
            "pointer-sha": "sha256",
            "receipt-tenant": "tenant_id",
            "receipt-sha": "sha256",
            "receipt-version": "version",
            "receipt-review": "reviewed_by",
        }[target]
        value[key] = "" if target == "receipt-review" else "0" * 64 if key == "sha256" else "wrong"
        path.write_text(json.dumps(value))
    with pytest.raises((owner.DomainSpecError, owner.DomainNotFoundError)):
        loader.load_domain("acme")
    assert loader.list_domains() == []


def test_publisher_cannot_use_runtime_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "unvalidated_domain_packs_enabled", True)
    monkeypatch.setattr(owner.settings, "dormant_domain_packs_enabled", True)
    with pytest.raises(owner.DomainNotFoundError, match="dormant"):
        publish(tmp_path, draft().replace(b"name: acme", b"name: acme\n  activation: dormant"))


def test_atomic_pointer_failure_preserves_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.publish_domain_pack as publisher

    first = publish(tmp_path)
    old = (tmp_path / "store" / "acme" / "current.json").read_bytes()
    real_replace = publisher.os.replace

    def fail_pointer(src: Path, dst: Path) -> None:
        if Path(dst).name == "current.json":
            raise OSError("test-only simulated interruption")
        real_replace(src, dst)

    monkeypatch.setattr(publisher.os, "replace", fail_pointer)
    with pytest.raises(OSError, match="interruption"):
        publish(tmp_path, draft("1.1.0-reviewed"))
    assert (tmp_path / "store" / "acme" / "current.json").read_bytes() == old
    assert (tmp_path / "store" / "acme" / "revisions" / first["sha256"]).exists()


def test_receipt_mode_requires_explicit_production_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    monkeypatch.setattr(owner.settings, "l9_env", "prod")
    monkeypatch.delenv("DOMAIN_SPECS_PATH", raising=False)
    with pytest.raises(owner.DomainSpecError, match="root"):
        owner.DomainPackLoader()
    with pytest.raises(owner.DomainSpecError, match="root"):
        owner.DomainPackLoader(str(tmp_path / "missing"))


@pytest.mark.asyncio
async def test_async_receipt_mode_rechecks_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publication = publish(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    loader = owner.DomainPackLoader(str(tmp_path / "store"))
    await loader.load_domain_async("acme")
    spec_path = tmp_path / "store" / "acme" / "revisions" / publication["sha256"] / "spec.yaml"
    spec_path.chmod(0o644)
    spec_path.write_bytes(b"tampered")
    with pytest.raises(owner.DomainSpecError):
        await loader.load_domain_async("acme")


@pytest.mark.parametrize("case", ["gate", "traversal", "scoring", "sync", "unknown-key", "alias-cycle", "alias-bomb"])
def test_complete_publication_refuses_native_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    raw = yaml.safe_load(draft())
    if case == "gate":
        raw["gates"] = [{"name": "ignored", "type": "boolean", "candidateprop": "thing_id", "strictwhen": "unconsumed"}]
    elif case == "traversal":
        raw["traversal"]["steps"] = [
            {"name": "unknown", "pattern": "(candidate)-[:MISSING]->(x:Thing)", "matchdirections": ["d1"]}
        ]
    elif case == "scoring":
        raw["scoring"]["dimensions"] = [
            {
                "name": "attention",
                "source": "computed",
                "computation": "preference_attention",
                "weightkey": "wa",
                "defaultweight": 0.1,
            }
        ]
    elif case == "sync":
        raw["sync"] = {
            "endpoints": [
                {"path": "things", "targetnode": "Missing", "idproperty": "thing_id", "batchstrategy": "UNWINDMERGE"}
            ]
        }
    elif case == "unknown-key":
        raw["hidden_authority"] = "ignored by permissive schema, never by publisher"
    if case == "alias-cycle":
        data = b"domain: &loop {id: acme, child: *loop}\n"
    elif case == "alias-bomb":
        data = b"a: &a [x,x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]\ne: [*d,*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
    else:
        data = yaml.safe_dump(raw).encode()
    monkeypatch.setattr(owner.settings, "unvalidated_domain_packs_enabled", True)
    with pytest.raises((owner.DomainSpecError, owner.DomainNotFoundError)):
        publish(tmp_path, data)
    assert not (tmp_path / "store" / "acme" / "current.json").exists()


def test_complete_refuses_disabled_schema_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "domain_strict_validation", False)
    with pytest.raises(owner.DomainSpecError, match="validation guards"):
        publish(tmp_path)


@pytest.mark.parametrize("version", ["1.0.0", "1.0.0-seed", "1.0.0-proposed", "1.0.0-reviewed", "1.0.0-production"])
@pytest.mark.parametrize("stage_flag", [False, True])
def test_runtime_stage_flag_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, stage_flag: bool
) -> None:
    path = source(tmp_path, draft(version))
    monkeypatch.setattr(owner.settings, "l9_env", "prod")
    monkeypatch.setattr(owner.settings, "domain_production_stage_required", stage_flag)
    results, code = validate_spec(path)
    refused = stage_flag and version.rsplit("-", 1)[-1] not in {"reviewed", "production"}
    if refused:
        with pytest.raises(owner.DomainSpecError) as err:
            owner.DomainPackLoader(str(tmp_path)).load_domain("acme")
        assert code == 1
        assert results[0]["message"] == str(err.value)
    else:
        assert owner.DomainPackLoader(str(tmp_path)).load_domain("acme").domain.version == version
        assert code == 0


@pytest.mark.parametrize("component", ["root", "tenant", "revisions", "revision", "spec", "pointer"])
def test_every_native_path_component_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    receipt = publish(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    root = tmp_path / "store"
    tenant = root / "acme"
    revisions = tenant / "revisions"
    revision = revisions / receipt["sha256"]
    path = {
        "root": root,
        "tenant": tenant,
        "revisions": revisions,
        "revision": revision,
        "spec": revision / "spec.yaml",
        "pointer": tenant / "current.json",
    }[component]
    path.parent.chmod(0o755)
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(owner.DomainSpecError, match="symlink"):
        owner.DomainPackLoader(str(root)).load_domain("acme")


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


def test_native_fixture_aliases_and_all_loader_cli_verdicts_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = owner.DomainPackLoader(str(REPO / "domains"))
    for path in sorted((REPO / "domains").glob("*/spec.yaml")):
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        results, code = validate_spec(path)
        try:
            loader.load_domain(path.parent.name)
        except (owner.DomainNotFoundError, owner.DomainSpecError) as err:
            message = str(err)
            assert code == 1
            assert results[0]["message"] == message
        else:
            assert code == 0
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    monkeypatch.setattr(owner.settings, "dormant_domain_packs_enabled", True)
    assert loader.load_domain("idea-portfolio").domain.id == "idea-portfolio"


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


def test_atomic_concurrent_readers_observe_complete_revisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    publish(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    loader = owner.DomainPackLoader(str(tmp_path / "store"))

    def read_many() -> set[str]:
        return {loader.load_domain("acme").domain.version for _ in range(25)}

    with ThreadPoolExecutor(max_workers=3) as executor:
        reads = [executor.submit(read_many) for _ in range(2)]
        publish(tmp_path, draft("1.1.0-reviewed"))
        observed = set().union(*(result.result() for result in reads))
    assert observed <= {"1.0.0-reviewed", "1.1.0-reviewed"}
    assert loader.load_domain("acme").domain.version == "1.1.0-reviewed"


@pytest.mark.parametrize(
    "record", ["missing-review", "future-time", "naive-time", "duplicate-json", "missing-previous"]
)
def test_hostile_native_approval_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: str) -> None:
    pointer = publish(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    path = tmp_path / "store" / "acme" / "revisions" / pointer["sha256"] / "receipt.json"
    value = json.loads(path.read_text())
    if record == "missing-review":
        value.pop("approval_ref")
    elif record == "missing-previous":
        value.pop("previous_sha256")
    elif record == "future-time":
        value["reviewed_at"] = "2999-01-01T00:00:00+00:00"
    elif record == "naive-time":
        value["reviewed_at"] = "2026-01-01T00:00:00"
    path.chmod(0o644)
    text = json.dumps(value)
    if record == "duplicate-json":
        text = text[:-1] + ', "approval_ref": "another"}'
    path.write_text(text)
    with pytest.raises(owner.DomainSpecError):
        owner.DomainPackLoader(str(tmp_path / "store")).load_domain("acme")


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


def test_secure_file_read_bound_and_nonregular_file(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"x" * 32)
    with pytest.raises(owner.DomainSpecError, match="maximum size"):
        owner.read_native_file(oversized, limit=16)
    with pytest.raises(owner.DomainSpecError, match="regular file"):
        owner.read_native_file(tmp_path)


def test_receipt_layout_off_does_not_implicitly_change_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(tmp_path, draft())
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", False)
    loader = owner.DomainPackLoader(str(tmp_path))
    assert loader.load_domain("acme").domain.version == "1.0.0-reviewed"
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    with pytest.raises(owner.DomainSpecError):
        loader.load_domain("acme")
    assert loader.list_domains() == []


@pytest.mark.parametrize("component", ["input", "root", "tenant", "current", "revision"])
def test_publisher_refuses_symlink_inputs_and_store_tamper(tmp_path: Path, component: str) -> None:
    from tools.publish_domain_pack import publish_pack

    pointer = publish(tmp_path)
    root = tmp_path / "store"
    input_path = tmp_path / "input" / "acme" / "spec.yaml"
    tenant = root / "acme"
    target = {
        "input": input_path,
        "root": root,
        "tenant": tenant,
        "current": tenant / "current.json",
        "revision": tenant / "revisions" / pointer["sha256"],
    }[component]
    target.parent.chmod(0o755)
    moved = target.with_name(target.name + ".original")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(owner.DomainSpecError, match="symlink"):
        publish_pack(
            input_path,
            root,
            tenant_id="acme",
            reviewed_by="Test Reviewer",
            approval_ref="test-only:approval",
            reviewed_input_sha256=hashlib.sha256(draft()).hexdigest(),
        )
