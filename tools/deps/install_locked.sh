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
# Install a dependency profile from uv.lock, the repository's single lock
# (SonarCloud githubactions:S8541/S8544, OpenSSF Scorecard Pinned-Dependencies).
#
#   bash tools/deps/install_locked.sh ci       # [dependency-groups] ci only
#   bash tools/deps/install_locked.sh full     # project + every group (with the Gate SDK)
#   bash tools/deps/install_locked.sh runtime  # project + licenses (with the Gate SDK)
#   bash tools/deps/install_locked.sh tools    # [dependency-groups] tools (pyyaml, yamllint)
#   bash tools/deps/install_locked.sh uv       # uv only (for `uv lock --check`)
#
# `uv sync --locked` installs exactly the versions uv.lock records and verifies
# every distribution against the sha256 the lock holds; it fails when uv.lock
# no longer matches pyproject.toml instead of re-resolving. The Gate SDK is a
# git source pinned to the commit uv.lock records (scripts/validate_sdk_pin.py);
# every other package installs from a locked wheel
# (tests/unit/test_uv_lock.py). uv itself is installed first from
# tools/deps/uv-bootstrap.txt with pip --require-hashes.
#
# Target environment: UV_PROJECT_ENVIRONMENT when set (Repo.mk: .venv; Docker:
# /opt/venv), otherwise the environment of $PYTHON (default: python on PATH),
# so a CI step's later `python -m pytest` sees what was installed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python}"

case "${1:-}" in
  ci) groups=(--only-group ci) ;;
  full) groups=(--all-groups) ;;
  runtime) groups=(--no-default-groups --group licenses) ;;
  tools) groups=(--only-group tools) ;;
  uv) groups=() ;;
  *) echo "usage: $0 ci|full|runtime|tools|uv" >&2; exit 2 ;;
esac

# Keep --require-hashes on this command line. Scorecard's pin probe does not
# follow a bash array, so a split `pip install "${args[@]}"` is an unpinned pipCommand.
"$PYTHON" -m pip install --disable-pip-version-check --quiet --no-deps --require-hashes -r "$ROOT/tools/deps/uv-bootstrap.txt"
[[ "$1" == "uv" ]] && exit 0

if [[ -z "${UV_PROJECT_ENVIRONMENT:-}" ]]; then
  UV_PROJECT_ENVIRONMENT="$("$PYTHON" -c 'import sys; print(sys.prefix)')"
fi
export UV_PROJECT_ENVIRONMENT
export UV_PYTHON_DOWNLOADS=never

# --inexact keeps what the target already holds (pip, uv itself, a runner's
# own packages); --locked refuses a uv.lock that pyproject.toml has outgrown.
"$PYTHON" -m uv sync --project "$ROOT" --locked --inexact --no-install-project "${groups[@]}"
