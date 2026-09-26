#!/usr/bin/env bash
# Build the images the Odoo rail needs, from each repository's OWN Dockerfile.
#
#   build_images_odoo.sh [odoo|eie-accommodated|all]
#
# gate / eie / ceg are built by Constellation.Gate's rail
# (constellation-gate/tests/e2e/docker/scripts/build_images.sh) — this script
# does not duplicate that. It reuses that rail's ca_inject.py, so every image
# differs from production by exactly one CA-trust layer per stage.
#
#   odoo              IB-Odoo_19/Dockerfile                   -> l9e2e/odoo:local
#   eie-accommodated  l9e2e/eie:local + one declared layer    -> l9e2e/eie:local
#
# eie-accommodated exists because EIE's image does not start at its current
# HEAD: the Dockerfile installs `.[dev]` from pyproject.toml, whose unpinned
# `sqlalchemy` resolves to 2.1.x, where greenlet is only pulled by the
# `asyncio` extra; app/services/pg_store.py imports sqlalchemy.ext.asyncio and
# the process exits with ImportError before registering with Gate.
# The accommodation installs the extra EIE's code already requires
# (`sqlalchemy[asyncio]`, the minimal change) ON TOP of the unmodified image,
# labels the image so the verdict can see it, and is reported as a deviation.
# It is removed the moment EIE fixes its dependency declaration.
set -Eeuo pipefail

WORKSPACE="${L9_E2E_WORKSPACE:-/home/user}"
GATE_RAIL="${WORKSPACE}/Constellation.Gate/constellation-gate/tests/e2e/docker"
OUT="${L9_E2E_BUILD_DIR:-/root/l9e2e/build}"
PROXY="${HTTPS_PROXY:-}"
NOPROXY="${NO_PROXY:-localhost,127.0.0.1}"
mkdir -p "$OUT"

build_odoo() {
  local ctx="${WORKSPACE}/IB-Odoo_19"
  python3 "${GATE_RAIL}/scripts/ca_inject.py" --src "${ctx}/Dockerfile" \
    --out "${OUT}/odoo.Dockerfile" | tee "${OUT}/odoo.cainject.txt"
  docker buildx build --network host --progress plain \
    --label "org.opencontainers.image.revision=$(git -C "$ctx" rev-parse --verify HEAD)" \
    --label "io.l9.e2e.node=odoo" \
    --build-context "l9ca=${GATE_RAIL}/ca" \
    --build-arg "HTTPS_PROXY=${PROXY}" --build-arg "https_proxy=${PROXY}" \
    --build-arg "NO_PROXY=${NOPROXY}" --build-arg "no_proxy=${NOPROXY}" \
    -f "${OUT}/odoo.Dockerfile" -t l9e2e/odoo:local --load "$ctx" \
    2>&1 | tee "${OUT}/odoo.build.log" | tail -5
}

build_eie_accommodated() {
  docker image inspect l9e2e/eie:local >/dev/null \
    || { echo "FATAL: build l9e2e/eie:local with the Gate rail first" >&2; return 1; }
  if docker image inspect l9e2e/eie:local \
       --format '{{index .Config.Labels "io.l9.e2e.accommodation"}}' | grep -q sqlalchemy; then
    echo "eie: accommodation already applied"; return 0
  fi
  # Probe first: only accommodate a defect that is actually present.
  if docker run --rm --entrypoint python l9e2e/eie:local -c \
       "import sqlalchemy.ext.asyncio" >/dev/null 2>&1; then
    echo "eie: sqlalchemy.ext.asyncio imports — no accommodation needed"; return 0
  fi
  local df="${OUT}/eie-accommodation.Dockerfile"
  cat > "$df" <<'EOF'
FROM l9e2e/eie:local
RUN pip install --no-cache-dir "sqlalchemy[asyncio]"
EOF
  docker buildx build --network host --progress plain \
    --label "io.l9.e2e.accommodation=sqlalchemy[asyncio]" \
    --build-arg "HTTPS_PROXY=${PROXY}" --build-arg "https_proxy=${PROXY}" \
    -f "$df" -t l9e2e/eie:local --load "${OUT}" \
    2>&1 | tee "${OUT}/eie-accommodation.build.log" | tail -5
}

case "${1:-all}" in
  odoo) build_odoo ;;
  eie-accommodated) build_eie_accommodated ;;
  all) build_odoo; build_eie_accommodated ;;
  *) echo "usage: $0 [odoo|eie-accommodated|all]" >&2; exit 2 ;;
esac
