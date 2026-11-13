"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-domain, supply-chain]
owner: engine-team
status: active
--- /L9_META ---

Native runtime admission and CLI parity contracts; disposable fixtures only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from engine.config import loader as owner
from engine.config.settings import Settings
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT
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


def receipt_layout(tmp_path: Path) -> tuple[Path, str]:
    """Real disposable runtime layout; unsigned test evidence, no publisher."""
    data = draft()
    digest = hashlib.sha256(data).hexdigest()
    pointer = owner.DomainCurrent(
        schema="ceg.domain-current/v1", tenant_id="acme", version="1.0.0-reviewed", sha256=digest
    )
    receipt = owner.DomainApproval(
        **{
            **pointer.root,
            "schema": "ceg.domain-approval/v1",
            "reviewed_input_sha256": digest,
            "reviewed_by": "TEST ONLY Reviewer",
            "approval_ref": "test-fixture:no-real-approval",
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "previous_sha256": None,
        }
    )
    root = tmp_path / "store"
    revision = root / "acme" / "revisions" / digest
    revision.mkdir(parents=True)
    (revision / "spec.yaml").write_bytes(data)
    (revision / "receipt.json").write_text(json.dumps(receipt.model_dump()))
    (root / "acme" / "current.json").write_text(json.dumps(pointer.model_dump()))
    return root, digest


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
    path = DESIGN_PACKS_ROOT / "plasticos" / "spec.yaml"
    assert owner.DomainPackLoader(str(DESIGN_PACKS_ROOT)).load_domain("plasticos").domain.version == "1.4.0"
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


@pytest.mark.parametrize("root_setting", ["config_path", "DOMAINS_ROOT"])
def test_legacy_loader_accepts_authorized_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_setting: str
) -> None:
    path = source(tmp_path / "actual" / "packs", draft())
    alias = tmp_path / "authorized"
    alias.symlink_to(tmp_path / "actual", target_is_directory=True)
    root = alias / "packs"
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", False)
    if root_setting == "config_path":
        loader = owner.DomainPackLoader(str(root))
    else:
        monkeypatch.setattr(owner.settings, "domains_root", root)
        loader = owner.DomainPackLoader()
    assert loader.load_domain("acme").domain.version == "1.0.0-reviewed"
    assert loader._resolve_spec_path("acme") == path.resolve()
    assert loader.list_domains() == ["acme"]
    assert path.read_bytes() == draft()


@pytest.mark.parametrize("root_setting", ["config_path", "DOMAINS_ROOT"])
def test_receipt_loader_accepts_real_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_setting: str) -> None:
    root, digest = receipt_layout(tmp_path)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    monkeypatch.setattr(owner.settings, "l9_env", "dev")
    if root_setting == "config_path":
        loader = owner.DomainPackLoader(str(root))
    else:
        monkeypatch.setattr(owner.settings, "domains_root", root)
        loader = owner.DomainPackLoader()
    assert loader.load_domain("acme").domain.version == "1.0.0-reviewed"
    assert loader.list_domains() == ["acme"]
    spec, receipt = owner.load_native_revision(root, "acme", digest)
    assert receipt.root["reviewed_input_sha256"] == digest
    assert spec.domain.version == "1.0.0-reviewed"
    assert (root / "acme" / "revisions" / digest / "spec.yaml").read_bytes() == draft()


@pytest.mark.parametrize("root_setting", ["config_path", "DOMAINS_ROOT"])
def test_receipt_loader_refuses_authorized_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_setting: str
) -> None:
    receipt_layout(tmp_path / "actual")
    alias = tmp_path / "authorized"
    alias.symlink_to(tmp_path / "actual", target_is_directory=True)
    root = alias / "store"
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    monkeypatch.setattr(owner.settings, "l9_env", "dev")
    if root_setting == "config_path":
        loader = owner.DomainPackLoader(str(root))
    else:
        monkeypatch.setattr(owner.settings, "domains_root", root)
        loader = owner.DomainPackLoader()
    with pytest.raises(owner.DomainSpecError, match="symlink"):
        loader.load_domain("acme")
    assert loader.list_domains() == []
    assert loader._cache == {}


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    path = source(tmp_path, draft() + b"gates: []\n")
    assert validate_spec(path)[1] == 1


def test_receipt_mode_requires_explicit_production_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    monkeypatch.setattr(owner.settings, "l9_env", "prod")
    monkeypatch.delenv("DOMAIN_SPECS_PATH", raising=False)
    with pytest.raises(owner.DomainSpecError, match="root"):
        owner.DomainPackLoader()
    with pytest.raises(owner.DomainSpecError, match="root"):
        owner.DomainPackLoader(str(tmp_path / "missing"))


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


def test_native_fixture_aliases_and_all_loader_cli_verdicts_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = owner.DomainPackLoader(str(DESIGN_PACKS_ROOT))
    for path in sorted(DESIGN_PACKS_ROOT.glob("*/spec.yaml")):
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
