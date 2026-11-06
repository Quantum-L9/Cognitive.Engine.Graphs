"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tooling]
tags: [tooling, ci, dependencies, supply-chain]
owner: engine-team
status: active
--- /L9_META ---

Regenerate the hash-locked CI dependency files from the requirements files.

    python tools/deps/lock_requirements.py              # re-render every lock
    python tools/deps/lock_requirements.py --check      # exit 1 when a lock is stale
    python tools/deps/lock_requirements.py --upgrade    # move every pin to the newest release

A render keeps each committed pin that still satisfies the sources: the
committed lock is uv's preference file. So the result depends only on the
repository, not on what PyPI published since. ``--check`` is therefore
deterministic: it fails when a requirements file changed without a re-render,
never because an upstream release appeared. ``--upgrade`` is the explicit
refresh; review its diff like any dependency bump.

Every render also passes uv ``--exclude-newer`` with the cutoff recorded in
tools/deps/exclude-newer.txt, so a wheel uploaded later for an already-pinned
version cannot change the committed hashes either. Only ``--upgrade`` moves the
cutoff: it renders against the current time and records that time there.

CI installs only from these locks, with ``--require-hashes`` and
``--only-binary :all:`` (SonarCloud githubactions:S8541 / S8544, OpenSSF Scorecard
Pinned-Dependencies), through tools/deps/install_locked.sh.

| Lock | Sources |
|---|---|
| requirements-ci.lock | requirements-ci.txt |
| requirements-full.lock | requirements.txt + requirements-dev.txt + requirements-ci.txt |
| requirements-runtime.lock | requirements.txt + pip-licenses (supply-chain license report) |
| requirements-tools.lock | pyyaml + yamllint (small workflow steps) |

constellation-node-sdk is a git dependency, which pip cannot hash. It is resolved
at the commit poetry.lock pins (``resolved_reference``) so its own dependencies
are locked, then written as that commit's source archive with its sha256
(``SDK_ARCHIVE_SHA256``, the same pin Constellation.Gate's requirements.lock
carries). It is the one source build: install_locked.sh names it with
``--no-binary``. When poetry.lock moves the SDK, update SDK_ARCHIVE_COMMIT and
SDK_ARCHIVE_SHA256 together (``pip hash`` on the new archive); rendering refuses
a mismatch.

