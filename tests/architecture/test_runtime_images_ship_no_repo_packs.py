"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, architecture, docker, prod, domain-packs]
owner: engine-team
status: active
--- /L9_META ---

tests/architecture/test_runtime_images_ship_no_repo_packs.py
Runtime images contain no repository domain packs (#400 A1-A3, A6).

Operator decision 2026-11-03: the packs kept in this repository are design and
test material. They must not be a runtime dependency. Decision D1 (2026-11-13):
a tenant spec reaches production through an operator-owned pack root mounted
read-only, the directory tools/publish_domain_pack.py publishes into.

Static guards over Dockerfile, Dockerfile.prod, chassis/Dockerfile.chassis and
docker-compose.prod.yml: no image copies a pack directory out of the build
context or offers a build-time pack selection, and production
compose serves only a required, read-only, operator-supplied mount. They keep
that shape from drifting; they do not prove a deployed container's behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
PACK_MOUNT_TARGET = "/app/domains"

# Any COPY/ADD instruction that names a pack directory as source or target, or
# the retired build-time pack selection. `RUN mkdir -p /app/domains` (the empty
# mount point) is not a COPY and stays allowed.
_PACK_COPY = re.compile(
    r"^[ \t]*(COPY|ADD)\b[^\n]*(domains|design_packs|DOMAIN_PACKS)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


@pytest.fixture(scope="module")
def api_service() -> dict[str, Any]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service: dict[str, Any] = compose["services"]["api"]
    return service


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.prod", "chassis/Dockerfile.chassis"])
def test_image_copies_no_pack_directory(dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    match = _PACK_COPY.search(text)
    assert match is None, f"{dockerfile} copies a domain pack directory into the image: {match.group(0).strip()!r}"
    for build_arg in ("DOMAIN_PACKS_SRC", "DOMAINS_DIR"):
        assert build_arg not in text, f"{dockerfile} must not offer a build-time pack selection ({build_arg})"


def test_pack_copy_pattern_catches_the_shapes_it_replaced() -> None:
    # Negative witnesses: the three lines this guard was written to keep out.
    for line in (
        "COPY domains/ ./domains/\n",
        "COPY --from=builder /build/domains ./domains\n",
        "COPY ${DOMAIN_PACKS_SRC}/ /app/domains/\n",
        "COPY tests/fixtures/design_packs/ /app/domains/\n",
    ):
        assert _PACK_COPY.search(line), line
    assert _PACK_COPY.search("COPY engine/ ./engine/\n") is None


def test_prod_serves_only_the_mounted_pack_root(api_service: dict[str, Any]) -> None:
    assert api_service["environment"]["DOMAINS_ROOT"] == PACK_MOUNT_TARGET


def test_prod_pack_root_is_a_required_read_only_operator_mount(api_service: dict[str, Any]) -> None:
    mounts = [m for m in api_service.get("volumes", []) if isinstance(m, str) and f":{PACK_MOUNT_TARGET}" in m]
    assert len(mounts) == 1, f"expected exactly one mount at {PACK_MOUNT_TARGET}, found {mounts}"
    mount = mounts[0]
    assert mount.endswith(f":{PACK_MOUNT_TARGET}:ro"), "the pack root must be mounted read-only"
    source = mount[: -len(f":{PACK_MOUNT_TARGET}:ro")]
    assert re.fullmatch(r"\$\{CEG_DOMAINS_DIR:\?[^}]+\}", source), (
        "the pack root must come from CEG_DOMAINS_DIR with no default, so compose refuses to start "
        f"without an operator-supplied directory; found {source!r}"
    )
