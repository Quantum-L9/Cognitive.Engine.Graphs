"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, domain-spec, schema]
owner: engine-team
status: active
--- /L9_META ---

Strict scoring schema validation (CEG scoring audit L2, operator decision D3 option A).

With ``scoring_spec_strict_validation_enabled`` on (default), each structurally
invalid scoring declaration raises a pydantic ``ValidationError`` when the
``DomainSpec`` loads; with it off the same declaration is admitted as on main.
Every committed design pack still loads with the flag on, and
``tools/validate_domain.py --dry-run`` reports a refused pack without serving it.
``cypher_number`` (the numeric-literal guard) refuses non-finite values.
Built on the synthetic ``orbital-logistics`` pack (``tests/fixtures/domains``).
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.utils.security import cypher_number
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT
from tools import validate_domain

pytestmark = pytest.mark.unit

_DOMAIN = "orbital-logistics"
_FLAG = "scoring_spec_strict_validation_enabled"


def _raw() -> dict[str, Any]:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8"))
    return copy.deepcopy(raw)


def _attention_dimension() -> dict[str, Any]:
    return {
        "name": "attention",
        "source": "computed",
        "computation": "preference_attention",
        "candidateprop": "community_id",
        "weightkey": "w_attention",
        "defaultweight": 0.05,
        "metadata": {"success_value": "docked", "outcome_relation": "DOCKED_AT"},
    }


def _set_first(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        raw["scoring"]["dimensions"][0][key] = value

    return mutate


def _duplicate_name(raw: dict[str, Any]) -> None:
    raw["scoring"]["dimensions"][1]["name"] = raw["scoring"]["dimensions"][0]["name"]


def _sample_size_auto(raw: dict[str, Any]) -> None:
    raw["scoring"]["preference_sample_size"] = "auto"


def _constraint_on_undeclared(raw: dict[str, Any]) -> None:
    raw["decision_arbitration"] = {"constraints": [{"dimension": "not_a_dimension", "threshold": 0.5}]}


def _attention_without_qe(raw: dict[str, Any]) -> None:
    raw["scoring"]["dimensions"].append(_attention_dimension())


# (case id, mutation, message fragment the refusal names)
REFUSALS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("defaultweight-nan", _set_first("defaultweight", math.nan), "defaultweight must be finite and >= 0"),
    ("defaultweight-inf", _set_first("defaultweight", math.inf), "defaultweight must be finite and >= 0"),
    ("defaultweight-negative", _set_first("defaultweight", -0.1), "defaultweight must be finite and >= 0"),
    ("defaultwhennull-nan", _set_first("defaultwhennull", math.nan), "defaultwhennull must be finite"),
    ("defaultwhennull-neg-inf", _set_first("defaultwhennull", -math.inf), "defaultwhennull must be finite"),
    ("duplicate-dimension-name", _duplicate_name, "Duplicate scoring dimension name(s): ['depot_reliability']"),
    ("unknown-dimension-key", _set_first("weight_key", "w_typo"), "unknown key(s) weight_key"),
    ("aggregation-multiplicative", _set_first("aggregation", "multiplicative"), "aggregation 'multiplicative'"),
    ("preference-sample-size-auto", _sample_size_auto, "preference_sample_size must be an integer"),
    (
        "arbitration-undeclared-dimension",
        _constraint_on_undeclared,
        "undeclared scoring dimension(s): ['not_a_dimension']",
    ),
    ("preference-attention-without-qe", _attention_without_qe, "binds a `qe` node"),
]


def test_flag_defaults_on() -> None:
    assert type(settings).model_fields[_FLAG].default is True


def test_baseline_pack_loads() -> None:
    assert DomainSpec.model_validate(_raw()).domain.id == _DOMAIN


