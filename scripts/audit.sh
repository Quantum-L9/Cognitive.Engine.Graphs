# --- L9_META ---
# l9_schema: 2
# origin: l9-template
# engine: graph
# layer: [scripts, audit]
# tags: [delivery]
# status: active
# --- /L9_META ---
#
!/usr/bin/env bash
set -euo pipefail
python tools/audit_engine.py
