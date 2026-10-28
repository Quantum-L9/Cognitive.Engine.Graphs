"""CEG-009 — every domain under domains/ must be in the shape the loader reads.

``DomainPackLoader`` resolves ``domains/<domain_id>/spec.yaml``
(``loader.SPEC_FILENAME``). Nine specs used a flat
``<name>_domain_spec.yaml`` convention instead and were therefore unreachable:
they looked like available verticals in a directory listing, but a tenant id
matching one of them failed to resolve. Only the packs already in the directory
shape loaded by default.

Two of those nine were additionally invalid — ``executive-assistant`` declared
an edge to an undeclared ``Skill`` node, ``roofing-company`` one to ``ZipCode``
— and nothing caught it, because a file the loader never opens is never
validated either.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engine.config.loader import (
    SPEC_FILENAME,
    DomainNotFoundError,
    DomainPackLoader,
    admission_defects,
    withholding_reason,
)
from engine.config.schema import DomainActivation, DomainSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAINS_ROOT = REPO_ROOT / "domains"


def _domain_dirs() -> list[Path]:
    return sorted(p for p in DOMAINS_ROOT.iterdir() if p.is_dir())


def test_no_flat_domain_spec_files_remain() -> None:
    stragglers = sorted(p.name for p in DOMAINS_ROOT.glob("*_domain_spec.yaml"))
    assert stragglers == [], (
        f"{stragglers} use the flat shape the loader never reads. Move each to "
        f"domains/<domain_id>/{SPEC_FILENAME}, or delete it if superseded."
    )


def test_every_domain_directory_holds_a_spec() -> None:
    missing = [d.name for d in _domain_dirs() if not (d / SPEC_FILENAME).is_file()]
    assert missing == [], f"domain directories without {SPEC_FILENAME}: {missing}"


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_domain_spec_validates(domain_dir: Path) -> None:
    """A spec the loader can find is a spec that must survive validation."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    DomainSpec.model_validate(raw)


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_directory_name_matches_declared_domain_id(domain_dir: Path) -> None:
    """The loader resolves by domain id, so the directory name IS the lookup key."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert raw["domain"]["id"] == domain_dir.name


def test_loader_discovers_every_admitted_domain() -> None:
    """Everything on disk but not listed is withheld by the generic policy, for a stated reason."""
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    discovered = set(loader.list_domains())
    on_disk = {d.name for d in _domain_dirs()}
    assert discovered <= on_disk
    for name in sorted(on_disk - discovered):
        raw = yaml.safe_load((DOMAINS_ROOT / name / SPEC_FILENAME).read_text(encoding="utf-8"))
        assert withholding_reason(DomainSpec.model_validate(raw)) is not None, f"{name} withheld without a reason"


def test_every_discovered_domain_actually_loads() -> None:
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    for domain_id in sorted(loader.list_domains()):
        assert loader.load_domain(domain_id).domain.id == domain_id


# --------------------------------------------------------------------------
# Readable is not the same as correct.
#
# The defect classifier lives in the engine (``engine.config.loader.admission_defects``)
# and is the loader's own admission check, so these tests exercise the policy
# that runs in production rather than a test-only reimplementation.
# --------------------------------------------------------------------------


def _spec(domain_dir: Path) -> DomainSpec:
    return DomainSpec.model_validate(yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8")))


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_admitted_domain_gates_compile_to_executable_cypher(domain_dir: Path) -> None:
    """A pack the loader will serve must produce Cypher that can run.

    The gap this closes: validating a spec against DomainSpec proves it parses,
    not that its gates execute. Packs that passed Pydantic validation still
    compiled to `exists((candidate)-[:RELATES_TO]->(t))` or `$85.0`. The generic
    policy withholds exactly those packs; a pack it withholds is exempt here,
    because withholding is what the policy records.
    """
    spec = _spec(domain_dir)
    reason = withholding_reason(spec)
    if reason is not None:
        pytest.skip(reason)
    defects = admission_defects(spec)
    assert defects == [], f"{domain_dir.name} would serve unexecutable Cypher:\n  " + "\n  ".join(defects)


def test_withheld_packs_are_withheld_for_a_reason_that_still_holds() -> None:
    """Every pack on disk that the policy withholds has a live, generic reason.

    Either its gates still do not compile (``admission_defects``) or it declares
    itself dormant. No pack is withheld by name: there is no per-domain flag to
    forget a fixed pack in.
    """
    withheld = [d for d in _domain_dirs() if withholding_reason(_spec(d)) is not None]
    assert withheld, "no pack is withheld; this suite documents a policy that is no longer exercised"
    for domain_dir in withheld:
        spec = _spec(domain_dir)
        dormant = spec.domain.activation is DomainActivation.DORMANT
        assert dormant or admission_defects(spec), (
            f"{domain_dir.name} is withheld but compiles cleanly and is not dormant — the policy has no reason"
        )


def test_operator_overrides_are_generic_and_default_off() -> None:
    """The only ways to serve a withheld pack are two generic settings, both off by default."""
    from engine.config.settings import Settings

    defaults = Settings(neo4j_password="test-pw", api_secret_key="test-key")
    assert defaults.unvalidated_domain_packs_enabled is False
    assert defaults.dormant_domain_packs_enabled is False
    assert not hasattr(defaults, "idea_portfolio_enabled")


def test_loader_source_names_no_domain() -> None:
    """The admission policy must not special-case any domain id."""
    loader_file = inspect.getsourcefile(withholding_reason)
    assert loader_file is not None
    source = Path(loader_file).read_text(encoding="utf-8")
    assert "_DOMAIN_FEATURE_FLAGS" not in source
    for name in (d.name for d in _domain_dirs()):
        assert f'"{name}"' not in source, f"loader names domain {name!r}"


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("exists((candidate)-[:SERVES]->(t {route_code: $route_code}))", ["route_code"]),
        (
            "candidate.capacity >= $min_capacity AND candidate.tier IN [$tier_a, $tier_b]",
            ["min_capacity", "tier_a", "tier_b"],
        ),
        ("candidate.ids CONTAINS $x; RETURN $y", ["x", "y"]),
        ("candidate.utilization <= $85.0", ["85.0"]),
        ("candidate.active = $True)", ["True"]),
    ],
)
def test_parameter_capture_stops_at_closing_punctuation(cypher: str, expected: list[str]) -> None:
    """`$param}` in a traversal target filter is the identifier `param`, not a defect (PR #318 review)."""
    from engine.config.loader import _IDENTIFIER, _PARAM_REF

    captured = _PARAM_REF.findall(cypher)
    assert captured == expected
    # The scalar-queryparam defect the capture exists to report is still reported.
    assert [p for p in captured if not _IDENTIFIER.fullmatch(p)] == [
        p for p in expected if not p[0].isalpha() and p[0] != "_"
    ]


# --------------------------------------------------------------------------
# Identity contract (CEG-IDENT-01 / CEG-DBNAME-01, decided 2026-10-22):
# tenant id == domain.id == directory name == Neo4j database name.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_every_domain_directory_is_a_legal_tenant_id(domain_dir: Path) -> None:
    """A shipped pack's directory is what a tenant id and a database will be named."""
    from engine.utils.security import validate_tenant_id

    assert validate_tenant_id(domain_dir.name) == domain_dir.name


def test_loader_refuses_an_illegal_tenant_id_before_reading_disk(tmp_path: Path) -> None:
    """A pack whose directory could not name a database is never opened.

    The grammar runs before path resolution, so even a directory that exists
    with a valid spec inside is refused by id, not by content.
    """
    pack = tmp_path / "Acme_Roofing"
    pack.mkdir()
    (pack / SPEC_FILENAME).write_text((DOMAINS_ROOT / "plasticos" / SPEC_FILENAME).read_text(encoding="utf-8"))
    loader = DomainPackLoader(str(tmp_path))
    with pytest.raises(DomainNotFoundError, match="not a legal tenant id"):
        loader.load_domain("Acme_Roofing")
    assert loader.list_domains() == []


def test_customer_spec_carries_template_provenance_and_keeps_tenant_identity() -> None:
    """A customer cut from a vertical keeps its own id; the vertical is provenance only."""
    raw = yaml.safe_load((DOMAINS_ROOT / "plasticos" / SPEC_FILENAME).read_text(encoding="utf-8"))
    raw["domain"]["id"] = "acme.recycling"
    raw["domain"]["template"] = {"id": "plasticos", "version": raw["domain"]["version"], "source": "crm-scan"}
    spec = DomainSpec.model_validate(raw)
    assert spec.domain.id == "acme.recycling"
    assert spec.domain.template is not None
    assert (spec.domain.template.id, spec.domain.template.source) == ("plasticos", "crm-scan")
    # Shipped verticals are the templates themselves: no provenance block.
    assert DomainSpec.model_validate(_raw("plasticos")).domain.template is None


def _raw(domain_id: str) -> dict[str, object]:
    raw = yaml.safe_load((DOMAINS_ROOT / domain_id / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_template_provenance_requires_id_and_version() -> None:
    raw = _raw("plasticos")
    raw["domain"]["id"] = "acme.recycling"
    raw["domain"]["template"] = {"id": "plasticos"}
    with pytest.raises(ValidationError):
        DomainSpec.model_validate(raw)


# --------------------------------------------------------------------------
# Correctness remediation lane C (plan 0d84ec8): served-pack re-expressions.
# --------------------------------------------------------------------------


def _admitted_spec(domain_id: str) -> DomainSpec:
    spec = _spec(DOMAINS_ROOT / domain_id)
    assert withholding_reason(spec) is None, withholding_reason(spec)
    return spec


def _gate(spec: DomainSpec, name: str):  # type: ignore[no-untyped-def]
    return next(g for g in spec.gates if g.name == name)


def _three_valued_ge(candidate: bool | None, param: bool | None) -> bool | None:
    """Cypher `candidate >= $param` over booleans: NULL propagates, false < true."""
    if candidate is None or param is None:
        return None
    return candidate >= param


def _pass_wrapped(candidate: bool | None, param: bool | None) -> bool:
    """`(c IS NULL OR $p IS NULL OR (c >= $p))` — the PASS wrap of a threshold gate."""
    return candidate is None or param is None or bool(_three_valued_ge(candidate, param))


# RG-044 (RU-034 / CEG-COR-005): "if food grade is required, the facility must be
# certified" is an implication. Equality excluded every certified facility from
# a request that did not require certification.
_IMPLICATION_GATES = [
    (
        "plasticos",
        "food_grade_check",
        (
            "(candidate.food_grade_certified IS NULL OR $requires_food_grade IS NULL OR "
            "(candidate.food_grade_certified >= $requires_food_grade))"
        ),
    ),
]


@pytest.mark.parametrize(("domain_id", "gate_name", "expected"), _IMPLICATION_GATES, ids=lambda v: str(v)[:24])
def test_requirement_gates_compile_as_implications(domain_id: str, gate_name: str, expected: str) -> None:
    from engine.gates.compiler import GateCompiler

    spec = _admitted_spec(domain_id)
    gate = _gate(spec, gate_name)
    fragment = GateCompiler(spec).compile(gate)
    assert fragment == expected
    assert " = $" not in fragment
    direction_where = GateCompiler(spec).compile_all_gates("supply_opportunity_to_buyer_facility")
    assert expected in direction_where


def test_food_grade_truth_table_is_an_implication() -> None:
    """p NULL/false -> every facility passes; p true -> certified or NULL passes."""
    table = {p: {c: _pass_wrapped(c, p) for c in (None, False, True)} for p in (None, False, True)}
    assert table == {
        None: {None: True, False: True, True: True},
        False: {None: True, False: True, True: True},
        True: {None: True, False: False, True: True},
    }
    gate = _gate(_admitted_spec("plasticos"), "food_grade_check")
    assert (gate.type.value, gate.operator, gate.nullbehavior.value) == ("threshold", ">=", "pass")


_DIR1 = "supply_opportunity_to_buyer_facility"
_DIR2 = "buyer_demand_to_supply_opportunity"
_PARAM_IDENTIFIER_REF = r"\$([A-Za-z_][A-Za-z0-9_]*)"


async def _capture_match(domain_id: str, direction: str, query: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Drive the real handle_match against a recording driver; return the match Cypher and its parameters."""
    from unittest.mock import AsyncMock, MagicMock

    from engine.handlers import handle_match, init_dependencies
    from engine.state import get_state

    calls: list[tuple[str, dict[str, object]]] = []

    async def _record(*, cypher: str, parameters: dict[str, object], database: str) -> list[dict[str, object]]:
        calls.append((cypher, parameters))
        return []

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_record)
    get_state().reset()
    init_dependencies(driver, DomainPackLoader(str(DOMAINS_ROOT)))
    try:
        await handle_match(domain_id, {"query": query, "match_direction": direction, "top_n": 5})
    finally:
        get_state().reset()
    match_calls = [c for c in calls if "ORDER BY score DESC\nLIMIT $top_n" in c[0]]
    assert len(match_calls) == 1, calls
    return match_calls[0]


def _missing_params(cypher: str, parameters: dict[str, object]) -> list[str]:
    import re

    return sorted({p for p in re.findall(_PARAM_IDENTIFIER_REF, cypher) if p not in parameters})


_PLASTICOS_QUERIES = {
    _DIR1: {
        "polymer_type": "HDPE",
        "density": 0.95,
        "mfi": 5.0,
        "lat": 40.0,
        "lon": -80.0,
        "volume_tons": 10.0,
        "requires_food_grade": False,
        "target_price": 300.0,
    },
    _DIR2: {"polymer_type": "HDPE", "volume_tons": 10.0, "lat": 40.0, "lon": -80.0, "target_price": 300.0},
}


# RG-052 (RU-040 / CEG-COR-013): the polymer constraint is an EXISTS gate bound to
# $polymer_type, not a row-multiplying traversal MATCH that ignores the polymer.
@pytest.mark.parametrize(
    ("direction", "edge", "label"),
    [(_DIR1, "PROCESSES", "Facility"), (_DIR2, "FULFILLS_POLYMER", "SupplyOpportunity")],
)
def test_plasticos_polymer_constraint_is_an_exists_gate(direction: str, edge: str, label: str) -> None:
    from engine.gates.compiler import GateCompiler
    from engine.traversal.assembler import TraversalAssembler

    spec = _admitted_spec("plasticos")
    where = GateCompiler(spec).compile_all_gates(direction)
    exists = f"EXISTS {{ MATCH (candidate)-[:{edge}]->(t:PolymerFamily) WHERE t.code = $polymer_type }}"
    assert f"($polymer_type IS NOT NULL AND ({exists}))" in where
    traversal = "\n".join(TraversalAssembler(spec).assemble_traversal(direction))
    assert f"MATCH (candidate:{label})-[:{edge}]" not in traversal
    assert f"[:{edge}]" not in traversal
    assert admission_defects(spec) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [_DIR1, _DIR2])
async def test_plasticos_match_cypher_binds_every_parameter(direction: str) -> None:
    """Fake-driver capture: every $parameter the served match Cypher references is bound."""
    cypher, parameters = await _capture_match("plasticos", direction, dict(_PLASTICOS_QUERIES[direction]))
    assert _missing_params(cypher, parameters) == []
    assert "$polymer_type" in cypher


# RG-069 (RU-055 / CEG-COR-151): served scoring inputs are normalised into [0, 1]
# by the computation that reads them, so the dimension discriminates instead of
# saturating the clamp.
def _dimension_expression(domain_id: str, dim_name: str) -> str:
    from engine.scoring.assembler import ScoringAssembler

    spec = _spec(DOMAINS_ROOT / domain_id)
    dim = next(d for d in spec.scoring.dimensions if d.name == dim_name)
    return ScoringAssembler(spec)._compile_dimension(dim)


def _evaluate_numeric(expression: str, candidate: dict[str, float]) -> float:
    """Evaluate a closed arithmetic scoring expression for one candidate, clamped like the assembler.

    `coalesce(candidate.<prop>, <default>)` is substituted with the candidate's
    value; what remains must be numbers, + - * /, parentheses and log(), walked
    with a whitelist (never eval).
    """
    import ast
    import math
    import operator
    import re

    def _coalesce(match: re.Match[str]) -> str:
        return repr(float(candidate.get(match.group(1), float(match.group(2)))))

    python = re.sub(r"coalesce\(candidate\.([A-Za-z_][A-Za-z0-9_]*), ([0-9.]+)\)", _coalesce, expression)
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def _walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return binary[type(node.op)](_walk(node.left), _walk(node.right))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log":
            return math.log(_walk(node.args[0]))
        msg = f"not a closed numeric expression: {python}"
        raise AssertionError(msg)

    return min(max(_walk(ast.parse(python, mode="eval")), 0.0), 1.0)


@pytest.mark.parametrize(
    ("domain_id", "dim_name", "prop", "low", "high"),
    [
        ("plasticos", "rate_score", "rate_per_ton", 200.0, 400.0),
        ("freight-matching", "ontime_rating", "ontimeratingpct", 80.0, 95.0),
    ],
)
def test_served_scoring_inputs_discriminate_within_unit_range(
    domain_id: str, dim_name: str, prop: str, low: float, high: float
) -> None:
    expression = _dimension_expression(domain_id, dim_name)
    low_score = _evaluate_numeric(expression, {prop: low})
    high_score = _evaluate_numeric(expression, {prop: high})
    assert 0.0 < low_score < high_score < 1.0, (expression, low_score, high_score)


def test_plasticos_recency_uses_the_declared_180_day_horizon() -> None:
    expression = _dimension_expression("plasticos", "recency")
    assert "duration.inDays(candidate.last_transaction_date, datetime()).days / 180.0" in expression
    assert "/ 90.0" not in expression


# RG-070 (RU-056 / CEG-COR-152): the authoring template is a spec the loader
# would admit and every direction of which assembles. It stays non-served: it is
# a file under domains/, not domains/<id>/spec.yaml.
TEMPLATE_PATH = DOMAINS_ROOT / "_template.yaml"


def _template_spec() -> DomainSpec:
    raw = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
    raw["domain"]["id"] = "template-check"
    return DomainSpec.model_validate(raw)


def test_template_is_not_a_served_pack() -> None:
    assert TEMPLATE_PATH.is_file()
    assert "_template" not in DomainPackLoader(str(DOMAINS_ROOT)).list_domains()


def test_template_validates_and_assembles_for_every_direction() -> None:
    from engine.scoring.assembler import ScoringAssembler
    from engine.traversal.assembler import TraversalAssembler

    spec = _template_spec()
    assert admission_defects(spec) == []
    for direction in spec.queryschema.matchdirections:
        traversal = TraversalAssembler(spec).assemble_traversal(direction)
        assert all(not clause.startswith("OPTIONAL MATCH") for clause in traversal[-1:]), traversal
        ScoringAssembler(spec).assemble_scoring_clause(direction, {})
    exclusion = next(g for g in spec.gates if g.type.value == "exclusion")
    assert (exclusion.fromnode, exclusion.tonode) == (None, None)


# RG-051 (RU-039): a pack the loader admits is executable for every declared match
# direction — its traversal assembles and every $parameter in gates, traversal and
# scoring is one handle_match binds.
def _synthetic_raw() -> dict[str, object]:
    return yaml.safe_load(
        """
domain: {id: synthetic-check, name: Synthetic, version: 0.1.0}
ontology:
  nodes:
    - label: Widget
      managedby: sync
      candidate: true
      matchdirection: d1
      properties: [{name: widget_id, type: string, required: true}, {name: size, type: float}]
    - label: Ask
      managedby: api
      queryentity: true
      matchdirection: d1
      properties: [{name: ask_id, type: string, required: true}]
    - label: Maker
      managedby: sync
      properties: [{name: maker_id, type: string, required: true}]
  edges:
    - {type: MADE_BY, from: Widget, to: Maker, direction: DIRECTED, category: capability, managedby: sync}
matchentities:
  candidate: [{label: Widget, matchdirection: d1}]
  queryentity: [{label: Ask, matchdirection: d1}]
queryschema:
  matchdirections: [d1]
  fields: [{name: size, type: float}]
traversal:
  steps: []
gates:
  - {name: size_floor, type: threshold, candidateprop: size, queryparam: size, operator: '>=', nullbehavior: pass}
scoring:
  dimensions: []
"""
    )


def test_synthetic_baseline_pack_is_admitted() -> None:
    assert admission_defects(DomainSpec.model_validate(_synthetic_raw())) == []


def test_pack_with_an_off_grammar_traversal_step_is_withheld() -> None:
    raw = _synthetic_raw()
    raw["traversal"] = {"steps": [{"name": "maker_hop", "pattern": "candidate-[:MADE_BY]->m:Maker", "required": True}]}
    spec = DomainSpec.model_validate(raw)
    defects = admission_defects(spec)
    assert any("traversal[d1]" in d and "maker_hop" in d for d in defects), defects
    assert withholding_reason(spec) is not None


def test_pack_with_an_unbound_scoring_default_is_withheld_by_the_full_match_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pricealignment defaults its query parameter to $target_price, which this pack does not declare."""
    from engine.config import loader as loader_module

    monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", False)
    raw = _synthetic_raw()
    raw["scoring"] = {
        "dimensions": [
            {
                "name": "price",
                "source": "candidateproperty",
                "candidateprop": "size",
                "computation": "pricealignment",
                "weightkey": "wp",
                "defaultweight": 0.5,
            }
        ]
    }
    spec = DomainSpec.model_validate(raw)
    defects = admission_defects(spec)
    assert any("d1" in d and "'target_price'" in d for d in defects), defects
    assert withholding_reason(spec) is not None


def test_pack_whose_gate_references_an_undeclared_parameter_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _synthetic_raw()
    raw["gates"].append(
        {"name": "size_cap", "type": "threshold", "candidateprop": "size", "queryparam": "max_size", "operator": "<="}
    )
    # RU-050: validation now refuses the undeclared reference before admission.
    with pytest.raises(ValueError, match="Gate 'size_cap' references undeclared parameter 'max_size'"):
        DomainSpec.model_validate(raw)
    # Admission remains the backstop when that validation is switched off.
    from engine.config import loader as loader_module

    monkeypatch.setattr(loader_module.settings, "domain_undeclared_param_rejected", False)
    defects = admission_defects(DomainSpec.model_validate(raw))
    assert any("match[d1]" in d and "'max_size'" in d for d in defects), defects


def test_pre_repair_mortgage_step_is_withheld_naming_its_unassemblable_step() -> None:
    """RU-039 coverage on the mortgage shape RU-038 repaired: the off-grammar lender step is named."""
    raw = _raw("mortgage-brokerage")
    raw["traversal"] = {
        "steps": [{"name": "lenderresolve", "pattern": "candidate-[:OFFEREDBY]->lender:Lender", "required": True}]
    }
    defects = admission_defects(DomainSpec.model_validate(raw))
    assert any("traversal[borrowertoproduct]" in d and "lenderresolve" in d for d in defects), defects
    # The repository pack's lender step assembles (RU-038).
    assert admission_defects(_spec(DOMAINS_ROOT / "mortgage-brokerage")) == []


def test_current_freight_pack_is_withheld_naming_its_unbound_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config import loader as loader_module

    monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", False)
    defects = "\n".join(admission_defects(_spec(DOMAINS_ROOT / "freight-matching")))
    for param in ("'lat'", "'lon'", "'target_price'"):
        assert param in defects


def test_full_match_check_off_restores_previous_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config import loader as loader_module

    monkeypatch.setattr(loader_module.settings, "admission_checks_full_match", False)
    monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", False)
    raw = _synthetic_raw()
    raw["traversal"] = {"steps": [{"name": "maker_hop", "pattern": "candidate-[:MADE_BY]->m:Maker", "required": True}]}
    assert admission_defects(DomainSpec.model_validate(raw)) == []


# --------------------------------------------------------------------------
# Correctness remediation lane J (plan 0d84ec8, round 2): served-pack
# re-expressions with existing gate types.
# --------------------------------------------------------------------------


def _fail_wrapped_ge(candidate: bool | None, param: bool | None) -> bool:
    """`(c IS NOT NULL AND $p IS NOT NULL AND (c >= $p))` — the FAIL wrap of a threshold gate."""
    return candidate is not None and param is not None and bool(_three_valued_ge(candidate, param))


# RG-042 (RU-033 / CEG-COR-004, DEC-12 option A'): "when true, only active
# SupplyOpportunity candidates pass" is an implication with a declared default of
# true. Omitting the field bound NULL and the FAIL wrap emptied every result;
# false admitted inactive supply only.
_ACTIVE_SUPPLY_FRAGMENT = (
    "(candidate.active IS NOT NULL AND $requires_active_supply IS NOT NULL AND "
    "(candidate.active >= $requires_active_supply))"
)


def test_plasticos_active_supply_gate_is_an_implication_with_a_true_default() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _admitted_spec("plasticos")
    gate = _gate(spec, "active_supply_available")
    assert (gate.type.value, gate.operator, gate.nullbehavior.value) == ("threshold", ">=", "fail")
    field = next(f for f in spec.queryschema.fields if f.name == "requires_active_supply")
    assert field.default is True
    where = GateCompiler(spec).compile_all_gates(_DIR2)
    assert _ACTIVE_SUPPLY_FRAGMENT in where
    assert "candidate.active = $requires_active_supply" not in where
    # Direction 1 never sees the gate.
    assert "requires_active_supply" not in GateCompiler(spec).compile_all_gates(_DIR1)
    assert admission_defects(spec) == []
    assert "plasticos" in DomainPackLoader(str(DOMAINS_ROOT)).list_domains()


def test_plasticos_active_supply_truth_table() -> None:
    """p=true admits active only; p=false admits active and inactive; NULL active never passes."""
    table = {p: {c: _fail_wrapped_ge(c, p) for c in (None, False, True)} for p in (False, True)}
    assert table == {
        False: {None: False, False: True, True: True},
        True: {None: False, False: False, True: True},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("supplied", "bound"), [(None, True), (True, True), (False, False)])
async def test_plasticos_active_supply_binds_the_declared_default(supplied: bool | None, bound: bool) -> None:
    """handle_match without requires_active_supply binds the declared default, not None."""
    query = dict(_PLASTICOS_QUERIES[_DIR2])
    if supplied is not None:
        query["requires_active_supply"] = supplied
    cypher, parameters = await _capture_match("plasticos", _DIR2, query)
    assert parameters["requires_active_supply"] is bound
    assert _ACTIVE_SUPPLY_FRAGMENT in cypher
    assert _missing_params(cypher, parameters) == []


def _dormant_pack_spec(domain_id: str) -> DomainSpec:
    """A pack declared dormant (not withheld for defects): validated, never served by default."""
    spec = _spec(DOMAINS_ROOT / domain_id)
    assert spec.domain.activation is DomainActivation.DORMANT
    assert admission_defects(spec) == []
    return spec


def _le_pass_wrapped(candidate: bool | None, param: bool | None) -> bool:
    """`(c IS NULL OR $p IS NULL OR (c <= $p))` over booleans (false < true)."""
    return candidate is None or param is None or candidate <= param


# RG-046 (RU-035 / CEG-COR-005, DEC-13 option A): "a product requiring VA
# eligibility needs a VA-eligible borrower" is an implication. Equality dropped
# every non-VA product for a VA-eligible borrower.
_VA_FRAGMENT = (
    "(candidate.requiresvaeligibility IS NULL OR $vaeligible IS NULL OR "
    "(candidate.requiresvaeligibility <= $vaeligible))"
)


def test_mortgage_va_eligibility_is_an_implication() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _dormant_pack_spec("mortgage-brokerage")
    gate = _gate(spec, "va_eligibility")
    assert (gate.type.value, gate.operator, gate.nullbehavior.value) == ("threshold", "<=", "pass")
    where = GateCompiler(spec).compile_all_gates("borrowertoproduct")
    assert _VA_FRAGMENT in where
    assert "candidate.requiresvaeligibility = $vaeligible" not in where
    table = {p: {c: _le_pass_wrapped(c, p) for c in (None, False, True)} for p in (None, False, True)}
    assert table == {
        None: {None: True, False: True, True: True},
        False: {None: True, False: True, True: False},
        True: {None: True, False: True, True: True},
    }
