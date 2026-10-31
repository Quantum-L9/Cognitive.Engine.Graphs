"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-packs, admission, serving-boundary]
owner: engine-team
status: active
--- /L9_META ---

Serving boundary for the retired legacy matching packs (operator decision
2026-10-25: domain-specific runtime architecture is retired).

The seven legacy packs below declare ``domain.activation: dormant`` and are
withheld by the generic loader admission policy (``withholding_reason``), not
by gate-compile defects. Compiler repairs to those gates must therefore never
widen what is served: under default settings each one is absent from
``list_domains`` and refused by ``load_domain``. The packs that were served
before the dormant marks keep being served.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import engine.config.loader as loader_module
from engine.config.schema import DomainActivation, DomainSpec
from engine.config.settings import Settings

DOMAINS = Path(__file__).resolve().parents[2] / "domains"

RETIRED_LEGACY_PACKS = (
    "aios-god-agent",
    "executive-assistant",
    "healthcare-referral",
    "legal-discovery",
    "repo-as-agent",
    "research-agent",
    "roofing-company",
)
# Served under default settings before the dormant marks; must stay served.
# freight-matching was withheld by the round-1 admission checks (it failed every
# match) and is served again since RU-037 bound its scoring (CEG-COR-011).
# mortgage-brokerage stays served, as on main (owner decision 2026-10-29): its
# DEC-15 dormant declaration is not applied until the owner enables withholding.
SERVED_PACKS = ("freight-matching", "mortgage-brokerage", "plasticos")
# Active packs that are not retired legacy packs and that their owner may declare
# dormant until an open product decision lands. Each names the decision. The pack
# must compile cleanly (admission_defects == []) so that, once declared dormant
# (re-created on a copy here), dormancy is the only reason it is not served, and
# the operator's dormant override serves it.
DORMANT_PENDING_DECISION = {
    # RU-038 / CEG-COR-012, DEC-15 "D now, then A": no gate construct can
    # enforce Lender-[:OPERATESIN]->State licensing or the borrower's
    # BLACKLISTEDLENDER exclusion, so the pack is dormant until one exists.
    "mortgage-brokerage": "DEC-15",
}


def _pre_repair_mortgage(raw: dict) -> None:
    """The mortgage pack as it was before RU-038: active, off-grammar traversal, query-anchored blacklist."""
    raw["domain"].pop("activation", None)
    raw["traversal"] = {
        "steps": [
            {"name": "lenderresolve", "pattern": "candidate-[:OFFEREDBY]->lender:Lender", "required": True},
            {
                "name": "lenderstatelicensing",
                "pattern": "lender-[:OPERATESIN]->st:State {statecode: $query.propertystate}",
                "required": True,
            },
        ]
    }
    raw["gates"].append(
        {
            "name": "blacklist_exclusion",
            "type": "exclusion",
            "edgetype": "BLACKLISTEDLENDER",
            "fromnode": "query",
            "tonode": "lender",
        }
    )


def _pre_repair_freight(raw: dict) -> None:
    """freight-matching as it was before RU-037: deadhead/rate dims declare no operands."""
    for dim in raw["scoring"]["dimensions"]:
        if dim["name"] in ("deadhead_distance", "rate_alignment"):
            for key in ("candidateprop", "candidateprop_lon", "queryprop", "queryprop_lon"):
                dim.pop(key, None)


# Unexecutable pack shapes the generic admission policy must withhold, for the
# named defects, until the operator switches the named checks off. ``None``
# means the repository pack as it is today; a mutation re-creates the defect a
# remediation unit repaired on a temporary copy, so the withholding mechanism
# stays covered after the repository pack is fixed.
WITHHELD_UNTIL_REPAIRED = {
    # RU-020: scoring references $lat/$lon/$target_price nothing binds (CEG-COR-011).
    # Repaired by RU-037; re-created on a copy.
    "freight-matching": (
        _pre_repair_freight,
        ("'lat'", "'lon'", "'target_price'"),
        ("scoring_admission_check_enabled", "admission_checks_full_match"),
    ),
    # RU-039: traversal step 'lenderresolve' is not in the assembler's grammar (CEG-COR-012).
    # RU-041: blacklist_exclusion declares fromnode/tonode the compiler cannot honour (CEG-COR-019).
    # Repaired by RU-038; re-created on a copy.
    "mortgage-brokerage": (
        _pre_repair_mortgage,
        ("lenderresolve", "blacklist_exclusion"),
        ("admission_checks_full_match", "exclusion_endpoint_validation"),
    ),
}


