"""Unit tests — moving-major Gate_SDK pin validator (CEG#266)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "validate_sdk_pin",
    _ROOT / "scripts" / "validate_sdk_pin.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
MAJOR_TAG = _mod.MAJOR_TAG
check_text = _mod.check_text
check_tree = _mod.check_tree


@pytest.mark.unit
def test_manifests_accept_major_tag() -> None:
    pyproject = 'constellation-node-sdk = {git = "https://github.com/Quantum-L9/Gate_SDK.git", rev = "v1"}\n'
    requirements = "constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@v1\n"
    assert check_text("pyproject.toml", pyproject) == []
    assert check_text("requirements.txt", requirements) == []


@pytest.mark.unit
def test_manifests_reject_sha_and_fork() -> None:
    sha = "69c6c67060b08440734a61473c03663423709964"
    text = f'constellation-node-sdk = {{git = "https://github.com/cryptoxdog/Gate_SDK.git", rev = "{sha}"}}\n'
    errors = check_text("pyproject.toml", text)
    assert any("cryptoxdog" in item for item in errors)
    assert any("SHA" in item for item in errors)
    assert any("Quantum-L9" in item for item in errors)


@pytest.mark.unit
def test_lock_requires_tag_reference_and_resolved_sha() -> None:
    lock = (
        'name = "constellation-node-sdk"\n'
        'version = "1.1.0"\n'
        "[package.source]\n"
        'type = "git"\n'
        'url = "https://github.com/Quantum-L9/Gate_SDK.git"\n'
        'reference = "v1"\n'
        'resolved_reference = "e9f829f982110be13752da8f18c7a9692e8ed908"\n'
    )
    assert check_text("poetry.lock", lock) == []
    stale = lock.replace('reference = "v1"', 'reference = "69c6c67060b08440734a61473c03663423709964"')
    assert check_text("poetry.lock", stale)


@pytest.mark.unit
def test_repo_tree_matches_v1_policy() -> None:
    errors = check_tree(_ROOT)
    assert errors == [], errors
    assert MAJOR_TAG == "v1"