@pytest.mark.parametrize(("case", "mutate", "message"), REFUSALS, ids=[case for case, _m, _msg in REFUSALS])
def test_flag_on_refuses_at_domain_spec_load(case: str, mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    raw = _raw()
    mutate(raw)
    with pytest.raises(ValidationError) as excinfo:
        DomainSpec.model_validate(raw)
    assert message in str(excinfo.value), case


# On main an infinite defaultweight was already refused, by the per-direction
# weight ceiling (W1-01) rather than by this check; flag off keeps that verdict.
_REFUSED_ON_MAIN = {"defaultweight-inf": "exceeding 1.0"}


@pytest.mark.parametrize(("case", "mutate", "message"), REFUSALS, ids=[case for case, _m, _msg in REFUSALS])
def test_flag_off_admits_as_on_main(
    monkeypatch: pytest.MonkeyPatch, case: str, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    monkeypatch.setattr(settings, _FLAG, False)
    raw = _raw()
    mutate(raw)
    if case in _REFUSED_ON_MAIN:
        with pytest.raises(ValidationError) as excinfo:
            DomainSpec.model_validate(raw)
        assert _REFUSED_ON_MAIN[case] in str(excinfo.value)
        assert message not in str(excinfo.value)
        return
    spec = DomainSpec.model_validate(raw)
    assert spec.domain.id == _DOMAIN, case


def test_flag_off_ignores_the_unknown_key_as_main_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, _FLAG, False)
    raw = _raw()
    raw["scoring"]["dimensions"][0]["weight_key"] = "w_typo"
    dim = DomainSpec.model_validate(raw).scoring.dimensions[0]
    assert not hasattr(dim, "weight_key")
    assert dim.weightkey == "w_reliability"


def test_zero_defaultweight_is_admitted() -> None:
    raw = _raw()
    raw["scoring"]["dimensions"][0]["defaultweight"] = 0.0
    assert DomainSpec.model_validate(raw).scoring.dimensions[0].defaultweight == 0.0


@pytest.mark.parametrize("size", [0, -3, "16", True, 16.0])
def test_preference_sample_size_must_be_a_positive_integer(size: Any) -> None:
    raw = _raw()
    raw["scoring"]["preference_sample_size"] = size
    with pytest.raises(ValidationError, match="preference_sample_size"):
        DomainSpec.model_validate(raw)


def test_preference_attention_is_admitted_when_traversal_binds_qe() -> None:
    raw = _raw()
    raw["scoring"]["dimensions"].append(_attention_dimension())
    raw["traversal"] = {
        "steps": [{"name": "query_entity", "pattern": "(qe:DockingWindow)-[:REQUESTS]->(candidate:OrbitalDepot)"}]
    }
    spec = DomainSpec.model_validate(raw)
    assert [d.name for d in spec.scoring.dimensions][-1] == "attention"


def test_a_qe_prefixed_alias_does_not_count_as_binding_qe() -> None:
    raw = _raw()
    raw["scoring"]["dimensions"].append(_attention_dimension())
    raw["traversal"] = {
        "steps": [{"name": "query_entity", "pattern": "(qex:DockingWindow)-[:REQUESTS]->(candidate:OrbitalDepot)"}]
    }
    with pytest.raises(ValidationError, match="binds a `qe` node"):
        DomainSpec.model_validate(raw)


def test_a_qe_step_scoped_to_another_direction_does_not_bind_qe() -> None:
    """TraversalAssembler filters steps by matchdirections; the check does the same, per direction."""
    raw = _raw()
    raw["scoring"]["dimensions"].append(_attention_dimension())
    raw["traversal"] = {
        "steps": [
            {
                "name": "query_entity",
                "pattern": "(qe:DockingWindow)-[:REQUESTS]->(candidate:OrbitalDepot)",
                "matchdirections": ["some_other_direction"],
            }
        ]
    }
    with pytest.raises(ValidationError, match="applicable to direction 'window_to_depot' binds a `qe` node"):
        DomainSpec.model_validate(raw)


def test_a_qe_step_scoped_to_the_scoring_direction_binds_qe() -> None:
    raw = _raw()
    raw["scoring"]["dimensions"].append(_attention_dimension())
    raw["traversal"] = {
        "steps": [
            {
                "name": "query_entity",
                "pattern": "(qe:DockingWindow)-[:REQUESTS]->(candidate:OrbitalDepot)",
                "matchdirections": ["window_to_depot"],
            }
        ]
    }
    assert DomainSpec.model_validate(raw).traversal is not None


def test_attention_scoped_away_from_every_declared_direction_is_not_checked() -> None:
    raw = _raw()
    raw["scoring"]["dimensions"].append({**_attention_dimension(), "matchdirections": ["some_other_direction"]})
    assert DomainSpec.model_validate(raw).scoring.dimensions[-1].name == "attention"


def test_arbitration_constraint_on_a_declared_dimension_is_admitted() -> None:
    raw = _raw()
    raw["decision_arbitration"] = {"constraints": [{"dimension": "depot_reliability", "threshold": 0.5}]}
    assert DomainSpec.model_validate(raw).decision_arbitration.constraints[0].dimension == "depot_reliability"


def test_admission_flag_default_is_unchanged() -> None:
    """D3 preserves the 2026-10-29 owner decision: admission withholding stays off."""
    assert type(settings).model_fields["scoring_admission_check_enabled"].default is False


def _committed_packs() -> list[Path]:
    return sorted(DESIGN_PACKS_ROOT.glob("*/spec.yaml")) + sorted(SYNTHETIC_DOMAINS_ROOT.glob("*/spec.yaml"))


def test_committed_packs_exist() -> None:
    assert len(_committed_packs()) >= 10


@pytest.mark.parametrize("path", _committed_packs(), ids=lambda p: p.parent.name)
def test_every_committed_pack_loads_with_the_flag_on(path: Path) -> None:
    assert settings.scoring_spec_strict_validation_enabled is True
    spec = DomainSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert spec.domain.id == path.parent.name


# ── tools/validate_domain.py --dry-run ──────────────────────────────────────


def _write_pack(root: Path, name: str, raw: dict[str, Any]) -> Path:
    raw = copy.deepcopy(raw)
    raw["domain"]["id"] = name
    pack = root / name
    pack.mkdir(parents=True)
    path = pack / "spec.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_dry_run_reports_a_refused_pack_and_admits_a_valid_one(tmp_path: Path) -> None:
    refused = _raw()
    _duplicate_name(refused)
    _write_pack(tmp_path, "orbital-refused", refused)
    _write_pack(tmp_path, "orbital-valid", _raw())

    results, code = validate_domain.dry_run(tmp_path)

    assert code == 1
    by_pack = {Path(r["spec_path"]).parent.name: r for r in results}
    assert by_pack["orbital-refused"]["status"] == "REFUSED"
    assert "Duplicate scoring dimension name(s)" in by_pack["orbital-refused"]["message"]
    assert by_pack["orbital-valid"]["status"] == "PASS"


def test_dry_run_forces_the_flag_on_and_restores_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator running with the flag still off sees the verdict the deploy will give."""
    monkeypatch.setattr(settings, _FLAG, False)
    refused = _raw()
    _set_first("aggregation", "multiplicative")(refused)
    path = _write_pack(tmp_path, "orbital-multiplicative", refused)

    results, code = validate_domain.dry_run(path)

    assert code == 1
    assert results[0]["status"] == "REFUSED"
    assert settings.scoring_spec_strict_validation_enabled is False


def test_dry_run_serves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dry run parses specs only: no loader, no driver, no handler state is touched."""
    from engine.state import get_state

    before = get_state()._domain_loader
    refused = _raw()
    _sample_size_auto(refused)
    _write_pack(tmp_path, "orbital-auto", refused)
    validate_domain.dry_run(tmp_path)
    after = get_state()._domain_loader
    assert before is after
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["spec.yaml"]


def test_dry_run_on_the_committed_design_packs_refuses_none() -> None:
    results, code = validate_domain.dry_run(DESIGN_PACKS_ROOT)
    assert code == 0, [r for r in results if r["status"] != "PASS"]
    assert len(results) == len(sorted(DESIGN_PACKS_ROOT.glob("*/spec.yaml")))


def test_dry_run_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    refused = _raw()
    _set_first("defaultweight", -1.0)(refused)
    _write_pack(tmp_path, "orbital-negative", refused)
    with pytest.raises(SystemExit) as excinfo:
        validate_domain.main(["--dry-run", "--json", "--root", str(tmp_path), str(tmp_path)])
    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["results"][0]["status"] == "REFUSED"


def test_dry_run_with_no_spec_fails(tmp_path: Path) -> None:
    results, code = validate_domain.dry_run(tmp_path)
    assert code == 1
    assert "no spec.yaml" in results[0]["message"]


@pytest.mark.parametrize("size", [True, 16.0])
def test_coercible_sample_sizes_are_admitted_with_the_flag_off(monkeypatch: pytest.MonkeyPatch, size: Any) -> None:
    monkeypatch.setattr(settings, _FLAG, False)
    raw = _raw()
    raw["scoring"]["preference_sample_size"] = size
    assert DomainSpec.model_validate(raw).scoring.preference_sample_size == int(size)


def test_dry_run_cli_text_mode_on_an_empty_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        validate_domain.main(["--dry-run", "--root", str(tmp_path), str(tmp_path)])
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert f"[FAIL] {tmp_path}: no spec.yaml under {tmp_path}" in out
    assert "Result: PACK(S) REFUSED" in out


# ── cypher_number: non-finite scalars never reach Cypher (ND-PARAM) ─────────


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, "inf", "-inf", "nan", float("1e999")])
def test_cypher_number_refuses_non_finite(value: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        cypher_number(value)


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (-2, -2.0), ("0.25", 0.25), (1e308, 1e308)])
def test_cypher_number_accepts_finite(value: object, expected: float) -> None:
    assert cypher_number(value) == expected


def test_cypher_number_still_refuses_non_numeric() -> None:
    with pytest.raises(ValueError, match="numeric"):
        cypher_number("1); MATCH (n) DETACH DELETE n //")


@given(value=st.floats(allow_nan=False, allow_infinity=False))
@hypothesis_settings(max_examples=200)
def test_cypher_number_round_trips_every_finite_float(value: float) -> None:
    assert cypher_number(value) == value
    assert cypher_number(repr(value)) == value


@given(value=st.sampled_from([math.inf, -math.inf, math.nan]))
def test_cypher_number_refuses_every_non_finite_float(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        cypher_number(value)
