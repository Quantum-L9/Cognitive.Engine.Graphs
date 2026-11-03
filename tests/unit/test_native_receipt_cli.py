"""The offline CLI consumes the same CEG-owned receipt contract as the loader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tools.validate_domain import validate_spec

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _inputs(tmp_path):
    raw = yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_bytes())
    raw["domain"]["version"] = "1.3.0-reviewed"
    spec = tmp_path / "plasticos/spec.yaml"
    spec.parent.mkdir()
    data = yaml.safe_dump(raw, sort_keys=False).encode()
    spec.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    receipt = tmp_path / "receipt.json"
    body = {
        "schema": "ceg.domain-approval/v1",
        "tenant_id": "plasticos",
        "version": "1.3.0-reviewed",
        "sha256": digest,
        "reviewed_input_sha256": digest,
        "reviewed_by": "test-fixture-only",
        "approval_ref": "test-fixture-only",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "previous_sha256": None,
    }
    receipt.write_text(json.dumps(body))
    return spec, receipt, body


def test_native_cli_receipt_passes_exact_reviewed_bytes(tmp_path):
    spec, receipt, _ = _inputs(tmp_path)
    results, code = validate_spec(spec, complete=True, require_reviewed=True, receipt_path=receipt)
    assert code == 0
    assert results[0]["status"] == "PASS"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "other-tenant"),
        ("version", "1.2.0-reviewed"),
        ("sha256", "a" * 64),
        ("reviewed_input_sha256", "b" * 64),
        ("reviewed_at", "2999-01-01T00:00:00Z"),
        ("reviewed_by", " "),
    ],
)
def test_native_cli_refuses_bad_native_receipt(tmp_path, field, value):
    spec, receipt, body = _inputs(tmp_path)
    body[field] = value
    receipt.write_text(json.dumps(body))
    results, code = validate_spec(spec, receipt_path=receipt)
    assert code == 1
    assert results[0]["status"] == "FAIL"


def test_duplicate_json_receipt_key_is_refused(tmp_path):
    spec, receipt, body = _inputs(tmp_path)
    receipt.write_text(json.dumps(body)[:-1] + ', "tenant_id": "plasticos"}')
    _, code = validate_spec(spec, receipt_path=receipt)
    assert code == 1
