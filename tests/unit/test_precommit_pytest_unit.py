"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [precommit, pytest, unit]
owner: platform
status: active
--- /L9_META ---

Unit tests for tools/run_precommit_pytest_unit.py path selection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

from run_precommit_pytest_unit import infer_unit_tests, select_unit_tests  # noqa: E402


@pytest.mark.unit
def test_compiler_change_selects_gate_compiler_not_catalog() -> None:
    selected = select_unit_tests(["engine/gates/compiler.py"], REPO)
    assert "tests/unit/test_gate_compiler.py" in selected
    assert "tests/unit/test_payload_contract_compiler.py" not in selected
    assert "tests/unit/test_gates_all_types.py" not in selected
    assert "tests/" not in selected


@pytest.mark.unit
def test_staged_slow_file_is_allowed() -> None:
    selected = select_unit_tests(["tests/unit/test_scoring.py"], REPO)
    assert selected == ["tests/unit/test_scoring.py"]


@pytest.mark.unit
def test_integration_change_is_ignored() -> None:
    selected = select_unit_tests(["tests/integration/test_pipeline.py"], REPO)
    assert selected == []


@pytest.mark.unit
def test_unknown_impl_does_not_dump_unit_dir() -> None:
    selected = select_unit_tests(["engine/does_not_exist_zzzz.py"], REPO)
    assert selected == []
    assert "tests/unit" not in selected


@pytest.mark.unit
def test_infer_parent_stem_mapping() -> None:
    hits = infer_unit_tests("engine/gates/compiler.py", REPO)
    assert "tests/unit/test_gate_compiler.py" in hits
