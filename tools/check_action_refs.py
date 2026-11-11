#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [ci]
# tags: [ci, security, supply-chain, actions]
# owner: platform
# status: active
# --- /L9_META ---
"""Guard: require immutable GitHub Actions references in workflow files.

Detects the "Frankenref" corruption pattern where a 40-hex commit SHA and a
version tag are concatenated into a single unusable ref:

    uses: owner/action@<40-hex-sha>v<version>     # BROKEN — resolves to nothing

This pattern appeared when an automated edit merged `@<sha>` pinning with
`@v<version>` tagging in one string. GitHub cannot resolve such a ref, so
every affected job dies at workflow-parse/setup time.

Also enforces immutable SHA pinning for every external action, including
first-party ``actions/*`` and ``github/*`` actions:

    uses: owner/action@<40-hex-sha> # v<version>  # OK — SHA-pinned, tag in comment
    uses: ./path/to/action                         # OK — local composite action

Exit codes: 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPOSITORY_ROOT / ".github" / "workflows"

# The corruption pattern: 40-hex SHA immediately followed by a version string.
FRANKENREF = re.compile(r"uses:\s*[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}v[\d]")

# Any `uses:` line, including local composite actions and reusable workflows.
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<reference>\S+)")

# Remote action or reusable-workflow reference: owner/repo[/path]@ref.
EXTERNAL_ACTION = re.compile(r"^(?P<action>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<ref>[^\s#]+)$")

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# The one non-SHA reference: Quantum-L9/l9-ci-core's consumer toolchain installer
# on its v2 channel. Core forbids consumers choosing a Core revision
# (release-plane.yaml, docs/consumer-lint-test.md); a SHA pin would be that.
CORE_INSTALLER = "Quantum-L9/l9-ci-core/.github/actions/install-consumer-ci@v2"


def main() -> int:
    violations: list[str] = []

    if not WORKFLOW_DIR.is_dir():
        print(f"ERROR: required workflow directory is missing: {WORKFLOW_DIR}")
        return 1

    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.split("#", 1)[0]  # ignore pure comments
            if FRANKENREF.search(stripped):
                violations.append(f"{path}:{lineno}: malformed SHA+tag Frankenref: {line.strip()}")
                continue
            m = USES_LINE.match(stripped)
            if not m:
                continue
            reference = m.group("reference")
            if reference.startswith("./"):
                continue  # local composite action
            action_ref = EXTERNAL_ACTION.match(reference)
            if not action_ref:
                violations.append(f"{path}:{lineno}: invalid non-local action reference: {line.strip()}")
                continue
            action, ref = action_ref.group("action"), action_ref.group("ref")
            if f"{action}@{ref}" == CORE_INSTALLER:
                continue
            if not FULL_SHA.match(ref):
                violations.append(
                    f"{path}:{lineno}: external action must be SHA-pinned: "
                    f"{action}@{ref} (convention: @<40-hex-sha> # v<version>)"
                )

    if violations:
        print(f"\n{len(violations)} invalid action reference(s) found:")
        for v in violations:
            print(f"ERROR: {v}")
        return 1
    print("action-ref check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
