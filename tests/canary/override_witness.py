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

CI canary fixture (DO NOT MERGE): consumer attempt to waive a central rule.

The inline nosemgrep comment below is a consumer-side attempt to suppress the
central SDK rule l9.baseline.python.tls-verify-disabled. A correct central
gate refuses it or still reports the finding; coverage must not drop.

Inert: never imported, never collected by pytest.
"""

import requests


def override_attempt(url: str) -> int:
    # nosemgrep: l9.baseline.python.tls-verify-disabled
    response = requests.get(url, timeout=5, verify=False)  # noqa: S501
    return response.status_code
