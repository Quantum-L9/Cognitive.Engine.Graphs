"""TASK-034: payload contract compiler validator."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


def test_compiler_report_passes(tmp_path: Path) -> None:
    mod = _load_compiler()
    report_path = tmp_path / "report.json"
    report = mod.compile_report(write_path=report_path)
    assert report["result"] == "PASS", json.dumps(report, indent=2)[:2000]
    assert report_path.is_file()
    assert report["domain_authority"]["loader"] == "DomainPackLoader"
    assert report["domain_authority"]["match_directions"]
    assert all(s["status"] == "PASS" for s in report["schemas"])
    assert all(p["status"] == "PASS" for p in report["positives"])
    assert all(n["status"] == "PASS" for n in report["negatives"])


def test_compiler_cli_exit_zero(tmp_path: Path) -> None:
    mod = _load_compiler()
    report_path = tmp_path / "cli-report.json"
    assert mod.main(["--report", str(report_path)]) == 0
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["result"] == "PASS"


def test_payload_schemas_exist() -> None:
    payloads = ROOT / "contracts" / "payloads"
    assert (payloads / "common.schema.yaml").is_file()
    assert (payloads / "match-request.schema.yaml").is_file()
    assert (payloads / "canonical-projection.schema.yaml").is_file()
