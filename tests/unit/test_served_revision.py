"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-domain, supply-chain, admin]
owner: engine-team
status: active
--- /L9_META ---

Issue #400 F4: the served revision (digest, version, stage) per tenant.

Disposable tmp_path packs only; no design pack is read as a production source.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config import loader as owner
from engine.handlers import handle_admin, init_dependencies
from engine.state import get_state
from tests.unit.test_domain_pack_loader import _minimal_spec_yaml

pytestmark = pytest.mark.unit

TENANT = "acme"
FIELDS = {"tenant_id", "sha256", "version", "stage", "source"}


def _spec_bytes(version: str, tenant: str = TENANT) -> bytes:
    return _minimal_spec_yaml(tenant).replace("version: 0.0.1", f"version: {version}").encode()


def _plain_root(tmp_path: Path, version: str, tenant: str = TENANT) -> tuple[Path, bytes]:
    root = tmp_path / "plain"
    data = _spec_bytes(version, tenant)
    (root / tenant).mkdir(parents=True)
    (root / tenant / "spec.yaml").write_bytes(data)
    return root, data


def _receipt_root(tmp_path: Path, version: str = "1.0.0-reviewed") -> tuple[Path, str]:
    data = _spec_bytes(version)
    digest = hashlib.sha256(data).hexdigest()
    pointer = owner.DomainCurrent(schema="ceg.domain-current/v1", tenant_id=TENANT, version=version, sha256=digest)
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
    revision = root / TENANT / "revisions" / digest
    revision.mkdir(parents=True)
    (revision / "spec.yaml").write_bytes(data)
    (revision / "receipt.json").write_text(json.dumps(receipt.model_dump()))
    (root / TENANT / "current.json").write_text(json.dumps(pointer.model_dump()))
    return root, digest


@pytest.fixture
def receipt_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    monkeypatch.setattr(owner.settings, "l9_env", "dev")


@pytest.fixture
def plain_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", False)
    monkeypatch.setattr(owner.settings, "l9_env", "dev")


@pytest.mark.parametrize("version", ["0.0.1", "1.0.0", "1.2.0-proposed", "2.0.0-production"])
def test_version_stage(version: str) -> None:
    expected = version.split("-", 1)[1] if "-" in version else None
    assert owner.version_stage(version) == expected


@pytest.mark.usefixtures("plain_mode")
@pytest.mark.parametrize(("version", "stage"), [("0.0.1", None), ("1.2.0-proposed", "proposed")])
def test_plain_layout_reports_digest_of_admitted_bytes(tmp_path: Path, version: str, stage: str | None) -> None:
    root, data = _plain_root(tmp_path, version)
    spec, revision = owner.DomainPackLoader(str(root)).load_domain_revision(TENANT)
    assert spec.domain.version == version
    assert revision == {
        "tenant_id": TENANT,
        "sha256": hashlib.sha256(data).hexdigest(),
        "version": version,
        "stage": stage,
        "source": "plain",
    }


@pytest.mark.usefixtures("plain_mode")
def test_plain_revision_follows_the_cached_spec(tmp_path: Path) -> None:
    root, _data = _plain_root(tmp_path, "1.0.0")
    loader = owner.DomainPackLoader(str(root))
    first_spec, first = loader.load_domain_revision(TENANT)
    # A cache hit returns the spec and the revision it was admitted from.
    assert loader.load_domain_revision(TENANT) == (first_spec, first)
    assert loader.load_domain(TENANT) is first_spec

    changed = _spec_bytes("1.1.0-reviewed")
    path = root / TENANT / "spec.yaml"
    path.write_bytes(changed)
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))
    loader._ttl_seconds = 0.0
    spec, revision = loader.load_domain_revision(TENANT)
    assert spec.domain.version == "1.1.0-reviewed"
    assert revision["sha256"] == hashlib.sha256(changed).hexdigest()
    assert revision["stage"] == "reviewed"

    loader.invalidate(TENANT)
    assert TENANT not in loader._revisions
    loader.load_domain(TENANT)
    loader.invalidate()
    assert loader._revisions == {}


