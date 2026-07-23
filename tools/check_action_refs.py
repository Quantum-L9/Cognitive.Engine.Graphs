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
"""Guard: reject malformed GitHub Actions references in workflow files.

Detects the "Frankenref" corruption pattern where a 40-hex commit SHA and a
version tag are concatenated into a single unusable ref:

    uses: owner/action@<40-hex-sha>v<version>     # BROKEN — resolves to nothing

This pattern appeared when an automated edit merged `@<sha>` pinning with
`@v<version>` tagging in one string. GitHub cannot resolve such a ref, so
every affected job dies at workflow-parse/setup time.

Also enforces the repository pinning convention for third-party actions:

    uses: owner/action@<40-hex-sha> # v<version>  # OK — SHA-pinned, tag in comment
    uses: actions/checkout@v6                     # OK — first-party (github.com/actions, github.com/github)

Exit codes: 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import pathlib
import re
import sys

WORKFLOW_DIR = pathlib.Path(".github/workflows")

# The corruption pattern: 40-hex SHA immediately followed by a version string.
FRANKENREF = re.compile(r"uses:\s*[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}v[\d]")

# Any `uses:` of an external action (owner/repo@ref), for convention checks.
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<action>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<ref>\S+)")

# First-party namespaces that may use tag refs directly.
FIRST_PARTY = ("actions/", "github/")

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    violations: list[str] = []
    warnings: list[str] = []

    if not WORKFLOW_DIR.is_dir():
        print(f"no {WORKFLOW_DIR} directory — nothing to check")
        return 0

    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.split("#", 1)[0]  # ignore pure comments
            if FRANKENREF.search(stripped):
                violations.append(f"{path}:{lineno}: malformed SHA+tag Frankenref: {line.strip()}")
                continue
            m = USES_LINE.match(stripped)
            if not m:
                continue
            action, ref = m.group("action"), m.group("ref")
            if action.startswith(FIRST_PARTY):
                continue
            if ref.startswith("./"):
                continue  # local composite action
            if not FULL_SHA.match(ref):
                warnings.append(
                    f"{path}:{lineno}: third-party action not SHA-pinned: "
                    f"{action}@{ref} (convention: @<40-hex-sha> # v<version>)"
                )

    for w in warnings:
        print(f"WARNING: {w}")
    if violations:
        print(f"\n{len(violations)} malformed action reference(s) found:")
        for v in violations:
            print(f"ERROR: {v}")
        return 1
    print(f"action-ref check passed ({len(warnings)} pin-convention warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
