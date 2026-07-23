#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: ceg
# engine: graph
# layer: [ci, tools]
# tags: [ci, baseline-ratchet, packet-envelope]
# owner: platform
# status: active
# --- /L9_META ---
"""Thin PacketEnvelope debt gate: delegates to the pinned l9-ci-sdk.

This wrapper contains NO scanning, normalization, fingerprinting, or
comparison logic. It only:

1. Provisions the immutable l9-ci-sdk at the pinned revision below
   (idempotent shallow clone into ``.l9/runtime/sdk``).
2. Runs ``l9_ci baseline scan-packet-envelope`` (full-repository AST scan,
   declaration sites exempt).
3. Runs ``l9_ci baseline compare-scan`` against the human-reviewed ledger
   at ``.l9/baselines/packet-envelope.yml``.
4. Exits with the SDK's fail-closed exit code (0 pass, nonzero block).

The same SDK revision and ledger are used by the blocking CI check
"Baseline Ratchet / Quarantined Debt" (see
.github/workflows/baseline-ratchet-caller.yml), so local pre-commit and CI
can never disagree about what constitutes tolerated PacketEnvelope debt.

Ledger updates are made ONLY by humans in reviewed PRs (CODEOWNERS-gated);
neither this wrapper nor CI ever writes the ledger.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SDK_REPOSITORY = "https://github.com/Quantum-L9/l9-ci-sdk.git"
SDK_REVISION = "b390dc78e3464cca539b998dfb723481927ed91b"
SDK_RUNTIME_DIR = Path(".l9/runtime/sdk")
LEDGER_PATH = Path(".l9/baselines/packet-envelope.yml")
DECLARATION_PATHS = (
    "engine/packet/packet_envelope.py",
    "l9_core/models.py",
)
GATE_LABEL = "scanner/packet-envelope"


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=False, **kwargs)  # type: ignore[call-overload]


def _provision_sdk(repo_root: Path) -> Path:
    """Ensure the pinned SDK revision exists; return its checkout path."""
    sdk_root = repo_root / SDK_RUNTIME_DIR
    head_file = sdk_root / ".git" / "HEAD"
    if head_file.is_file():
        head = _run(
            ["git", "-C", str(sdk_root), "rev-parse", "HEAD"],
            capture_output=True,
        )
        if head.returncode == 0 and head.stdout.decode().strip() == SDK_REVISION:
            return sdk_root
    sdk_root.parent.mkdir(parents=True, exist_ok=True)
    if sdk_root.exists():
        import shutil

        shutil.rmtree(sdk_root)
    clone = _run(["git", "clone", "--no-checkout", SDK_REPOSITORY, str(sdk_root)])
    if clone.returncode != 0:
        print("error: cannot clone l9-ci-sdk", file=sys.stderr)
        raise SystemExit(1)
    checkout = _run(["git", "-C", str(sdk_root), "checkout", "--detach", SDK_REVISION])
    if checkout.returncode != 0:
        print(f"error: cannot checkout SDK revision {SDK_REVISION}", file=sys.stderr)
        raise SystemExit(1)
    return sdk_root


def main() -> int:
    # pre-commit passes filenames when pass_filenames is true; this gate
    # deliberately ignores them: the directive requires that changed-file
    # scanning can never bypass the full-repository baseline comparison.
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    ledger = repo_root / LEDGER_PATH
    if not ledger.is_file():
        print(f"error: missing PacketEnvelope debt ledger: {LEDGER_PATH}", file=sys.stderr)
        print("The gate fails closed without its ledger.", file=sys.stderr)
        return 1

    sdk_root = _provision_sdk(repo_root)

    with tempfile.TemporaryDirectory() as tmp:
        findings_path = Path(tmp) / "packet-envelope-findings.json"
        scan_cmd = [
            sys.executable,
            "-m",
            "l9_ci",
            "baseline",
            "scan-packet-envelope",
            "--repository-root",
            str(repo_root),
            "--exclude-dir",
            ".l9/runtime",
            "--output",
            str(findings_path),
        ]
        for declaration in DECLARATION_PATHS:
            scan_cmd.extend(["--declaration-path", declaration])
        scan = _run(scan_cmd, cwd=str(sdk_root))
        if scan.returncode != 0:
            print("error: SDK PacketEnvelope scan failed", file=sys.stderr)
            return scan.returncode or 1

        compare_cmd = [
            sys.executable,
            "-m",
            "l9_ci",
            "baseline",
            "compare-scan",
            "--gate",
            GATE_LABEL,
            "--findings",
            str(findings_path),
            "--ledger",
            str(ledger),
        ]
        compare = _run(compare_cmd, cwd=str(sdk_root), capture_output=True)
        sys.stdout.write(compare.stdout.decode())
        sys.stderr.write(compare.stderr.decode())
        if compare.returncode != 0:
            try:
                verdict = json.loads(compare.stdout.decode())
                total = len(verdict.get("violations", []))
                print(
                    f"PacketEnvelope gate BLOCKED: {total} ledger violation(s). "
                    "See .l9/baselines/packet-envelope.yml and "
                    "docs/CI_PIPELINE.md (baseline ratchet).",
                    file=sys.stderr,
                )
            except (json.JSONDecodeError, ValueError):
                pass
        return compare.returncode


if __name__ == "__main__":
    raise SystemExit(main())
