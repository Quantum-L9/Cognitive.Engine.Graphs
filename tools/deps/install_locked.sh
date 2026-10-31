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
# Install CI dependencies from a hash-locked file (SonarCloud githubactions:S8541/S8544).
#
#   bash tools/deps/install_locked.sh ci       # requirements-ci.lock
#   bash tools/deps/install_locked.sh full     # requirements-full.lock + constellation-node-sdk
#   bash tools/deps/install_locked.sh runtime  # requirements-runtime.lock + constellation-node-sdk
#
# Every package comes from the lock with its hash checked and only as a wheel, so
# no package setup script runs. The Gate SDK is a git dependency pip cannot hash:
# it is installed with --no-deps at the commit the lock records (its own
# dependencies are already in the lock). Regenerate locks with
# tools/deps/lock_requirements.py.
set -euo pipefail

case "${1:-}" in
  ci) lock=requirements-ci.lock ;;
  full) lock=requirements-full.lock ;;
  runtime) lock=requirements-runtime.lock ;;
  *) echo "usage: $0 ci|full|runtime" >&2; exit 2 ;;
esac

python -m pip install --only-binary :all: --require-hashes -r "$lock"

sdk=$(sed -n 's/^# sdk: //p' "$lock")
if [ -n "$sdk" ]; then
  python -m pip install --only-binary :all: --no-deps "$sdk"
fi