@pytest.fixture
def default_admission(monkeypatch: pytest.MonkeyPatch) -> loader_module.DomainPackLoader:
    """A loader over the repository's domains/ with both admission overrides at their defaults."""
    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", False)
    monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", False)
    # The admission hardening checks ship off (owner decision 2026-10-29) and this
    # module asserts what they withhold, so it pins every one that exists on; the
    # test_*_admission_default_off modules assert the shipped defaults.
    for flag in ("scoring_admission_check_enabled", "admission_checks_full_match", "exclusion_endpoint_validation"):
        if flag in Settings.model_fields:
            monkeypatch.setattr(loader_module.settings, flag, True)
    return loader_module.DomainPackLoader(config_path=str(DOMAINS))


def _withheld_root(pack: str, tmp_path: Path) -> Path:
    """The domains root holding the pack in WITHHELD_UNTIL_REPAIRED: the repository, or a mutated temporary copy."""
    mutation, _cited, _flags = WITHHELD_UNTIL_REPAIRED[pack]
    if mutation is None:
        return DOMAINS
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    mutation(raw)
    (tmp_path / pack).mkdir()
    (tmp_path / pack / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return tmp_path


def test_admission_overrides_default_off() -> None:
    fields = Settings.model_fields
    assert fields["dormant_domain_packs_enabled"].default is False
    assert fields["unvalidated_domain_packs_enabled"].default is False


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_declares_dormant(pack: str) -> None:
    """The pack file itself carries the generic dormant declaration; the engine names no domain."""
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert raw["domain"]["activation"] == DomainActivation.DORMANT.value


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_is_not_listed(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    assert pack not in default_admission.list_domains()


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_is_refused_as_dormant(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    with pytest.raises(loader_module.DomainNotFoundError, match=r"declared dormant by its pack"):
        default_admission.load_domain(pack)


@pytest.mark.parametrize("pack", SERVED_PACKS)
def test_previously_served_pack_is_unaffected(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    assert pack in default_admission.list_domains()
    assert default_admission.load_domain(pack).domain.id == pack


def test_served_set_is_exactly_the_previously_served_packs(default_admission: loader_module.DomainPackLoader) -> None:
    """No compiler or pack change may silently add a pack to the default serving surface."""
    assert set(default_admission.list_domains()) == set(SERVED_PACKS)


def _declared_dormant_root(pack: str, tmp_path: Path) -> Path:
    """A copy of the pack with the owner's dormant declaration applied (the withholding switch)."""
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    raw["domain"]["activation"] = DomainActivation.DORMANT.value
    (tmp_path / pack).mkdir()
    (tmp_path / pack / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("pack", sorted(DORMANT_PENDING_DECISION))
def test_pending_pack_is_served_until_its_owner_declares_it_dormant(
    pack: str, default_admission: loader_module.DomainPackLoader
) -> None:
    """Owner decision 2026-10-29: active in the repository, compiles cleanly, served by default."""
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert raw["domain"].get("activation", "active") == DomainActivation.ACTIVE.value
    assert loader_module.admission_defects(DomainSpec.model_validate(raw)) == []
    assert default_admission.load_domain(pack).domain.id == pack


@pytest.mark.parametrize("pack", sorted(DORMANT_PENDING_DECISION))
def test_pending_pack_is_withheld_only_because_it_is_dormant(
    pack: str, tmp_path: Path, default_admission: loader_module.DomainPackLoader
) -> None:
    """Once declared dormant by its owner: compiles cleanly, refused with the dormant reason."""
    loader = loader_module.DomainPackLoader(config_path=str(_declared_dormant_root(pack, tmp_path)))
    raw = yaml.safe_load((tmp_path / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert loader_module.admission_defects(DomainSpec.model_validate(raw)) == []
    assert pack not in loader.list_domains()
    with pytest.raises(loader_module.DomainNotFoundError, match=r"declared dormant by its pack"):
        loader.load_domain(pack)


@pytest.mark.parametrize("pack", sorted(DORMANT_PENDING_DECISION))
def test_pending_pack_is_served_by_the_dormant_override(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_admission: loader_module.DomainPackLoader
) -> None:
    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", True)
    loader = loader_module.DomainPackLoader(config_path=str(_declared_dormant_root(pack, tmp_path)))
    assert pack in loader.list_domains()
    assert loader.load_domain(pack).domain.id == pack


@pytest.mark.parametrize("pack", sorted(WITHHELD_UNTIL_REPAIRED))
def test_unexecutable_pack_is_withheld_for_its_defects(
    pack: str, tmp_path: Path, default_admission: loader_module.DomainPackLoader
) -> None:
    """Withheld for a named admission defect, not dormancy: the pack itself is active."""
    root = _withheld_root(pack, tmp_path)
    loader = loader_module.DomainPackLoader(config_path=str(root))
    raw = yaml.safe_load((root / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert raw["domain"].get("activation", "active") == "active"
    assert pack not in loader.list_domains()
    with pytest.raises(loader_module.DomainNotFoundError, match="admission defect") as excinfo:
        loader.load_domain(pack)
    _mutation, cited, _flags = WITHHELD_UNTIL_REPAIRED[pack]
    for fragment in cited:
        assert fragment in str(excinfo.value)


@pytest.mark.parametrize("pack", sorted(WITHHELD_UNTIL_REPAIRED))
def test_withheld_pack_is_served_again_when_its_checks_are_switched_off(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_admission: loader_module.DomainPackLoader
) -> None:
    loader = loader_module.DomainPackLoader(config_path=str(_withheld_root(pack, tmp_path)))
    _mutation, _cited, flags = WITHHELD_UNTIL_REPAIRED[pack]
    for flag in flags:
        monkeypatch.setattr(loader_module.settings, flag, False)
    assert loader.load_domain(pack).domain.id == pack


# RG-050 (RU-038): every pack the loader serves — and every pack the dormant
# override would serve — completes handle_match with a recording driver for each
# declared direction, sending exactly one match query that binds every parameter.
_SAMPLE_BY_TYPE = {
    "string": "x",
    "text": "x",
    "int": 1,
    "float": 1.0,
    "bool": True,
    "datetime": "2026-11-01T08:00:00Z",
    "date": "2026-11-01",
}


def _queryschema_only_query(spec: DomainSpec) -> dict[str, object]:
    """One type-appropriate value per queryschema field; enums take the first declared value."""
    declared_enums = {
        prop.name: prop.values
        for node in spec.ontology.nodes
        for prop in node.properties
        if getattr(prop, "values", None)
    }
    query: dict[str, object] = {}
    for field in spec.queryschema.fields:
        kind = field.type.value
        if kind == "enum":
            query[field.name] = (declared_enums.get(field.name) or ["x"])[0]
        else:
            query[field.name] = _SAMPLE_BY_TYPE.get(kind, "x")
    return query


def _servable_packs() -> list[str]:
    return sorted({*SERVED_PACKS, *DORMANT_PENDING_DECISION})


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", _servable_packs())
async def test_every_servable_pack_completes_handle_match_for_each_direction(
    pack: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re
    from unittest.mock import AsyncMock, MagicMock

    from engine.handlers import handle_match, init_dependencies
    from engine.state import get_state

    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", True)
    monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", False)
    loader = loader_module.DomainPackLoader(config_path=str(DOMAINS))
    spec = loader.load_domain(pack)
    calls: list[tuple[str, dict[str, object]]] = []

    async def _record(*, cypher: str, parameters: dict[str, object], database: str) -> list[dict[str, object]]:
        calls.append((cypher, parameters))
        return []

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_record)
    for direction in spec.queryschema.matchdirections:
        calls.clear()
        get_state().reset()
        init_dependencies(driver, loader)
        try:
            await handle_match(pack, {"query": _queryschema_only_query(spec), "match_direction": direction, "top_n": 5})
        finally:
            get_state().reset()
        match_calls = [c for c in calls if "ORDER BY score DESC" in c[0] and "LIMIT $top_n" in c[0]]
        assert len(match_calls) == 1, (direction, calls)
        cypher, parameters = match_calls[0]
        refs = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher))
        assert sorted(refs - set(parameters)) == [], (direction, cypher)


@pytest.mark.asyncio
async def test_mortgage_lender_traversal_is_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """RU-038: the lender step is grammar-conforming; licensing and blacklist are absent (not enforced).

    Owner decision 2026-10-29: the pack is still served, as on main, until the owner
    declares it dormant."""
    from engine.traversal.assembler import TraversalAssembler

    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", True)
    spec = loader_module.DomainPackLoader(config_path=str(DOMAINS)).load_domain("mortgage-brokerage")
    clauses = TraversalAssembler(spec).assemble_traversal("borrowertoproduct")
    assert "MATCH (candidate:LoanProduct)-[:OFFEREDBY]->(lender:Lender)" in clauses
    assert not any("OPERATESIN" in c for c in clauses)
    assert not any(g.edgetype == "BLACKLISTEDLENDER" for g in spec.gates)
    assert spec.domain.activation is DomainActivation.ACTIVE
