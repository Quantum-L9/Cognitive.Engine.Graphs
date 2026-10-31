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
locked package must trace back (through uv's ``# via`` annotations) to a
requirement still in a source file, every locked package must carry a hash, and
the Gate SDK pin must be poetry.lock's.
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
_URL_PIN = re.compile(r"^([A-Za-z0-9_.\-]+) @ (\S+)")


def _locked(lock: str) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
    """Locked versions, hash counts per requirement, and URL-pinned requirements."""
    versions: dict[str, str] = {}
    hashes: dict[str, int] = {}
    urls: dict[str, str] = {}
    current = None
    for line in (ROOT / lock).read_text().splitlines():
        if match := _PIN.match(line):
            current = canonicalize_name(match.group(1).split("[")[0])
            versions[current] = match.group(2)
            hashes[current] = 0
        elif match := _URL_PIN.match(line):
            current = canonicalize_name(match.group(1))
            urls[current] = match.group(2)
            hashes[current] = 0
        elif current and line.strip().startswith("--hash="):
            hashes[current] += 1
    return versions, hashes, urls


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


_EXTRAS_VIA = "lock_requirements.EXTRAS"


def _via(lock: str) -> dict[str, set[str]]:
    """Each locked package and what uv says pulled it in (``-r file``, extras, or packages)."""
    via: dict[str, set[str]] = {}
    current = None
    in_block = False
    for line in (ROOT / lock).read_text().splitlines():
        if match := _PIN.match(line) or _URL_PIN.match(line):
            current = canonicalize_name(match.group(1).split("[")[0])
            via[current] = set()
            in_block = False
        elif current is None:
            continue
        elif line.startswith("    # via "):
            via[current].add(line.removeprefix("    # via ").strip())
        elif line == "    # via":
            in_block = True
        elif in_block and line.startswith("    #   "):
            via[current].add(line.removeprefix("    #   ").strip())
        else:
            in_block = False
    return via


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_every_locked_package_traces_to_a_current_source(lock: str) -> None:
    """A requirement removed from its source must not linger in the lock with its dependencies."""
    via = _via(lock)
    direct = {canonicalize_name(r.name) for name in lock_requirements.LOCKS[lock] for r in _source_requirements(name)}
    direct |= {canonicalize_name(Requirement(e).name) for e in lock_requirements.EXTRAS.get(lock, ())}
    sources = {f"-r {name}" for name in lock_requirements.LOCKS[lock]} | {_EXTRAS_VIA}
    reachable = {pkg for pkg, parents in via.items() if pkg in direct and parents & sources}
    grew = True
    while grew:
        grew = False
        for pkg, parents in via.items():
            if pkg not in reachable and {canonicalize_name(p) for p in parents} & reachable:
                reachable.add(pkg)
                grew = True
    assert set(via) - reachable == set(), (
        f"{lock} keeps packages no current requirement pulls in — run python tools/deps/lock_requirements.py"
    )


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_every_locked_package_is_hashed(lock: str) -> None:
    _, hashes, _ = _locked(lock)
    assert hashes
    assert [name for name, count in hashes.items() if count == 0] == []


@pytest.mark.parametrize("lock", sorted(lock_requirements.LOCKS))
def test_the_gate_sdk_is_poetry_locks_commit_as_a_hashed_archive(lock: str) -> None:
    _, _, urls = _locked(lock)
    wants_sdk = any(r.url for name in lock_requirements.LOCKS[lock] for r in _source_requirements(name))
    if not wants_sdk:
        assert urls == {}
        return
    commit = lock_requirements.sdk_commit()
    assert urls == {lock_requirements.SDK_NAME: lock_requirements.SDK_ARCHIVE.format(commit=commit)}
    body = (ROOT / lock).read_text()
    assert lock_requirements.sdk_archive_line(commit) in body


def test_installs_are_wheel_only_and_hash_checked() -> None:
    script = (ROOT / "tools" / "deps" / "install_locked.sh").read_text()
    installs = [
        line.strip() for line in script.splitlines() if "pip install" in line and not line.lstrip().startswith("#")
    ]
    assert installs == [
        'python -m pip install --only-binary :all: --no-binary constellation-node-sdk --require-hashes -r "$lock"',
    ]
