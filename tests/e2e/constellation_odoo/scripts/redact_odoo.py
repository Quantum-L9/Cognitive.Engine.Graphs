#!/usr/bin/env python3
"""Keep every secret from the rail's env file out of the evidence bundle.

  redact_odoo.py <src|-> <dst> <env-file>   copy with secrets replaced
  redact_odoo.py --scan <bundle-dir> <env-file>   report files still leaking

Unlike a fixed variable list, EVERY value in the env file is treated as secret
(the file holds nothing else), and JSON keyring values are expanded. Key IDs
(gate-e2e, eie-e2e, ceg-e2e, odoo-e2e) are never values, so they survive — they
are what makes the signing evidence meaningful. Exit 1 from --scan on a leak.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# YAML-safe: a leading "*" makes an unquoted value parse as an alias, which
# broke check-yaml on redacted `docker compose config` output.
PLACEHOLDER = "REDACTED-BY-L9-E2E"
MIN_SECRET_LEN = 12


def secrets_from(env_file: str) -> list[str]:
    found: set[str] = set()
    for line in Path(env_file).read_text().splitlines():
        _key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        value = value.strip().strip("'")
        if value.startswith("{"):
            try:
                found.update(str(v) for v in json.loads(value).values())
            except json.JSONDecodeError:
                pass
        found.add(value)
    return sorted((s for s in found if len(s) >= MIN_SECRET_LEN), key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, PLACEHOLDER)
    return text


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--scan":
        secrets = secrets_from(argv[3])
        leaks = []
        for path in sorted(Path(argv[2]).rglob("*")):
            if path.is_file():
                text = path.read_text(errors="replace")
                if any(s in text for s in secrets):
                    leaks.append(str(path))
        print(json.dumps({"secret_scan": "FAIL" if leaks else "PASS", "leaking_files": leaks}))
        return 1 if leaks else 0
    if len(argv) == 4:
        secrets = secrets_from(argv[3])
        text = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text(errors="replace")
        Path(argv[2]).write_text(redact_text(text, secrets))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
