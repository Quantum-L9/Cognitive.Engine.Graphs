"""
Auditor wiring contract.

Every registered auditor advertises two documentation pointers:

  * ``contract_file``    — the contract governing the pattern it enforces
  * ``remediation_doc``  — the runbook for fixing its findings

Both are surfaced to humans and agents via ``audit_dispatch.py --list`` and
the PR comment. If either path does not resolve, an agent chasing a finding
lands on a 404 and has no way to fix it. These tests make the pointers
load-bearing rather than decorative.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.auditors.base import get_all_auditors  # noqa: E402

AUDITORS_DIR = REPO_ROOT / "tools" / "auditors"


def _load_all_auditors() -> list:
    """Import every auditor module so the registry is fully populated.

    Mirrors the auto-discovery in audit_dispatch.py; without it the registry
    only contains whatever a previous import happened to pull in.
    """
    for py_file in sorted(AUDITORS_DIR.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue
        importlib.import_module(f"tools.auditors.{py_file.stem}")
    return get_all_auditors()


AUDITORS = _load_all_auditors()
AUDITOR_IDS = [a.name for a in AUDITORS]


def test_auditors_are_registered() -> None:
    """Guard against the registry silently emptying out."""
    assert AUDITORS, "No auditors registered — auto-discovery is broken"


@pytest.mark.parametrize("auditor", AUDITORS, ids=AUDITOR_IDS)
def test_contract_file_resolves(auditor) -> None:
    """Each auditor's contract_file must point at a real contract."""
    target = REPO_ROOT / auditor.contract_file
    assert target.is_file(), (
        f"Auditor '{auditor.name}' references contract '{auditor.contract_file}' which does not exist. "
        f"Findings from this auditor would link to a missing document."
    )


@pytest.mark.parametrize("auditor", AUDITORS, ids=AUDITOR_IDS)
def test_contract_file_is_under_contracts_dir(auditor) -> None:
    """Contract pointers must resolve inside docs/contracts/, not anywhere else."""
    assert auditor.contract_file.startswith("docs/contracts/"), (
        f"Auditor '{auditor.name}' contract_file '{auditor.contract_file}' is outside docs/contracts/"
    )


@pytest.mark.parametrize("auditor", AUDITORS, ids=AUDITOR_IDS)
def test_remediation_doc_resolves(auditor) -> None:
    """Each auditor must ship a runbook telling an agent how to fix its findings."""
    target = REPO_ROOT / auditor.remediation_doc
    assert target.is_file(), (
        f"Auditor '{auditor.name}' has no remediation runbook at '{auditor.remediation_doc}'. "
        f"Create it so findings are actionable."
    )


@pytest.mark.parametrize("auditor", AUDITORS, ids=AUDITOR_IDS)
def test_remediation_doc_references_its_auditor(auditor) -> None:
    """The runbook must name the auditor it belongs to, so pairings can't drift."""
    body = (REPO_ROOT / auditor.remediation_doc).read_text(encoding="utf-8")
    assert auditor.name in body, (
        f"Remediation doc '{auditor.remediation_doc}' never mentions auditor '{auditor.name}' — "
        f"likely copied from another auditor or renamed without updating."
    )


def test_no_orphan_remediation_docs() -> None:
    """Every runbook maps to a registered auditor (audit_finding.md is the exception)."""
    remediation_dir = AUDITORS_DIR / "remediation"
    assert remediation_dir.is_dir(), "tools/auditors/remediation/ is missing"

    # audit_finding.md covers tools/audit_engine.py rule findings, which are not
    # emitted through the BaseAuditor registry.
    allowed_non_auditor = {"audit_finding"}
    registered = {a.name for a in AUDITORS} | allowed_non_auditor

    orphans = [p.name for p in sorted(remediation_dir.glob("*.md")) if p.stem not in registered]
    assert not orphans, f"Remediation runbooks with no matching auditor: {orphans}"