@pytest.mark.usefixtures("plain_mode")
def test_injected_cache_entry_without_revision_is_reloaded(tmp_path: Path) -> None:
    """A cached spec with no recorded revision is never paired with a guessed one."""
    root, data = _plain_root(tmp_path, "1.0.0")
    loader = owner.DomainPackLoader(str(root))
    stale = MagicMock()
    loader._cache[TENANT] = (stale, 0.0, 0.0)
    loader._ttl_seconds = 3600.0
    spec, revision = loader.load_domain_revision(TENANT)
    assert spec is not stale
    assert revision["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.usefixtures("receipt_mode")
def test_receipt_layout_reports_the_verified_revision_digest(tmp_path: Path) -> None:
    root, digest = _receipt_root(tmp_path)
    loader = owner.DomainPackLoader(str(root))
    _spec, revision = loader.load_domain_revision(TENANT)
    assert revision == {
        "tenant_id": TENANT,
        "sha256": digest,
        "version": "1.0.0-reviewed",
        "stage": "reviewed",
        "source": "receipt",
    }
    assert loader.list_domain_revisions() == [revision]
    assert loader.list_domains() == [TENANT]


@pytest.mark.usefixtures("receipt_mode")
def test_tampered_revision_is_still_refused(tmp_path: Path) -> None:
    root, digest = _receipt_root(tmp_path)
    spec_path = root / TENANT / "revisions" / digest / "spec.yaml"
    spec_path.write_bytes(spec_path.read_bytes().replace(b"name: acme", b"name: evil"))
    loader = owner.DomainPackLoader(str(root))
    with pytest.raises(owner.DomainSpecError, match="tamper"):
        loader.load_domain_revision(TENANT)
    with pytest.raises(owner.DomainSpecError, match="tamper"):
        loader.load_domain(TENANT)
    assert loader.list_domain_revisions() == []
    assert loader.list_domains() == []


@pytest.mark.usefixtures("plain_mode")
def test_list_domain_revisions_omits_unadmitted_packs(tmp_path: Path) -> None:
    root, _data = _plain_root(tmp_path, "1.0.0")
    (root / "broken").mkdir()
    (root / "broken" / "spec.yaml").write_text("domain: {}\n")
    loader = owner.DomainPackLoader(str(root))
    assert [r["tenant_id"] for r in loader.list_domain_revisions()] == loader.list_domains() == [TENANT]


@pytest.fixture
def wired_admin(tmp_path: Path) -> owner.DomainPackLoader:
    root, _digest = _receipt_root(tmp_path)
    loader = owner.DomainPackLoader(str(root))
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    init_dependencies(driver, loader)
    get_state()._tenant_allowlist = {TENANT}
    return loader


@pytest.mark.usefixtures("receipt_mode")
async def test_admin_get_domain_reports_served_revision(wired_admin: owner.DomainPackLoader) -> None:
    _spec, expected = wired_admin.load_domain_revision(TENANT)
    result = await handle_admin(TENANT, {"subaction": "get_domain", "domain_id": TENANT})
    assert result["domain"]["domain"]["version"] == "1.0.0-reviewed"
    assert result["served_revision"] == expected
    assert set(result["served_revision"]) == FIELDS
    assert "/" not in json.dumps(result["served_revision"])  # no filesystem path


@pytest.mark.usefixtures("receipt_mode")
async def test_admin_list_domains_reports_served_revisions(wired_admin: owner.DomainPackLoader) -> None:
    _spec, expected = wired_admin.load_domain_revision(TENANT)
    result = await handle_admin(TENANT, {"subaction": "list_domains"})
    assert result["domains"] == [TENANT]
    assert result["served_revisions"] == {TENANT: expected}
    assert result["status"] == "success"
