"""TASK-034: payload contract compiler validator — domain-neutral (Stage 2 R04)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT, write_synthetic_fixture_set

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPILER = ROOT / "tools" / "payload_contract_compiler.py"


def _load_compiler():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("payload_contract_compiler", COMPILER)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compiler_report_passes_for_repository_example(tmp_path: Path) -> None:
    """The shipped PlasticOS fixtures validate against the shipped PlasticOS pack."""
    mod = _load_compiler()
    report_path = tmp_path / "report.json"
    report = mod.compile_report(write_path=report_path)
    assert report["result"] == "PASS", json.dumps(report, indent=2)[:2000]
    assert report_path.is_file()
    assert report["domain_authority"]["loader"] == "DomainPackLoader"
    assert report["domain_authority"]["domain_id"] == "plasticos"
    assert report["domain_authority"]["match_directions"]
    assert all(s["status"] == "PASS" for s in report["schemas"])
    assert all(p["status"] == "PASS" for p in report["positives"])
    assert all(n["status"] == "PASS" for n in report["negatives"])


@pytest.mark.parametrize("domain_id", ["orbital-logistics", "cheese-aging"])
def test_compiler_passes_for_arbitrary_domain_without_plasticos_directory(tmp_path: Path, domain_id: str) -> None:
    """R04: an approved test domain validates with its own fixtures and no PlasticOS pack on disk."""
    mod = _load_compiler()
    domains_root = tmp_path / "domains"
    shutil.copytree(SYNTHETIC_DOMAINS_ROOT / domain_id, domains_root / domain_id)
    assert not (domains_root / "plasticos").exists()
    fixtures_root = write_synthetic_fixture_set(domain_id, tmp_path / "fixtures")

    report = mod.compile_report(write_path=None, domains_root=domains_root, fixtures_root=fixtures_root)

    assert report["result"] == "PASS", json.dumps(report, indent=2)[:3000]
    assert report["domain_authority"]["domain_id"] == domain_id
    assert report["domain_authority"]["loaded"] is True
    assert len(report["positives"]) == len(list((fixtures_root / "examples").glob("*.json")))
    assert len(report["negatives"]) == len(list((fixtures_root / "negative_examples").glob("*.json")))


def test_compiler_report_is_deterministic(tmp_path: Path) -> None:
    mod = _load_compiler()
    first = mod.compile_report(write_path=None)
    second = mod.compile_report(write_path=None)
    assert first["digest"] == second["digest"]


def test_missing_domain_pack_is_a_hard_failure(tmp_path: Path) -> None:
    """Fixtures naming a domain the loader cannot admit fail the report; nothing is substituted."""
    mod = _load_compiler()
    fixtures_root = write_synthetic_fixture_set("orbital-logistics", tmp_path / "fixtures")
    empty_domains = tmp_path / "domains"
    empty_domains.mkdir()

    report = mod.compile_report(write_path=None, domains_root=empty_domains, fixtures_root=fixtures_root)

    assert report["result"] == "FAIL"
    assert report["domain_authority"]["loaded"] is False
    assert "not found" in report["domain_authority"]["error"]
    assert report["positives"] == []


def test_dormant_domain_pack_is_not_an_authority(tmp_path: Path) -> None:
    """A pack the loader withholds cannot serve as the fixtures' admission authority."""
    mod = _load_compiler()
    domains_root = tmp_path / "domains"
    shutil.copytree(SYNTHETIC_DOMAINS_ROOT / "cheese-aging", domains_root / "cheese-aging")
    spec_path = domains_root / "cheese-aging" / "spec.yaml"
    spec_path.write_text(spec_path.read_text().replace("version: 0.2.0", "version: 0.2.0\n  activation: dormant"))
    fixtures_root = write_synthetic_fixture_set("cheese-aging", tmp_path / "fixtures")

    report = mod.compile_report(write_path=None, domains_root=domains_root, fixtures_root=fixtures_root)

    assert report["result"] == "FAIL"
    assert "dormant" in report["domain_authority"]["error"]


def test_fixtures_naming_two_domains_are_rejected(tmp_path: Path) -> None:
    mod = _load_compiler()
    fixtures_root = write_synthetic_fixture_set("orbital-logistics", tmp_path / "fixtures")
    stray = fixtures_root / "examples" / "match-request.json"
    data = json.loads(stray.read_text())
    data["domain"] = "cheese-aging"
    stray.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="more than one domain"):
        mod.compile_report(write_path=None, domains_root=SYNTHETIC_DOMAINS_ROOT, fixtures_root=fixtures_root)


def test_missing_fixture_set_is_a_hard_failure(tmp_path: Path) -> None:
    mod = _load_compiler()
    with pytest.raises(ValueError, match="no positive fixtures"):
        mod.compile_report(write_path=None, domains_root=SYNTHETIC_DOMAINS_ROOT, fixtures_root=tmp_path)


def test_compiler_cli_exit_zero() -> None:
    mod = _load_compiler()
    report_path = ROOT / "artifacts" / "payload-contract-compiler-cli-test.json"
    try:
        assert mod.main(["--report", str(report_path)]) == 0
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["result"] == "PASS"
    finally:
        if report_path.exists():
            report_path.unlink()


def test_compiler_cli_accepts_domain_roots(tmp_path: Path) -> None:
    mod = _load_compiler()
    fixtures_root = write_synthetic_fixture_set("orbital-logistics", tmp_path / "fixtures")
    assert (
        mod.main(
            [
                "--stdout-only",
                "--domains-root",
                str(SYNTHETIC_DOMAINS_ROOT),
                "--fixtures-root",
                str(fixtures_root),
                "--domain",
                "orbital-logistics",
            ]
        )
        == 0
    )


def test_compiler_cli_rejects_escape_path(tmp_path: Path) -> None:
    mod = _load_compiler()
    with pytest.raises(ValueError, match="repo root"):
        mod.main(["--report", str(tmp_path / "escape.json")])


def test_payload_schemas_exist() -> None:
    payloads = ROOT / "contracts" / "payloads"
    assert (payloads / "common.schema.yaml").is_file()
    assert (payloads / "match-request.schema.yaml").is_file()
    assert (payloads / "canonical-projection.schema.yaml").is_file()
