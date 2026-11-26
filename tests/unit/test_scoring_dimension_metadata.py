"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, domain-spec, admission]
owner: engine-team
status: active
--- /L9_META ---

``ScoringDimensionSpec.metadata`` is reachable from a pack (ceg-preference-attention-unreachable).

Before this field, pydantic dropped ``metadata:`` from pack YAML, so a
``preference_attention`` dimension's ``success_value`` (and, since Stage 2
item 7, its ``outcome_community_property``) could be set only by code that
built the dimension directly. These tests go end to end from YAML on the
synthetic ``orbital-logistics`` pack (``tests/fixtures/domains``): the declared
metadata survives the loader and drives the compiled scoring Cypher, a
misspelled or unread key is refused at load, and a ``preference_attention``
dimension without ``success_value`` is withheld at admission with a named
defect (``preference_attention_metadata_required_enabled``).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engine.config.loader import DomainNotFoundError, DomainPackLoader, DomainSpecError, admission_defects
from engine.config.schema import DomainSpec, ScoringDimensionMetadata
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

_DOMAIN = "orbital-logistics"
_DIRECTION = "window_to_depot"
_DEFECT = (
    "undeclared scoring metadata: Dimension 'docking_history': preference_attention requires "
    "metadata.success_value (the outcome value the domain counts as success); the engine has no default"
)


def _attention(metadata: dict[str, Any] | None) -> dict[str, Any]:
    dim: dict[str, Any] = {
        "name": "docking_history",
        "source": "computed",
        "computation": "preference_attention",
        "candidateprop": "depot_cluster",
        "weightkey": "w_history",
        "defaultweight": 0.1,
    }
    if metadata is not None:
        dim["metadata"] = metadata
    return dim


_DECLARED = {"success_value": "docked", "outcome_community_property": "docking_cluster"}


_QE_STEP = {"name": "query_depot", "pattern": "(candidate:OrbitalDepot)-[:RESUPPLIED_BY]->(qe:OrbitalDepot)"}


def _raw(extra_dimension: dict[str, Any]) -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["scoring"]["dimensions"].append(extra_dimension)
    # preference_attention matches its outcome sample from `qe`; strict scoring
    # validation (D3) refuses it unless a traversal step binds `qe`.
    raw["traversal"] = {"steps": [_QE_STEP]}
    return raw


def _pack_root(tmp_path: Path, extra_dimension: dict[str, Any]) -> DomainPackLoader:
    """A pack root holding orbital-logistics plus ``extra_dimension``, written as YAML."""
    pack = tmp_path / _DOMAIN
    pack.mkdir()
    (pack / "spec.yaml").write_text(yaml.safe_dump(_raw(extra_dimension), sort_keys=False), encoding="utf-8")
    return DomainPackLoader(config_path=str(tmp_path))


@pytest.fixture(autouse=True)
def _default_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "unvalidated_domain_packs_enabled", False)
    monkeypatch.setattr(settings, "domain_pack_receipts_enabled", False)


# ── A pack declaring the metadata loads, keeps it, and compiles with it ──────


def test_declared_metadata_survives_the_loader(tmp_path: Path) -> None:
    spec = _pack_root(tmp_path, _attention(_DECLARED)).load_domain(_DOMAIN)
    dim = next(d for d in spec.scoring.dimensions if d.name == "docking_history")
    assert isinstance(dim.metadata, ScoringDimensionMetadata)
    assert dim.metadata.success_value == "docked"
    assert dim.metadata.outcome_community_property == "docking_cluster"


def test_declared_metadata_drives_the_compiled_scoring_cypher(tmp_path: Path) -> None:
    spec = _pack_root(tmp_path, _attention(_DECLARED)).load_domain(_DOMAIN)
    assembler = ScoringAssembler(spec)
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {})
    # The declared success value is bound as the dimension's parameter ...
    assert "= $pref_success_docking_history" in clause
    assert assembler.last_query_params["pref_success_docking_history"] == "docked"
    # ... and the declared outcome community property is compared with the
    # candidate's declared community property, over the declared outcome label.
    assert ".docking_cluster = candidate.depot_cluster" in clause
    assert ":DockingResult)" in clause
    assert "AS docking_history" in clause


