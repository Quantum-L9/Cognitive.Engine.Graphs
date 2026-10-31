"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, ci, dependencies, supply-chain]
owner: engine-team
status: active
--- /L9_META ---

The hash-locked CI dependency files stay in step with their sources.

CI installs only from requirements-*.lock (tools/deps/install_locked.sh), so a
requirements file that moves without a lock regeneration would silently stop
reaching CI. Each source specifier must be satisfied by the locked version, every
locked package must carry a hash, and the Gate SDK pin must be poetry.lock's.
Regenerate with: python tools/deps/lock_requirements.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "deps"))

import lock_requirements  # noqa: E402

pytestmark = pytest.mark.unit

_PIN = re.compile(r"^([A-Za-z0-9_.\-\[\],]+)==([^\s\\;]+)")


def _locked(lock: str) -> tuple[dict[str, str], dict[str, int], str | None]:
    """Locked versions, hash counts per package, and the ``# sdk:`` line."""
    versions: dict[str, str] = {}
    hashes: dict[str, int] = {}
    sdk = None
    current = None
    for line in (ROOT / lock).read_text().splitlines():
        if line.startswith("# sdk: "):
            sdk = line.removeprefix("# sdk: ")
            continue
        match = _PIN.match(line)
        if match:
            current = canonicalize_name(match.group(1).split("[")[0])
            versions[current] = match.group(2)
            hashes[current] = 0
        elif current and line.strip().startswith("--hash="):
            hashes[current] += 1
    return versions, hashes, sdk


def _source_requirements(name: str) -> list[Requirement]:
    reqs = []
    for raw in (ROOT / name).read_text().splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith(("#", "-r")):
            continue
        try:
            reqs.append(Requirement(line))
        except InvalidRequirement:
            pytest.fail(f"{name}: unparseable requirement {line!r}")
    return reqs


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_every_source_requirement_is_satisfied_by_the_lock(lock: str) -> None:
    versions, _, _ = _locked(lock)
    specs = [r for name in lock_requirements.LOCKS[lock] for r in _source_requirements(name)]
    specs += [Requirement(extra) for extra in lock_requirements.EXTRAS.get(lock, ())]
    stale = []
    for req in specs:
        if req.url:
            continue  # the Gate SDK: checked against poetry.lock below
        locked = versions.get(canonicalize_name(req.name))
        if locked is None or not req.specifier.contains(locked, prereleases=True):
            stale.append(f"{req} (locked: {locked})")
    assert not stale, f"{lock} is stale — run python tools/deps/lock_requirements.py: {stale}"


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_every_locked_package_is_hashed(lock: str) -> None:
    _, hashes, _ = _locked(lock)
    assert hashes
    assert [name for name, count in hashes.items() if count == 0] == []


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_the_gate_sdk_pin_is_poetry_locks(lock: str) -> None:
    _, _, sdk = _locked(lock)
    wants_sdk = any(r.url for name in lock_requirements.LOCKS[lock] for r in _source_requirements(name))
    if not wants_sdk:
        assert sdk is None
        return
    assert sdk == f"{lock_requirements.SDK_NAME} @ git+{lock_requirements.SDK_REPO}@{lock_requirements.sdk_commit()}"


def test_installs_are_wheel_only_and_hash_checked() -> None:
    script = (ROOT / "tools" / "deps" / "install_locked.sh").read_text()
    installs = [
        line.strip() for line in script.splitlines() if "pip install" in line and not line.lstrip().startswith("#")
    ]
    assert installs == [
        'python -m pip install --only-binary :all: --require-hashes -r "$lock"',
        'python -m pip install --only-binary :all: --no-deps "$sdk"',
    ]
