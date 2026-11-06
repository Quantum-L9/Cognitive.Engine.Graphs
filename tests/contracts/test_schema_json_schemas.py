"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, contracts]
owner: engine-team
status: active
--- /L9_META ---

Verify the two API contracts against actual Pydantic class definitions and
chassis error classes in code. Do not treat README/OpenAPI prose as source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _load(root, rel):
    p = root / "api/schemas" / rel
    assert p.is_file(), p
    doc = yaml.safe_load(p.read_text())
    assert doc["source_refs"]
    assert doc["baseline"]["commit"]
    return doc


def _source(path):
    p = REPO / path
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _class_fields(name: str) -> set[str]:
    tree = ast.parse(_source("chassis/chassis_app.py"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    return {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}


def test_shared_models_legacy_envelopes_match_pydantic_sources(contracts_root) -> None:
    d = _load(contracts_root, "shared-models.yaml")
    assert d["schema"] == "l9.ceg.api.shared-models/v1"
    assert _class_fields("ExecuteRequest") == set(d["models"]["legacy_execute_request"]["declared_fields"])
    assert _class_fields("ExecuteResponse") == set(d["models"]["legacy_execute_response"]["declared_fields"])
    assert d["models"]["sdk_transport_packet"]["live_model_fields_verified_at_source_sha"] is False
    assert d["models"]["typed_match_request"]["owner"] == "engine/models/payloads.py:MatchRequest"
    assert d["pagination"]["paginated_model"] is None


def test_error_classes_source_status_codes_and_wire_shape(contracts_root) -> None:
    d = _load(contracts_root, "error-responses.yaml")
    classes = ast.parse(_source("chassis/errors.py"))
    actual = {}
    for c in classes.body:
        if isinstance(c, ast.ClassDef):
            for n in c.body:
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "status_code":
                    if isinstance(n.value, ast.Constant) and isinstance(n.value.value, int):
                        actual[c.name] = n.value.value
    assert d["modes"]["legacy_dev_only"]["chassis_exception_codes"] == {
        key: actual[key] for key in d["modes"]["legacy_dev_only"]["chassis_exception_codes"]
    }
    fields = set(d["modes"]["legacy_dev_only"]["exception_wire_shape"])
    assert fields == {"error", "message", "action", "tenant", "detail", "context"}
    assert "def to_dict(self)" in _source("chassis/errors.py")
    legacy_app = _source("chassis/chassis_app.py")
    assert "_raise_for_failed_result" in legacy_app
    assert "_raise_for_status_hint" in legacy_app
    assert "legacy_chassis_status_mapping_enabled" in legacy_app
    assert d["modes"]["sdk_production"]["failure_mapping_proven_here"] is False
