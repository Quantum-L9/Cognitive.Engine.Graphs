"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, ci]
tags: [test, unit, ci, compliance]
owner: engine-team
status: active
--- /L9_META ---

tools/check_compliance_layout.py: banned engine files and design-pack validation,
plus the PacketEnvelope gate's SDK runtime directory, which must not be the one
Analyze (central Core) provisions before `make validate`.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = _load("check_compliance_layout")


def _repo_with_one_valid_pack(tmp_path: Path) -> Path:
    source = sorted(ROOT.glob(layout.DESIGN_PACK_GLOB))[0]
    target = tmp_path / source.relative_to(ROOT)
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    (tmp_path / "engine").mkdir()
    return tmp_path


def test_repository_passes() -> None:
    assert layout.main(ROOT) == 0


def test_banned_engine_middleware_fails(tmp_path: Path) -> None:
    root = _repo_with_one_valid_pack(tmp_path)
    (root / "engine" / "middleware.py").write_text("", encoding="utf-8")
    assert layout.banned_path_errors(root) == ["engine/middleware.py exists (tenant resolution is chassis-only)"]
    assert layout.main(root) == 1


def test_banned_engine_api_directory_fails(tmp_path: Path) -> None:
    root = _repo_with_one_valid_pack(tmp_path)
    (root / "engine" / "api").mkdir()
    assert layout.main(root) == 1


def test_invalid_design_pack_fails(tmp_path: Path) -> None:
    root = _repo_with_one_valid_pack(tmp_path)
    broken = root / "tests/fixtures/design_packs/broken/spec.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("domain: {}\n", encoding="utf-8")
    (error,) = layout.design_pack_errors(root)
    assert "broken/spec.yaml does not validate as a DomainSpec" in error


def test_no_design_pack_fails_rather_than_validating_nothing(tmp_path: Path) -> None:
    assert layout.design_pack_errors(tmp_path) == [f"no {layout.DESIGN_PACK_GLOB} found: the glob matches nothing"]


def test_packet_envelope_gate_keeps_its_own_sdk_runtime() -> None:
    gate = _load("packet_envelope_gate")
    core_runtime = Path(".l9/runtime/sdk")
    assert core_runtime != gate.SDK_RUNTIME_DIR
    assert gate.SDK_RUNTIME_DIR.parts[:2] == (".l9", "runtime")
