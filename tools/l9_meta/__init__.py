"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

Config-driven L9_META header pipeline (Contract C-018).

Public surface:

- `model.MetaRecord` — the schema v2 record
- `formats.for_path()` — dispatch a path to its format module
- `config.load()` / `resolve.resolve()` — path -> record via `l9-meta.yaml`
- `discover.tracked_files()` — eligible files from the git index
- `cli.main()` — `check | apply | sync | report | init`
"""

from __future__ import annotations

from tools.l9_meta.model import SCHEMA_VERSION, MetaRecord

__all__ = ["SCHEMA_VERSION", "MetaRecord"]
