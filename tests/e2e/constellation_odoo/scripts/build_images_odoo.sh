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
# eie-accommodated exists because EIE's image, built from its own Dockerfile at
# b583c9e, cannot serve `converge`:
#   1. `pip install ".[dev]"` resolves the unpinned `sqlalchemy` to 2.1.x, where
#      greenlet is only pulled by the `asyncio` extra; app/services/pg_store.py
#      imports sqlalchemy.ext.asyncio and the process exits before registering.
#   2. `alembic` is not a dependency, so the documented schema step
#      (`alembic upgrade head`) cannot run and every durable converge fails
#      with `relation "enrichment_results" does not exist`.
# The accommodation adds ONLY what is missing, ON TOP of the unmodified image,
# labels the image so the verdict can see it, and is reported as a deviation.
# It disappears on its own once EIE declares these dependencies.
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
  # Probe first: accommodate only a defect that is actually present in the
  # image as built from EIE's own Dockerfile.
  #   sqlalchemy[asyncio]  app/services/pg_store.py imports sqlalchemy.ext.asyncio
  #   alembic              migrations/ + alembic.ini ship in the image and
  #                        `alembic upgrade head` is EIE's documented schema
  #                        step, but alembic is not a declared dependency
  local missing=()
  docker run --rm --entrypoint python l9e2e/eie:local -c "import sqlalchemy.ext.asyncio" \
    >/dev/null 2>&1 || missing+=("sqlalchemy[asyncio]")
  docker run --rm --entrypoint python l9e2e/eie:local -c "import alembic" \
    >/dev/null 2>&1 || missing+=("alembic")
  if [[ ${#missing[@]} -eq 0 ]]; then
    echo "eie: no accommodation needed"; return 0
  fi
  local prior label
  prior="$(docker image inspect l9e2e/eie:local \
             --format '{{index .Config.Labels "io.l9.e2e.accommodation"}}' 2>/dev/null || true)"
  [[ "$prior" == "<no value>" ]] && prior=""
  label="$(IFS=,; echo "${prior:+${prior},}${missing[*]}")"
  local df="${OUT}/eie-accommodation.Dockerfile"
  {
    echo "FROM l9e2e/eie:local"
    printf 'RUN pip install --no-cache-dir'; printf ' "%s"' "${missing[@]}"; echo
  } > "$df"
  echo "eie: accommodating ${missing[*]}"
  docker buildx build --network host --progress plain \
    --label "io.l9.e2e.accommodation=${label}" \
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
