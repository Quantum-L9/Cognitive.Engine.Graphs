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

    python tools/deps/lock_requirements.py            # rewrite every lock
    python tools/deps/lock_requirements.py --check    # exit 1 when a lock is stale

CI installs only from these locks, with ``--require-hashes`` and
``--only-binary :all:`` (SonarCloud githubactions:S8541 / S8544, OpenSSF Scorecard
Pinned-Dependencies), through tools/deps/install_locked.sh.

| Lock | Sources |
|---|---|
| requirements-ci.lock | requirements-ci.txt |
| requirements-full.lock | requirements.txt + requirements-ci.txt |
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SDK_NAME = "constellation-node-sdk"
SDK_REPO = "https://github.com/Quantum-L9/Gate_SDK.git"
SDK_ARCHIVE = "https://github.com/Quantum-L9/Gate_SDK/archive/{commit}.tar.gz"
SDK_ARCHIVE_COMMIT = "7ec6cdf5e26c837057ca35b7324a88de4d499ea3"
SDK_ARCHIVE_SHA256 = "f397d6951a2615d92839864824ff5c1d48316ca7d9052b10519d157f3885ab6e"
PYTHON_VERSION = "3.12"
COMMAND = "python tools/deps/lock_requirements.py"
LOCKS: dict[str, tuple[str, ...]] = {
    "requirements-ci.lock": ("requirements-ci.txt",),
    "requirements-full.lock": ("requirements.txt", "requirements-ci.txt"),
    "requirements-runtime.lock": ("requirements.txt",),
    "requirements-tools.lock": (),
}
# Extra pins locked alongside a lock's sources. The license report must cover the
# runtime set only (CI tools carry licenses the allow-list does not name), plus
# the reporter itself; the version matches requirements-ci.txt.
EXTRAS: dict[str, tuple[str, ...]] = {
    "requirements-runtime.lock": ("pip-licenses==5.5.5",),
    "requirements-tools.lock": ("pyyaml==6.0.3", "yamllint==1.38.0"),
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


def render(lock: str, commit: str) -> str:
    """The lock file content uv produces for ``lock``'s sources."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        sources = _pinned_sources(LOCKS[lock], commit, workdir)
        if EXTRAS.get(lock):
            extras = workdir / "extras.txt"
            extras.write_text("\n".join(EXTRAS[lock]) + "\n")
            sources.append(extras)
        out = workdir / lock
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
            ],
            cwd=workdir,
            check=True,
        )
        body = out.read_text().replace(str(workdir) + "/", "").replace("-r extras.txt", "lock_requirements.EXTRAS")
    return _hash_sdk(body, commit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="exit 1 when a lock differs from a fresh render")
    args = parser.parse_args(argv)
    commit = sdk_commit()
    stale = []
    for lock in LOCKS:
        fresh = render(lock, commit)
        path = ROOT / lock
        if args.check:
            if not path.exists() or path.read_text() != fresh:
                stale.append(lock)
        else:
            path.write_text(fresh)
            print(f"wrote {lock}")
    if stale:
        print(f"stale: {', '.join(stale)} — run {COMMAND}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