Requires uv (https://docs.astral.sh/uv/).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUDE_NEWER_FILE = Path(__file__).resolve().parent / "exclude-newer.txt"
SDK_NAME = "constellation-node-sdk"
SDK_REPO = "https://github.com/Quantum-L9/Gate_SDK.git"
SDK_ARCHIVE = "https://github.com/Quantum-L9/Gate_SDK/archive/{commit}.tar.gz"
SDK_ARCHIVE_COMMIT = "bc1c74c06d70126e82eba73c47df8f2f2af4bc2b"
SDK_ARCHIVE_SHA256 = "cd9d2c206af8c1740cff4369f3ef408330aa73d3e8768e89f84ef43abe45cd64"
PYTHON_VERSION = "3.12"
COMMAND = "python tools/deps/lock_requirements.py"
LOCKS: dict[str, tuple[str, ...]] = {
    "requirements-ci.lock": ("requirements-ci.txt",),
    "requirements-full.lock": ("requirements.txt", "requirements-dev.txt", "requirements-ci.txt"),
    "requirements-runtime.lock": ("requirements.txt",),
    "requirements-tools.lock": (),
}
# Extra pins locked alongside a lock's sources. The license report must cover the
# runtime set only (CI tools carry licenses the allow-list does not name), plus
# the reporter itself; the version matches requirements-ci.txt.
EXTRAS: dict[str, tuple[str, ...]] = {
    "requirements-runtime.lock": ("pip-licenses==5.5.5",),
    # uv is the renderer `--check` runs in CI (Gate_SDK Channel job). Pin the
    # release that rendered the committed locks: another uv can format the same
    # resolution differently, which would fail the byte comparison spuriously.
    "requirements-tools.lock": ("pyyaml==6.0.3", "yamllint==1.38.0", "uv==0.11.32"),
}
_SDK_LINE = re.compile(rf"^{re.escape(SDK_NAME)}\s*@\s*\S+", re.MULTILINE)


def sdk_commit() -> str:
    """The Gate_SDK commit poetry.lock pins: the single source for the SDK pin."""
    lock = tomllib.loads((ROOT / "poetry.lock").read_text())
    for package in lock.get("package", []):
        if package.get("name") == SDK_NAME:
            commit = package.get("source", {}).get("resolved_reference")
            if commit:
                if commit != SDK_ARCHIVE_COMMIT:
                    msg = (
                        f"poetry.lock pins {SDK_NAME} at {commit}, but SDK_ARCHIVE_SHA256 is for "
                        f"{SDK_ARCHIVE_COMMIT}: update SDK_ARCHIVE_COMMIT and SDK_ARCHIVE_SHA256 together"
                    )
                    raise SystemExit(msg)
                return str(commit)
    msg = f"poetry.lock has no resolved_reference for {SDK_NAME}"
    raise SystemExit(msg)


def sdk_archive_line(commit: str) -> str:
    """The hashed archive requirement the locks carry for the Gate SDK."""
    return f"{SDK_NAME} @ {SDK_ARCHIVE.format(commit=commit)} \\\n    --hash=sha256:{SDK_ARCHIVE_SHA256}"


def _pinned_sources(sources: tuple[str, ...], commit: str, workdir: Path) -> list[Path]:
    """Copies of the sources with the SDK's moving ref replaced by poetry.lock's commit."""
    pinned = []
    for name in sources:
        text = (ROOT / name).read_text()
        text = _SDK_LINE.sub(f"{SDK_NAME} @ git+{SDK_REPO}@{commit}", text)
        target = workdir / name
        target.write_text(text)
        pinned.append(target)
    return pinned


def _hash_sdk(body: str, commit: str) -> str:
    """Replace the unhashable git SDK entry with the commit's hashed source archive."""
    return _SDK_LINE.sub(lambda _: sdk_archive_line(commit), body)


def read_exclude_newer(path: Path | None = None) -> str:
    """The recorded uv ``--exclude-newer`` cutoff (RFC 3339, UTC)."""
    cutoff = (path or EXCLUDE_NEWER_FILE).read_text().strip()
    try:
        datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        msg = f"{EXCLUDE_NEWER_FILE.name}: {cutoff!r} is not YYYY-MM-DDTHH:MM:SSZ"
        raise SystemExit(msg) from exc
    return cutoff


def render(lock: str, commit: str, *, exclude_newer: str, upgrade: bool = False) -> str:
    """The lock file content uv produces for ``lock``'s sources.

    Without ``upgrade`` the committed lock seeds the output file, which uv reads
    as resolver preferences: every pin that still satisfies the sources is kept.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        sources = _pinned_sources(LOCKS[lock], commit, workdir)
        if EXTRAS.get(lock):
            extras = workdir / "extras.txt"
            extras.write_text("\n".join(EXTRAS[lock]) + "\n")
            sources.append(extras)
        out = workdir / lock
        committed = ROOT / lock
        if not upgrade and committed.exists():
            out.write_text(committed.read_text())
        subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                *map(str, sources),
                "--universal",
                "--python-version",
                PYTHON_VERSION,
                "--generate-hashes",
                "--custom-compile-command",
                COMMAND,
                "--output-file",
                str(out),
                "--quiet",
                "--exclude-newer",
                exclude_newer,
                *(["--upgrade"] if upgrade else []),
            ],
            cwd=workdir,
            check=True,
        )
        body = out.read_text().replace(str(workdir) + "/", "").replace("-r extras.txt", "lock_requirements.EXTRAS")
    return _hash_sdk(body, commit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 when a lock differs from a re-render")
    mode.add_argument("--upgrade", action="store_true", help="move every pin to the newest allowed release")
    args = parser.parse_args(argv)
    commit = sdk_commit()
    cutoff = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if args.upgrade else read_exclude_newer()
    stale = []
    for lock in LOCKS:
        fresh = render(lock, commit, exclude_newer=cutoff, upgrade=args.upgrade)
        path = ROOT / lock
        if args.check:
            if not path.exists() or path.read_text() != fresh:
                stale.append(lock)
        else:
            path.write_text(fresh)
            print(f"wrote {lock}")
    if args.upgrade:
        EXCLUDE_NEWER_FILE.write_text(cutoff + "\n")
        print(f"wrote {EXCLUDE_NEWER_FILE.name}: {cutoff}")
    if stale:
        print(f"stale: {', '.join(stale)} — run {COMMAND}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
