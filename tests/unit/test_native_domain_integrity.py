"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-domain, integrity]
owner: engine-team
status: active
--- /L9_META ---

Adversarial native publication integrity contracts; disposable stores only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from engine.config import loader as owner
from tests.unit.test_domain_pack_loader import _minimal_spec_yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def draft(version: str = "1.0.0-reviewed") -> bytes:
    return _minimal_spec_yaml("acme").replace("0.0.1", version).encode()


def source(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "acme" / "spec.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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
