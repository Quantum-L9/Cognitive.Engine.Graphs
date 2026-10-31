#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [tooling]
# tags: [tooling, ci, dependencies, supply-chain]
# owner: engine-team
# status: active
# --- /L9_META ---
#
# Install CI dependencies from a hash-locked file (SonarCloud githubactions:S8541/S8544,
# OpenSSF Scorecard Pinned-Dependencies).
#
#   bash tools/deps/install_locked.sh ci       # requirements-ci.lock
#   bash tools/deps/install_locked.sh full     # requirements-full.lock (with the Gate SDK)
#   bash tools/deps/install_locked.sh runtime  # requirements-runtime.lock (with the Gate SDK)
#   bash tools/deps/install_locked.sh tools    # requirements-tools.lock (pyyaml, yamllint)
#
# Every requirement, the Gate SDK included, is pinned with its sha256 and checked.
# Packages install only as wheels, so no setup script runs, with one named
# exception: the Gate SDK, which publishes no wheel and builds from its hashed
# source archive. Regenerate locks with tools/deps/lock_requirements.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "${1:-}" in
  ci) lock="$ROOT/requirements-ci.lock" ;;
  full) lock="$ROOT/requirements-full.lock" ;;
  runtime) lock="$ROOT/requirements-runtime.lock" ;;
  tools) lock="$ROOT/requirements-tools.lock" ;;
  *) echo "usage: $0 ci|full|runtime|tools" >&2; exit 2 ;;
esac

args=(--only-binary :all: --no-binary constellation-node-sdk --require-hashes -r "$lock")
if [[ -n "${PIP_PREFIX:-}" ]]; then
  args=(--prefix "$PIP_PREFIX" "${args[@]}")
fi
python -m pip install "${args[@]}"
