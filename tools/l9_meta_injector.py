#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [delivery, harness]
status: active
--- /L9_META ---

Compatibility shim. The implementation now lives in `tools/l9_meta/`.

This file used to hold a 245-entry `FILE_REGISTRY` that was the sole source of
header values. It drifted: 10 of those paths no longer existed, ~240 tracked
files were absent from it, and two formatters emitted only a comment prefix, so
`--apply` silently flattened real headers. Values now come from `l9-meta.yaml`
via path rules, and `l9-meta check` fails on drift.

Six docs reference this path, so it stays as an entry point:

    python tools/l9_meta_injector.py check
    python tools/l9_meta_injector.py apply --dry-run

Equivalent to `python -m tools.l9_meta.cli <same args>`.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.l9_meta.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
