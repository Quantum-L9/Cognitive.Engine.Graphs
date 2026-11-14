"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, security]
tags: [test, unit, supply-chain, dependencies, containers, ci]
owner: engine-team
status: active
--- /L9_META ---

Static regression coverage for committed dependency and container installation paths.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

pytestmark = pytest.mark.unit

_SHA = re.compile(r"^[0-9a-f]{40}$")
# The one non-SHA reference: Quantum-L9/l9-ci-core's consumer toolchain installer
# on its v2 channel. Core's contract (release-plane.yaml, docs/consumer-lint-test.md)
# forbids consumers choosing a Core revision; a SHA here would be exactly that.
CORE_INSTALLER = "Quantum-L9/l9-ci-core/.github/actions/install-consumer-ci@v2"
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def test_every_external_workflow_action_is_immutable() -> None:
    floating = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = re.match(r"\s*uses:\s+([^\s]+)", line)
            if not match:
                continue
            reference = match.group(1)
            if reference.startswith("./") or reference == CORE_INSTALLER:
                continue
            _, _, revision = reference.rpartition("@")
            if not _SHA.fullmatch(revision):
                floating.append(f"{workflow.relative_to(ROOT)}:{line_number}: {reference}")
    assert not floating, "floating external Actions are forbidden: " + "; ".join(floating)


