"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [ci, canary, semgrep, fixtures]
owner: platform
status: active
--- /L9_META ---

CI canary fixture (DO NOT MERGE): central rule l9.baseline.python.tls-verify-disabled.

Inert: never imported, never collected by pytest.
"""

import requests


def known_bad(url: str) -> int:
    # Known-bad: TLS verification disabled. Central rule must report this line.
    return requests.get(url, timeout=5, verify=False).status_code  # noqa: S501


def known_good(url: str) -> int:
    # Known-good: verification left on. Central rule must not report this line.
    return requests.get(url, timeout=5).status_code