def test_unset_metadata_keys_keep_the_compilers_defaults(tmp_path: Path) -> None:
    spec = _pack_root(tmp_path, _attention({"success_value": "docked"})).load_domain(_DOMAIN)
    clause, _ = ScoringAssembler(spec).assemble_scoring_clause(_DIRECTION, {})
    # outcome_community_property defaults to the candidate's community property,
    # success_property to `outcome_type`, the outcome node to the declared label.
    assert ".depot_cluster = candidate.depot_cluster" in clause
    assert ".outcome_type = $pref_success_docking_history" in clause
    assert ":DockingResult)" in clause


def test_admission_reports_no_defect_for_a_declared_success_value() -> None:
    spec = DomainSpec.model_validate(_raw(_attention(_DECLARED)))
    assert admission_defects(spec) == []


# ── The same pack without the required metadata is refused, by name ──────────


@pytest.mark.parametrize("metadata", [None, {"outcome_community_property": "docking_cluster"}], ids=["none", "partial"])
def test_missing_success_value_is_withheld_with_the_named_defect(
    tmp_path: Path, metadata: dict[str, Any] | None
) -> None:
    loader = _pack_root(tmp_path, _attention(metadata))
    with pytest.raises(DomainNotFoundError) as refused:
        loader.load_domain(_DOMAIN)
    assert _DEFECT in str(refused.value)
    assert loader.list_domains() == []


def test_missing_success_value_is_one_defect_with_the_scoring_check_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scoring compiler's refusal of the same dimension is not reported a second time."""
    monkeypatch.setattr(settings, "scoring_admission_check_enabled", True)
    spec = DomainSpec.model_validate(_raw(_attention(None)))
    assert admission_defects(spec) == [_DEFECT]


def test_flag_off_admits_the_pack_and_the_compiler_refuses_at_match_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-21 rollback: the pack is admitted as before; the compiler still has no default."""
    monkeypatch.setattr(settings, "preference_attention_metadata_required_enabled", False)
    spec = _pack_root(tmp_path, _attention(None)).load_domain(_DOMAIN)
    assembler = ScoringAssembler(spec)
    with pytest.raises(ValueError, match="requires metadata.success_value"):
        assembler.assemble_scoring_clause(_DIRECTION, {})


# ── A misspelled or unread key fails validation instead of being dropped ─────


@pytest.mark.parametrize(
    ("dimension", "message"),
    [
        (_attention({"sucess_value": "docked"}), "sucess_value"),
        (_attention({"success_value": 1}), "success_value"),
        (
            {
                "name": "ports_again",
                "source": "candidateproperty",
                "candidateprop": "docking_ports",
                "computation": "lognormalized",
                "weightkey": "w_ports_again",
                "defaultweight": 0.05,
                "metadata": {"success_value": "docked"},
            },
            "metadata key(s) success_value are not read by computation 'lognormalized'",
        ),
        (
            {**_attention({"success_value": "docked"}), "computation": "community_bridge"},
            "metadata key(s) success_value are not read by computation 'community_bridge'",
        ),
    ],
    ids=["misspelled", "mistyped", "unread-by-lognormalized", "unread-by-community_bridge"],
)
def test_metadata_the_compilers_do_not_read_is_refused(dimension: dict[str, Any], message: str, tmp_path: Path) -> None:
    raw = _raw(dimension)
    loader = _pack_root(tmp_path, dimension)
    with pytest.raises(ValidationError, match=re.escape(message)):
        DomainSpec.model_validate(raw)
    with pytest.raises(DomainSpecError, match=re.escape(message)):
        loader.load_domain(_DOMAIN)


# ── community_bridge reads metadata.community_properties from the pack ───────


def test_community_bridge_reads_its_declared_community_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = {
        "name": "cluster_bridge",
        "source": "computed",
        "computation": "community_bridge",
        "weightkey": "w_bridge",
        "defaultweight": 0.1,
        "metadata": {"community_properties": ["orbit_cluster", "supply_cluster"]},
    }
    spec = DomainSpec.model_validate(_raw(bridge))
    # Declared through metadata, so no undeclared-structural-property defect.
    assert not [d for d in admission_defects(spec) if "cluster_bridge" in d]
    expression = ScoringAssembler(spec)._compile_dimension(spec.scoring.dimensions[-1])
    assert "candidate.orbit_cluster = $query_orbit_cluster" in expression
    assert "candidate.supply_cluster = $query_supply_cluster" in expression
