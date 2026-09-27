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
#
# SDK participation proof (L9-PARTICIPATION-01; run_odoo_e2e.sh with
# L9E2E_SDK_PARTICIPATION=1) adds three declared, labelled layers:
#
#   sdk-overlay   every node image + the Gate_SDK checkout's HEAD commit, from
#                 its GitHub archive (so provenance reads the commit)
#                 -> io.l9.e2e.sdk_overlay=<sha>
#   sdk-adoption  EIE / CEG + ../patches/<node>-sdk-adoption.diff: their own
#                 registration loops removed, SDK participation on
#                 -> io.l9.e2e.sdk_adoption=<diff sha256>
#   sdk-node      Gate_SDK examples/minimal_node at HEAD -> l9e2e/sdk-node:local
#
# Each is reported by the verdict as a deviation from the pinned release set.
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

SDK_REPO="${WORKSPACE}/Gate_SDK"
PATCHES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../patches" && pwd)"

sdk_sha() {
  [[ -z "$(git -C "$SDK_REPO" status --porcelain --untracked-files=no)" ]] \
    || { echo "FATAL: Gate_SDK checkout has uncommitted changes" >&2; return 1; }
  git -C "$SDK_REPO" rev-parse --verify HEAD
}

build_sdk_overlay() {
  local sha url
  sha="$(sdk_sha)"
  url="https://github.com/Quantum-L9/Gate_SDK/archive/${sha}.tar.gz"
  for n in gate eie ceg odoo; do
    local img="l9e2e/${n}:local" user df
    user="$(docker image inspect "$img" --format '{{.Config.User}}')"
    df="${OUT}/${n}-sdk-overlay.Dockerfile"
    {
      echo "FROM ${img}"
      echo "USER root"
      if [[ "$n" == "odoo" ]]; then
        echo "RUN pip3 install --no-cache-dir --no-deps --force-reinstall --break-system-packages \"constellation-node-sdk @ ${url}\""
      else
        echo "RUN pip install --no-cache-dir --no-deps --force-reinstall \"constellation-node-sdk @ ${url}\""
      fi
      [[ -n "$user" ]] && echo "USER ${user}"
    } > "$df"
    echo "${n}: SDK overlay ${sha:0:12}"
    docker buildx build --network host --progress plain \
      --label "io.l9.e2e.sdk_overlay=${sha}" \
      --build-arg "HTTPS_PROXY=${PROXY}" --build-arg "https_proxy=${PROXY}" \
      -f "$df" -t "$img" --load "${OUT}" \
      2>&1 | tee "${OUT}/${n}-sdk-overlay.build.log" | tail -3
  done
}

build_sdk_adoption() {
  for pair in "eie:Enrichment.Inference.Engine" "ceg:Cognitive.Engine.Graphs"; do
    local n="${pair%%:*}" repo="${WORKSPACE}/${pair#*:}" diff ctx digest
    diff="${PATCHES}/${n}-sdk-adoption.diff"
    ctx="$(mktemp -d -p "${OUT}" "${n}-adoption.XXXX")"
    digest="$(sha256sum "$diff" | cut -c1-16)"
    # Patch exactly the files the diff names, taken from the committed HEAD
    # the base image was built from.
    for f in $(grep -E '^\+\+\+ b/' "$diff" | sed -E 's#^\+\+\+ b/##'); do
      mkdir -p "${ctx}/$(dirname "$f")"
      git -C "$repo" show "HEAD:${f}" > "${ctx}/${f}"
    done
    patch -d "$ctx" -p1 --no-backup-if-mismatch < "$diff"
    {
      echo "FROM l9e2e/${n}:local"
      for f in $(grep -E '^\+\+\+ b/' "$diff" | sed -E 's#^\+\+\+ b/##'); do
        echo "COPY --chmod=0644 ${f} /app/${f}"
      done
    } > "${ctx}/Dockerfile"
    echo "${n}: SDK adoption ${digest}"
    docker buildx build --progress plain \
      --label "io.l9.e2e.sdk_adoption=${n}-sdk-adoption.diff@sha256:${digest}" \
      -f "${ctx}/Dockerfile" -t "l9e2e/${n}:local" --load "$ctx" \
      2>&1 | tee "${OUT}/${n}-sdk-adoption.build.log" | tail -3
  done
}

build_sdk_node() {
  local sha url ctx
  sha="$(sdk_sha)"
  url="https://github.com/Quantum-L9/Gate_SDK/archive/${sha}.tar.gz"
  ctx="$(mktemp -d -p "${OUT}" "sdk-node.XXXX")"
  mkdir -p "${ctx}/examples/minimal_node"
  for f in app.py handlers.py spec.yaml; do
    git -C "$SDK_REPO" show "HEAD:examples/minimal_node/${f}" > "${ctx}/examples/minimal_node/${f}"
  done
  cat > "${ctx}/Dockerfile.src" <<DOCKERFILE
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONPATH=/srv PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "constellation-node-sdk @ ${url}"
COPY examples/ /srv/examples/
DOCKERFILE
  python3 "${GATE_RAIL}/scripts/ca_inject.py" --src "${ctx}/Dockerfile.src" \
    --out "${ctx}/Dockerfile" | tee "${OUT}/sdk-node.cainject.txt"
  docker buildx build --network host --progress plain \
    --label "org.opencontainers.image.revision=${sha}" \
    --label "io.l9.e2e.node=sdk-minimal-node" \
    --build-context "l9ca=${GATE_RAIL}/ca" \
    --build-arg "HTTPS_PROXY=${PROXY}" --build-arg "https_proxy=${PROXY}" \
    --build-arg "NO_PROXY=${NOPROXY}" --build-arg "no_proxy=${NOPROXY}" \
    -f "${ctx}/Dockerfile" -t l9e2e/sdk-node:local --load "$ctx" \
    2>&1 | tee "${OUT}/sdk-node.build.log" | tail -3
}

case "${1:-all}" in
  odoo) build_odoo ;;
  eie-accommodated) build_eie_accommodated ;;
  sdk-overlay) build_sdk_overlay ;;
  sdk-adoption) build_sdk_adoption ;;
  sdk-node) build_sdk_node ;;
  sdk-participation) build_sdk_overlay; build_sdk_adoption; build_sdk_node ;;
  all) build_odoo; build_eie_accommodated ;;
  *) echo "usage: $0 [odoo|eie-accommodated|sdk-overlay|sdk-adoption|sdk-node|sdk-participation|all]" >&2; exit 2 ;;
esac