def test_action_reference_checker_is_independent_of_calling_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed repository-owned script and interpreter
        [sys.executable, str(ROOT / "tools" / "check_action_refs.py")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_workflow_dependency_installs_use_locked_profiles() -> None:
    violations = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        direct_pip = re.search(r"(?m)^(?!\s*#).*\b(?:python\s+-m\s+)?pip\s+install\b", text)
        if direct_pip or "--install-types" in text:
            violations.append(str(workflow.relative_to(ROOT)))
    assert not violations, "workflow dependency resolution must use install_locked.sh: " + ", ".join(violations)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.prod", "chassis/Dockerfile.chassis"])
def test_application_docker_bases_are_digest_pinned(dockerfile: str) -> None:
    unpinned = []
    for line_number, line in enumerate((ROOT / dockerfile).read_text().splitlines(), start=1):
        if line.startswith("FROM "):
            image = line.split()[1]
            if not _IMAGE_DIGEST.search(image):
                unpinned.append(f"{dockerfile}:{line_number}: {image}")
    assert not unpinned, "application base images must use immutable digests: " + "; ".join(unpinned)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.prod", "chassis/Dockerfile.chassis"])
def test_application_docker_runtime_user_is_non_root(dockerfile: str) -> None:
    assert re.search(r"^USER l9$", (ROOT / dockerfile).read_text(), flags=re.MULTILINE)


def test_ci_service_images_are_digest_pinned() -> None:
    unpinned = []
    for workflow in (WORKFLOWS / "ci.yml", WORKFLOWS / "ci-quality.yml"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            if re.match(r"\s+image:\s+", line):
                image = line.split(":", 1)[1].strip()
                if not _IMAGE_DIGEST.search(image):
                    unpinned.append(f"{workflow.relative_to(ROOT)}:{line_number}: {image}")
    assert not unpinned, "CI service images must use immutable digests: " + "; ".join(unpinned)


def test_external_compose_images_are_digest_pinned() -> None:
    compose_files = [
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.prod.yml",
        ROOT / "telemetry" / "dashboards" / "dashboards-docker-compose.monitoring.yml",
        ROOT / "tests" / "e2e" / "constellation_odoo" / "compose.odoo.yml",
        ROOT / "tests" / "e2e" / "constellation_odoo" / "compose.sdk-participation.yml",
    ]
    unpinned = []
    for compose_file in compose_files:
        for line_number, line in enumerate(compose_file.read_text().splitlines(), start=1):
            if not re.match(r"\s+image:\s+", line):
                continue
            image = line.split(":", 1)[1].strip()
            if image.endswith(":local"):
                continue  # E2E images are built and provenance-checked within the test rail.
            if not _IMAGE_DIGEST.search(image):
                unpinned.append(f"{compose_file.relative_to(ROOT)}:{line_number}: {image}")
    assert not unpinned, "external Compose images must use immutable digests: " + "; ".join(unpinned)


def test_committed_runtime_installers_do_not_resolve_dependencies() -> None:
    paths = [
        ROOT / "Dockerfile",
        ROOT / "Dockerfile.prod",
        ROOT / "chassis" / "Dockerfile.chassis",
        ROOT / "scripts" / "scripts-setup.sh",
        ROOT / ".devcontainer" / "devcontainer.json",
    ]
    forbidden = ("pip install", "poetry install")
    offenders = [str(path.relative_to(ROOT)) for path in paths if any(token in path.read_text() for token in forbidden)]
    assert not offenders, "runtime and developer installers must delegate to locked profiles: " + ", ".join(offenders)


_APPLICATION_DOCKERFILES = ["Dockerfile", "Dockerfile.prod", "chassis/Dockerfile.chassis"]
_COPY = re.compile(r"^[ \t]*COPY[ \t]+(?P<args>[^\n]+)$", re.MULTILINE)


def _copy_sources(text: str) -> list[str]:
    """Build-context sources of every COPY that does not read from another stage."""
    sources: list[str] = []
    for match in _COPY.finditer(text):
        words = match.group("args").split()
        if any(word.startswith("--from=") for word in words):
            continue
        sources.extend(word for word in words[:-1] if not word.startswith("--"))
    return sources


@pytest.mark.parametrize("dockerfile", _APPLICATION_DOCKERFILES)
def test_application_docker_copies_only_tracked_sources(dockerfile: str) -> None:
    """A COPY of a path the repository no longer has fails only at build time.

    chassis/Dockerfile.chassis kept copying requirements-runtime.lock after
    uv.lock replaced it, so the chassis image no longer built.
    """
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    sources = [source for source in _copy_sources(text) if "$" not in source]
    assert sources, f"{dockerfile} copies nothing from the build context"
    missing = [source for source in sources if not (ROOT / source.rstrip("/")).exists()]
    assert not missing, f"{dockerfile} copies paths absent from the repository: {missing}"


@pytest.mark.parametrize("dockerfile", _APPLICATION_DOCKERFILES)
def test_application_docker_installs_from_uv_lock(dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    sources = _copy_sources(text)
    assert "uv.lock" in sources, f"{dockerfile} must install from uv.lock"
    assert "pyproject.toml" in sources, f"{dockerfile} must copy the manifest uv.lock is checked against"
    assert "install_locked.sh runtime" in text, f"{dockerfile} must use the locked runtime profile"
    stray = [source for source in sources if re.search(r"requirements|poetry\.lock|Pipfile", source)]
    assert not stray, f"{dockerfile} copies a retired dependency manifest: {stray}"


def test_copy_source_parser_reads_the_shapes_it_guards() -> None:
    text = (
        "COPY requirements-runtime.lock requirements-runtime.lock\n"
        "COPY tools/deps/install_locked.sh tools/deps/uv-bootstrap.txt tools/deps/\n"
        "COPY --from=deps /opt/venv /opt/venv\n"
        "COPY --chown=l9:l9 chassis/ /app/chassis/\n"
    )
    assert _copy_sources(text) == [
        "requirements-runtime.lock",
        "tools/deps/install_locked.sh",
        "tools/deps/uv-bootstrap.txt",
        "chassis/",
    ]


def test_locked_installer_is_rooted_and_hash_enforced() -> None:
    script = (ROOT / "tools" / "deps" / "install_locked.sh").read_text()
    assert 'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"' in script
    # uv itself installs by hash; every other package syncs from uv.lock, which
    # verifies each distribution's sha256 (tests/unit/test_uv_lock.py keeps every
    # registry package a hashed wheel). --locked refuses a stale lock.
    assert '--require-hashes -r "$ROOT/tools/deps/uv-bootstrap.txt"' in script
    assert "uv sync" in script
    assert "--locked" in script
    assert "--no-verify-hashes" not in script


def test_docker_context_excludes_sensitive_local_material() -> None:
    patterns = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {
        ".env*",
        "*.env",
        "credentials.json",
        "secrets.json",
        ".aws/",
        ".kube/",
        ".ssh/",
        ".netrc",
        ".npmrc",
        "secrets/",
        "*.pem",
        "*.key",
    } <= patterns


def test_devcontainer_uses_the_locked_full_profile() -> None:
    config = json.loads((ROOT / ".devcontainer" / "devcontainer.json").read_text())
    assert config["postCreateCommand"] == "bash tools/deps/install_locked.sh full && pre-commit install"


def test_pre_commit_sources_are_immutable_and_local_python_hooks_are_ambient() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text()
    revisions = re.findall(r"^\s+rev:\s+([^\s#]+)", config, flags=re.MULTILINE)
    assert revisions
    assert all(_SHA.fullmatch(revision) for revision in revisions)
    assert "additional_dependencies:" not in config
    assert "entry: mypy" in config
    assert "language: system" in config


def test_docker_run_and_e2e_sdk_installs_are_immutable_and_hash_checked() -> None:
    smoke = (ROOT / "tools" / "infra" / "precommit_smoke.sh").read_text()
    assert "curlimages/curl:latest@sha256:" in smoke

    e2e = (ROOT / "tests" / "e2e" / "constellation_odoo" / "scripts" / "build_images_odoo.sh").read_text()
    assert "dynamic E2E installs are forbidden" in e2e
    assert "archive --format=tar.gz" in e2e
    assert "FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159" in e2e
    assert "--require-hashes" in e2e
    assert "uv export" in e2e
    assert "--no-emit-package constellation-node-sdk" in e2e
