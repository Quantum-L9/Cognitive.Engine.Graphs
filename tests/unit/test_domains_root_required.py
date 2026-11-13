"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, config, domain-loader]
owner: engine-team
status: active
--- /L9_META ---

Issue #400 A4/A5: production never serves packs from an implicit or absent root,
and no engine or chassis reader names a pack directory of its own.

Every case builds its own ``Settings`` (DOMAINS_ROOT read or deleted from the
environment) and swaps it into ``engine.config.loader``, so whether the root is
"explicitly configured" is decided by that instance alone, never by what other
tests did to the process-wide singleton.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import engine.config.loader as loader_mod
from chassis.config import YAMLDomainLoader
from engine.config.loader import DomainPackLoader, DomainSpecError
from engine.config.settings import Settings
from engine.intake.vertical_discovery import discover_vertical_fields
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

SYNTHETIC_IDS = sorted(p.name for p in SYNTHETIC_DOMAINS_ROOT.iterdir() if (p / "spec.yaml").is_file())


def _settings(monkeypatch: pytest.MonkeyPatch, *, env: str, domains_root: str | None = None, **flags: Any) -> Settings:
    """Bind a fresh Settings into the loader; DOMAINS_ROOT set only when given."""
    monkeypatch.delenv("DOMAINS_ROOT", raising=False)
    monkeypatch.delenv("DOMAIN_SPECS_PATH", raising=False)
    if domains_root is not None:
        monkeypatch.setenv("DOMAINS_ROOT", domains_root)
    built = Settings(
        l9_env=env,
        neo4j_password="test-only-neo4j",
        api_secret_key="test-only-api-secret",
        domain_pack_receipts_enabled=False,
        **flags,
    )
    monkeypatch.setattr(loader_mod, "settings", built)
    return built


def _pack_root(tmp_path: Path) -> Path:
    root = tmp_path / "packs"
    shutil.copytree(SYNTHETIC_DOMAINS_ROOT, root)
    return root


# ── Explicitness ─────────────────────────────────────────


def test_synthetic_packs_present() -> None:
    """The positive cases below are only meaningful with packs to list."""
    assert SYNTHETIC_IDS


def test_environment_domains_root_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    built = _settings(monkeypatch, env="prod", domains_root=str(tmp_path))
    assert "domains_root" in built.model_fields_set
    assert built.domains_root == tmp_path


def test_unset_domains_root_is_not_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _settings(monkeypatch, env="prod")
    assert "domains_root" not in built.model_fields_set


# ── Production, flag on: refusals ────────────────────────


def test_prod_unset_root_refused_without_argument(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unset DOMAINS_ROOT never falls back to ./domains, even if it exists."""
    (tmp_path / "domains").mkdir()
    monkeypatch.chdir(tmp_path)
    _settings(monkeypatch, env="prod")
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT is not set"):
        DomainPackLoader()


def test_prod_unset_root_refused_in_boot_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Boot passes str(settings.domains_root); the relative default is not explicit."""
    (tmp_path / "domains").mkdir()
    monkeypatch.chdir(tmp_path)
    built = _settings(monkeypatch, env="prod")
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT is not set"):
        DomainPackLoader(config_path=str(built.domains_root))


def test_prod_relative_root_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pack_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    built = _settings(monkeypatch, env="prod", domains_root="packs")
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT must be an absolute path"):
        DomainPackLoader(config_path=str(built.domains_root))


def test_prod_missing_root_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    built = _settings(monkeypatch, env="prod", domains_root=str(missing))
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT does not exist"):
        DomainPackLoader(config_path=str(built.domains_root))


def test_prod_file_root_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a_file = tmp_path / "packs.yaml"
    a_file.write_text("not a directory\n", encoding="utf-8")
    built = _settings(monkeypatch, env="prod", domains_root=str(a_file))
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT is not a directory"):
        DomainPackLoader(config_path=str(built.domains_root))


def test_prod_unreadable_root_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Readability is os.access(R_OK|X_OK); a denial is refused.

    The denial is injected for this path only because the suite may run as root,
    for whom a chmod 000 directory stays readable and so proves nothing.
    """
    root = _pack_root(tmp_path)
    built = _settings(monkeypatch, env="prod", domains_root=str(root))
    real_access = os.access

    def deny_root(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
        if Path(path) == root:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(loader_mod.os, "access", deny_root)
    with pytest.raises(DomainSpecError, match=r"DOMAINS_ROOT is not readable"):
        DomainPackLoader(config_path=str(built.domains_root))


# ── Production, flag on: admitted roots ──────────────────


def test_prod_explicit_environment_root_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _pack_root(tmp_path)
    built = _settings(monkeypatch, env="prod", domains_root=str(root))
    assert DomainPackLoader(config_path=str(built.domains_root)).list_domains() == SYNTHETIC_IDS
    assert DomainPackLoader().list_domains() == SYNTHETIC_IDS


def test_prod_explicit_argument_root_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A caller-named absolute root is explicit even with DOMAINS_ROOT unset."""
    root = _pack_root(tmp_path)
    _settings(monkeypatch, env="prod")
    assert DomainPackLoader(config_path=str(root)).list_domains() == SYNTHETIC_IDS


# ── Flag off / non-production: behaviour unchanged ───────


@pytest.mark.parametrize(
    ("env", "flag"),
    [("prod", False), ("dev", True), ("dev", False), ("staging", True)],
)
def test_missing_root_lists_nothing_when_not_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: str, flag: bool
) -> None:
    built = _settings(monkeypatch, env=env, domains_root=str(tmp_path / "absent"), domains_root_required_in_prod=flag)
    assert DomainPackLoader(config_path=str(built.domains_root)).list_domains() == []


