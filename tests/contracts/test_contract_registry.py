"""
Contract registry drift gate.

Asserts that the three halves of the contract system agree:

- ``contracts/contract_NN.yaml``  — identity and wiring (SSOT)
- ``docs/contracts/*.md``         — prose (SSOT)
- ``tools/contract_scanner.py``   — grep-able rule IDs
- ``tests/contracts/test_contracts.py`` — behavioral assertions

Any pointer that goes stale in one half without the other is caught here
rather than silently rotting.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONTRACTS_DIR = ROOT / "contracts"
SCANNER = ROOT / "tools" / "contract_scanner.py"
VERIFIER = ROOT / "tools" / "verify_contracts.py"

EXPECTED_COUNT = 24
REQUIRED_KEYS = {"id", "name", "layer", "level", "scope", "preconditions", "postconditions", "verification", "docs"}
VALID_LEVELS = {"MUST", "SHOULD", "MAY"}


def _load_contracts() -> list[dict]:
    specs = []
    for f in sorted(CONTRACTS_DIR.glob("contract_*.yaml")):
        specs.append((f.name, yaml.safe_load(f.read_text(encoding="utf-8"))))
    return specs


CONTRACTS = _load_contracts()


def _scanner_rule_ids() -> set[str]:
    """Extract every rule ID registered in contract_scanner.py via AST."""
    tree = ast.parse(SCANNER.read_text(encoding="utf-8"), filename=str(SCANNER))
    ids: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_rule"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            ids.add(node.args[0].value)
    return ids


def _test_class_names() -> set[str]:
    """Class names defined in the monolithic contract test module."""
    path = ROOT / "tests" / "contracts" / "test_contracts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef)}


def _required_contract_docs() -> list[str]:
    """REQUIRED_CONTRACTS list from verify_contracts.py, read without importing."""
    tree = ast.parse(VERIFIER.read_text(encoding="utf-8"), filename=str(VERIFIER))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_CONTRACTS" for t in node.targets
        ):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    msg = "REQUIRED_CONTRACTS not found in tools/verify_contracts.py"
    raise AssertionError(msg)


@pytest.mark.contract
def test_ids_are_contiguous_and_unique():
    ids = [spec["id"] for _f, spec in CONTRACTS]
    assert len(ids) == len(set(ids)), f"Duplicate contract IDs: {ids}"
    expected = [f"CONTRACT-{n:02d}" for n in range(1, EXPECTED_COUNT + 1)]
    assert sorted(ids) == expected, f"Contract IDs must be {expected[0]}..{expected[-1]} with no gaps"


@pytest.mark.contract
def test_filename_matches_id():
    for filename, spec in CONTRACTS:
        number = re.fullmatch(r"contract_(\d{2})\.yaml", filename)
        assert number, f"Unexpected contract filename: {filename}"
        assert spec["id"] == f"CONTRACT-{number.group(1)}", f"{filename} declares {spec['id']}"


@pytest.mark.contract
@pytest.mark.parametrize(("filename", "spec"), CONTRACTS, ids=[f for f, _ in CONTRACTS])
def test_schema_is_uniform(filename, spec):
    missing = REQUIRED_KEYS - set(spec)
    assert not missing, f"{filename} missing keys: {sorted(missing)}"
    assert spec["level"] in VALID_LEVELS, f"{filename} has invalid level {spec['level']!r}"
    assert isinstance(spec["scope"].get("paths"), list), f"{filename} scope.paths must be a list"
    assert isinstance(spec["docs"], list), f"{filename} docs must be a list"
    assert spec["docs"], f"{filename} docs must be non-empty"
    verification = spec["verification"]
    assert isinstance(verification.get("scanner_rules"), list), f"{filename} scanner_rules must be a list"
    assert isinstance(verification.get("test"), str), f"{filename} verification.test must be a string"


@pytest.mark.contract
@pytest.mark.parametrize(("filename", "spec"), CONTRACTS, ids=[f for f, _ in CONTRACTS])
def test_docs_paths_exist(filename, spec):
    missing = [d for d in spec["docs"] if not (ROOT / d).is_file()]
    assert not missing, f"{filename} points at non-existent docs: {missing}"


@pytest.mark.contract
@pytest.mark.parametrize(("filename", "spec"), CONTRACTS, ids=[f for f, _ in CONTRACTS])
def test_scanner_rules_are_registered(filename, spec):
    known = _scanner_rule_ids()
    declared = spec["verification"]["scanner_rules"]
    unknown = [r for r in declared if r not in known]
    assert not unknown, f"{filename} references unregistered scanner rules: {unknown}"


@pytest.mark.contract
@pytest.mark.parametrize(("filename", "spec"), CONTRACTS, ids=[f for f, _ in CONTRACTS])
def test_verification_test_resolves(filename, spec):
    node_id = spec["verification"]["test"]
    file_part, _, class_part = node_id.partition("::")
    assert (ROOT / file_part).exists(), f"{filename} test path does not exist: {file_part}"
    assert class_part, f"{filename} verification.test must be a pytest node ID, got {node_id!r}"
    assert class_part in _test_class_names(), f"{filename} references missing test class: {class_part}"


@pytest.mark.contract
def test_every_required_doc_is_claimed_by_a_contract():
    """Reverse direction: no orphaned markdown in the required set."""
    claimed = {d for _f, spec in CONTRACTS for d in spec["docs"]}
    orphaned = [d for d in _required_contract_docs() if d not in claimed]
    assert not orphaned, f"Required contract docs not referenced by any YAML docs: field: {orphaned}"


@pytest.mark.contract
def test_every_scanner_rule_is_claimed_by_a_contract():
    claimed = {r for _f, spec in CONTRACTS for r in spec["verification"]["scanner_rules"]}
    orphaned = sorted(_scanner_rule_ids() - claimed)
    assert not orphaned, f"Scanner rules not claimed by any contract YAML: {orphaned}"
