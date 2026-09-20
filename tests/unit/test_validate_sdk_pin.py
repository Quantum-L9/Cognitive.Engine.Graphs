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
if _SPEC is None:
    raise RuntimeError("validate_sdk_pin.py did not load")
if _SPEC.loader is None:
    raise RuntimeError("validate_sdk_pin.py has no loader")
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
MAJOR_TAG = _mod.MAJOR_TAG
check_text = _mod.check_text
check_tree = _mod.check_tree
lock_resolution = _mod.lock_resolution
compare_lock_to_tag = _mod.compare_lock_to_tag

CHANNEL_OBJECT = "e9f829f982110be13752da8f18c7a9692e8ed908"
STALE_OBJECT = "69c6c67060b08440734a61473c03663423709964"


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


# ── stale-lock agreement (--verify-tag logic, exercised without network) ──────
#
# The structural checks above cannot catch a stale lock: `v1` moves in Gate_SDK
# and nothing in this repository changes. These cover the comparison that does.


@pytest.mark.unit
def test_lock_resolution_reads_the_resolved_object() -> None:
    lock = (
        'name = "constellation-node-sdk"\n'
        "[package.source]\n"
        'url = "https://github.com/Quantum-L9/Gate_SDK.git"\n'
        'reference = "v1"\n'
        f'resolved_reference = "{CHANNEL_OBJECT}"\n'
    )
    assert lock_resolution(lock) == CHANNEL_OBJECT
    assert lock_resolution("no sdk source block here\n") is None


@pytest.mark.unit
def test_a_lock_current_with_the_channel_passes() -> None:
    assert compare_lock_to_tag(CHANNEL_OBJECT, CHANNEL_OBJECT) == []


@pytest.mark.unit
def test_a_stale_lock_fails() -> None:
    errors = compare_lock_to_tag(STALE_OBJECT, CHANNEL_OBJECT)
    assert any("stale" in item for item in errors), errors


@pytest.mark.unit
def test_an_unresolvable_channel_fails_closed() -> None:
    """Required networked mode: inability to resolve is a failure, not a pass."""
    errors = compare_lock_to_tag(CHANNEL_OBJECT, None)
    assert any("could not resolve" in item for item in errors), errors


@pytest.mark.unit
def test_a_lock_without_a_resolved_reference_fails_closed() -> None:
    errors = compare_lock_to_tag(None, CHANNEL_OBJECT)
    assert any("no resolved_reference" in item for item in errors), errors