@pytest.mark.parametrize(("env", "flag"), [("prod", False), ("dev", True)])
def test_relative_default_still_served_when_not_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: str, flag: bool
) -> None:
    shutil.copytree(SYNTHETIC_DOMAINS_ROOT, tmp_path / "domains")
    monkeypatch.chdir(tmp_path)
    built = _settings(monkeypatch, env=env, domains_root_required_in_prod=flag)
    assert DomainPackLoader(config_path=str(built.domains_root)).list_domains() == SYNTHETIC_IDS
    assert DomainPackLoader().list_domains() == SYNTHETIC_IDS


@pytest.mark.parametrize("flag", [True, False])
def test_prod_file_root_lists_nothing_with_flag_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: bool
) -> None:
    a_file = tmp_path / "packs.yaml"
    a_file.write_text("not a directory\n", encoding="utf-8")
    built = _settings(monkeypatch, env="prod", domains_root=str(a_file), domains_root_required_in_prod=flag)
    if flag:
        with pytest.raises(DomainSpecError, match="DOMAINS_ROOT"):
            DomainPackLoader(config_path=str(built.domains_root))
    else:
        assert DomainPackLoader(config_path=str(built.domains_root)).list_domains() == []


# ── A5: no literal fallback; DOMAIN_SPECS_PATH stays a legacy alias ──────────


def test_legacy_alias_is_honoured_when_no_root_is_passed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DOMAIN_SPECS_PATH keeps the precedence it had before #400: after the argument, before Settings."""
    root = _pack_root(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    _settings(monkeypatch, env="dev", domains_root=str(empty))
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(root))
    assert DomainPackLoader().list_domains() == SYNTHETIC_IDS


def test_argument_beats_the_legacy_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _pack_root(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    _settings(monkeypatch, env="dev")
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(empty))
    assert DomainPackLoader(config_path=str(root)).list_domains() == SYNTHETIC_IDS
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(root))
    assert DomainPackLoader(config_path=str(empty)).list_domains() == []


def test_empty_legacy_alias_falls_through_to_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _pack_root(tmp_path)
    _settings(monkeypatch, env="dev", domains_root=str(root))
    monkeypatch.setenv("DOMAIN_SPECS_PATH", "")
    assert DomainPackLoader().list_domains() == SYNTHETIC_IDS


def test_prod_accepts_a_valid_legacy_alias_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A root supplied by the alias is explicitly configured, so A4 admits it when it is valid."""
    root = _pack_root(tmp_path)
    _settings(monkeypatch, env="prod", domains_root_required_in_prod=True)
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(root))
    assert DomainPackLoader().list_domains() == SYNTHETIC_IDS


@pytest.mark.parametrize("bad", ["missing", "relative"])
def test_prod_refuses_a_bad_legacy_alias_root_naming_the_alias(
    bad: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _settings(monkeypatch, env="prod", domains_root_required_in_prod=True)
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(tmp_path / "absent") if bad == "missing" else "packs")
    with pytest.raises(DomainSpecError, match="DOMAIN_SPECS_PATH"):
        DomainPackLoader()


def test_chassis_loader_needs_a_root_from_the_argument_or_the_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _pack_root(tmp_path)
    monkeypatch.delenv("DOMAIN_SPECS_PATH", raising=False)
    with pytest.raises(ValueError, match="pack root"):
        YAMLDomainLoader()
    assert YAMLDomainLoader(root).list_domains() == SYNTHETIC_IDS
    assert YAMLDomainLoader(str(root)).list_domains() == SYNTHETIC_IDS
    # The alias this loader read before #400 still supplies the root; the argument wins over it.
    monkeypatch.setenv("DOMAIN_SPECS_PATH", str(root))
    assert YAMLDomainLoader().list_domains() == SYNTHETIC_IDS
    empty = tmp_path / "empty"
    empty.mkdir()
    assert YAMLDomainLoader(empty).list_domains() == []


def test_vertical_discovery_reads_no_default_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without a spec or a named root no YAML is read, not even ./domains."""
    shutil.copytree(SYNTHETIC_DOMAINS_ROOT, tmp_path / "domains")
    monkeypatch.chdir(tmp_path)
    keywords = {"cheese": "cheese-aging"}
    implicit = discover_vertical_fields("We age cheese", vertical_keywords=keywords)
    assert implicit.vertical_name == "cheese-aging"
    assert implicit.vertical_fields == []
    assert implicit.discovery_suggested
    named = discover_vertical_fields("We age cheese", domains_dir=tmp_path / "domains", vertical_keywords=keywords)
    assert named.vertical_fields
    assert not named.discovery_suggested
