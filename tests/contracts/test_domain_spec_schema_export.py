"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tests]
tags: [tests, contracts, domain-spec, json-schema]
owner: engine-team
status: active
--- /L9_META ---

Drift and conformance tests for the published DomainSpec JSON Schema (#400 B1/B2).

Sources:
  engine/config/schema.py:DomainSpec
  engine/config/domain_spec_schema_version.py:DOMAIN_SPEC_SCHEMA_VERSION
  tools/export_domain_spec_schema.py
  docs/contracts/data/models/domain-spec.schema.json
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from engine.config.domain_spec_schema_version import DOMAIN_SPEC_SCHEMA_VERSION
from tools import export_domain_spec_schema as exporter

pytestmark = pytest.mark.unit

FIXTURE_ROOT = exporter.REPO_ROOT / "tests" / "fixtures" / "domains"
FIXTURE_PACKS = sorted(FIXTURE_ROOT.glob("*/spec.yaml"))


def _committed_text() -> str:
    return exporter.ARTIFACT_PATH.read_text(encoding="utf-8")


def _validator() -> Draft202012Validator:
    schema = json.loads(_committed_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _pack(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a YAML mapping"
    return loaded


def test_committed_artifact_equals_fresh_export() -> None:
    assert _committed_text() == exporter.render(), (
        "docs/contracts/data/models/domain-spec.schema.json drifted from engine/config/schema.py. "
        "Bump DOMAIN_SPEC_SCHEMA_VERSION and run: python tools/export_domain_spec_schema.py"
    )


def test_export_is_deterministic() -> None:
    first = exporter.render()
    second = exporter.render()
    assert first == second
    assert first.endswith("}\n")


def test_id_carries_declared_version() -> None:
    schema = json.loads(_committed_text())
    assert schema["$id"] == exporter.schema_id(DOMAIN_SPEC_SCHEMA_VERSION)
    assert f"/{DOMAIN_SPEC_SCHEMA_VERSION}/" in schema["$id"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "DomainSpec"


def test_check_passes_on_committed_artifact(capsys: pytest.CaptureFixture[str]) -> None:
    assert exporter.main(["--check"]) == 0
    assert f"version={DOMAIN_SPEC_SCHEMA_VERSION}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace('"title": "DomainSpec"', '"title": "DomainSpecX"', 1),
        lambda text: text.rstrip("\n"),
        lambda text: text + "\n",
    ],
    ids=["value-edit", "missing-trailing-newline", "extra-trailing-newline"],
)
def test_check_fails_on_edited_artifact(mutate: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edited = tmp_path / "domain-spec.schema.json"
    mutated = mutate(_committed_text())
    assert mutated != _committed_text()
    edited.write_text(mutated, encoding="utf-8")

    assert exporter.main(["--check", "--root", str(tmp_path), "--artifact", str(edited)]) == 1
    err = capsys.readouterr().err
    assert "DRIFT" in err
    assert str(edited) in err
    assert edited.read_text(encoding="utf-8") == mutated, "--check must not write"


def test_check_fails_on_missing_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert exporter.main(["--check", "--root", str(tmp_path), "--artifact", str(tmp_path / "absent.json")]) == 1
    assert "missing" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--check"], []], ids=["check", "write"])
def test_artifact_outside_root_is_refused(flag: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"

    assert exporter.main([*flag, "--root", str(root), "--artifact", str(outside)]) == 2
    assert "--artifact must stay under --root" in capsys.readouterr().err
    assert not outside.exists(), "a refused path must not be written"

    # The default root is the repository: a path outside it is refused too.
    assert exporter.main([*flag, "--artifact", str(outside)]) == 2
    assert not outside.exists()


def test_fixture_packs_were_discovered() -> None:
    # An empty glob would make the parametrized conformance test pass vacuously.
    assert len(FIXTURE_PACKS) >= 2, f"expected synthetic packs under {FIXTURE_ROOT}"


@pytest.mark.parametrize("pack", FIXTURE_PACKS, ids=lambda p: p.parent.name)
def test_fixture_pack_validates_against_artifact(pack: Path) -> None:
    errors = sorted(_validator().iter_errors(_pack(pack)), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]


@pytest.mark.parametrize("pack", FIXTURE_PACKS, ids=lambda p: p.parent.name)
def test_mutated_pack_is_rejected(pack: Path) -> None:
    validator = _validator()
    valid = _pack(pack)

    missing_required = copy.deepcopy(valid)
    del missing_required["ontology"]
    wrong_type = copy.deepcopy(valid)
    wrong_type["gates"] = "not-a-list"
    bad_nested = copy.deepcopy(valid)
    bad_nested["domain"]["id"] = 42

    for name, mutant, path in (
        ("missing required section", missing_required, []),
        ("wrong section type", wrong_type, ["gates"]),
        ("wrong nested field type", bad_nested, ["domain", "id"]),
    ):
        errors = list(validator.iter_errors(mutant))
        assert errors, f"{pack.parent.name}: {name} was accepted"
        assert any(list(e.absolute_path) == path for e in errors), f"{name}: no error at {path}"
