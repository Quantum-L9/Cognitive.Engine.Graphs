"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api]
tags: [chassis, canary]
owner: platform-team
status: active
--- /L9_META ---

CANARY, DO NOT MERGE: a deliberate type error in chassis/.

Analyze (central Core) must fail on this file through Repo.mk repo-check's
`mypy engine/ chassis/ scripts/`. Imported by nothing.
"""

from __future__ import annotations


def port_number() -> int:
    return "8000"
