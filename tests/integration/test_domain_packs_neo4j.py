"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, domain-packs, gates, CEG-009]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_domain_packs_neo4j.py
CEG-009d — the per-pack Neo4j 5 witness for every pack on
``engine.config.loader.WITNESS_PENDING_PACKS``.

``tests/unit/test_domain_pack_shape.py`` proves the static half: each gated
pack compiles to Cypher that references only parameters something supplies.
This module is the live half, against the same ``neo4j:5.18-enterprise`` image
the rest of the integration suite uses, in two tiers per pack:

1. **Gate witness** — seed one candidate that should pass every gate and one
   candidate per gate that should fail exactly that gate, run the compiled
   WHERE clause with the pack's real query values, and assert the survivor
   set. This is what CEG-009a-c changed, so it is what un-gating turns on.
2. **Pipeline witness** — run the whole ``handle_match`` pipeline (traversal
   steps, gates, scoring) for the same pack and assert the passing candidate
   comes back. Scoring-side references nothing supplies (``$lat``, ``$param``,
   ``candidate.value``…) surface here; they belong to the scoring assembler
   and the packs' ``scoring:`` sections, not to the gate compiler.

A pack leaves ``WITNESS_PENDING_PACKS`` — and ``unvalidated_domain_packs_enabled``
— only when both tiers are green for it. The scenarios below are the record
of what "green" means per pack; they are not fixtures to be relaxed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config.loader import SPEC_FILENAME, WITNESS_PENDING_PACKS
from engine.config.schema import DomainSpec
from engine.gates.compiler import GateCompiler
from engine.utils.security import sanitize_database_name, sanitize_label

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"

WITNESS_TAG_PROP = "witness_tag"
WITNESS_ID_PROP = "witness_id"


@dataclass(frozen=True)
class Candidate:
    """One seeded candidate node and the taxonomy nodes it is linked to."""

    witness_id: str
    props: dict[str, Any]
    # (edge type, target label, target props) — each MERGEd and linked from the candidate.
    links: tuple[tuple[str, str, dict[str, Any]], ...] = ()


@dataclass(frozen=True)
class Scenario:
    """A query and the witness ids that must survive every gate for it."""

    name: str
    query: dict[str, Any]
    expect_pass: frozenset[str]


@dataclass(frozen=True)
class PackWitness:
    direction: str
    candidates: tuple[Candidate, ...]
    scenarios: tuple[Scenario, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


def _d(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, day)


# --------------------------------------------------------------------------
# Per-pack witness record. Candidate ids name the gate they are built to fail.
# --------------------------------------------------------------------------

PACK_WITNESSES: dict[str, PackWitness] = {
    "healthcare-referral": PackWitness(
        direction="patienttoprovider",
        candidates=(
            Candidate(
                "pass",
                {"acceptsnewpatients": True, "currentpanelsize": 0, "maxpanelsize": 10, "distance": 5.0},
                (("ACCEPTS_INSURANCE", "InsurancePlan", {"planid": "PLAN-A"}),),
            ),
            Candidate(
                "fail_not_accepting",
                {"acceptsnewpatients": False, "currentpanelsize": 0, "maxpanelsize": 10, "distance": 5.0},
                (("ACCEPTS_INSURANCE", "InsurancePlan", {"planid": "PLAN-A"}),),
            ),
            Candidate(
                "fail_panel_full",
                # selfrange with the literal operand 1: currentpanelsize <= 1 <= maxpanelsize
                {"acceptsnewpatients": True, "currentpanelsize": 5, "maxpanelsize": 10, "distance": 5.0},
                (("ACCEPTS_INSURANCE", "InsurancePlan", {"planid": "PLAN-A"}),),
            ),
            Candidate(
                "fail_too_far",
                {"acceptsnewpatients": True, "currentpanelsize": 0, "maxpanelsize": 10, "distance": 40.0},
                (("ACCEPTS_INSURANCE", "InsurancePlan", {"planid": "PLAN-A"}),),
            ),
            Candidate(
                "fail_wrong_plan",
                {"acceptsnewpatients": True, "currentpanelsize": 0, "maxpanelsize": 10, "distance": 5.0},
                (("ACCEPTS_INSURANCE", "InsurancePlan", {"planid": "PLAN-B"}),),
            ),
            Candidate(
                "fail_no_plan",
                {"acceptsnewpatients": True, "currentpanelsize": 0, "maxpanelsize": 10, "distance": 5.0},
            ),
        ),
        scenarios=(
            Scenario(
                "routine",
                {
                    "patientid": "p-1",
                    "age": 41,
                    "zipcode": "90001",
                    "insuranceplan": "PLAN-A",
                    "primarycondition": "asthma",
                    "urgency": "routine",
                    "maxdistancemiles": 25.0,
                },
                frozenset({"pass"}),
            ),
        ),
        notes=(
            "insurance_accepted replaces the insurance_check traversal step, whose property map TraversalAssembler forbids.",
        ),
    ),
    "legal-discovery": PackWitness(
        direction="casetodocument",
        candidates=(
            Candidate("pass", {"jurisdiction": "CA", "privileged": False, "documentdate": _d(2020, 6, 1)}),
            Candidate(
                "fail_other_jurisdiction", {"jurisdiction": "NY", "privileged": False, "documentdate": _d(2020, 6, 1)}
            ),
            Candidate("fail_privileged", {"jurisdiction": "CA", "privileged": True, "documentdate": _d(2020, 6, 1)}),
            Candidate(
                "fail_out_of_window", {"jurisdiction": "CA", "privileged": False, "documentdate": _d(2019, 1, 1)}
            ),
        ),
        scenarios=(
            Scenario(
                "privilege_excluded",
                {
                    "caseid": "c-1",
                    "jurisdiction": "CA",
                    "casetype": "civil",
                    "topics": "contracts",
                    "daterangestart": _d(2020, 1, 1),
                    "daterangeend": _d(2021, 1, 1),
                    "includeprivileged": False,
                },
                frozenset({"pass"}),
            ),
            Scenario(
                "privilege_included",
                # strictwhen `$query.includeprivileged = false` no longer holds: privilege_filter opens.
                {
                    "caseid": "c-1",
                    "jurisdiction": "CA",
                    "casetype": "civil",
                    "topics": "contracts",
                    "daterangestart": _d(2020, 1, 1),
                    "daterangeend": _d(2021, 1, 1),
                    "includeprivileged": True,
                },
                frozenset({"pass", "fail_privileged"}),
            ),
        ),
    ),
    "research-agent": PackWitness(
        direction="querytopaper",
        candidates=(
            Candidate("pass", {"publicationdate": _d(2021, 3, 1), "hascode": True, "hasdataset": False}),
            Candidate(
                "pass_dataset_not_required", {"publicationdate": _d(2021, 3, 1), "hascode": True, "hasdataset": True}
            ),
            Candidate("fail_stale", {"publicationdate": _d(2019, 3, 1), "hascode": True, "hasdataset": False}),
            Candidate("fail_no_code", {"publicationdate": _d(2021, 3, 1), "hascode": False, "hasdataset": False}),
        ),
        scenarios=(
            Scenario(
                "code_required",
                {
                    "queryid": "q-1",
                    "query": "graph matching",
                    "domain": "ml",
                    "mindate": _d(2020, 1, 1),
                    "requirescode": True,
                    "requiresdataset": False,
                },
                # dataset_availability is strict only while requiresdataset is true, so a
                # paper WITH a dataset is not rejected for not matching `false`.
                frozenset({"pass", "pass_dataset_not_required"}),
            ),
            Scenario(
                "nothing_required",
                {
                    "queryid": "q-1",
                    "query": "graph matching",
                    "domain": "ml",
                    "mindate": _d(2020, 1, 1),
                    "requirescode": False,
                    "requiresdataset": False,
                },
                frozenset({"pass", "pass_dataset_not_required", "fail_no_code"}),
            ),
        ),
    ),
    "executive-assistant": PackWitness(
        direction="tasktoexpert",
        candidates=(
            Candidate(
                "pass",
                {"availablehours": 10.0, "currentutilizationpct": 50.0, "avgresponsetimehours": 2.0},
                (("HAS_SKILL", "Skill", {"name": "python"}),),
            ),
            Candidate(
                "fail_no_skill",
                {"availablehours": 10.0, "currentutilizationpct": 50.0, "avgresponsetimehours": 2.0},
                (("HAS_SKILL", "Skill", {"name": "go"}),),
            ),
            Candidate(
                "fail_no_hours",
                {"availablehours": 2.0, "currentutilizationpct": 50.0, "avgresponsetimehours": 2.0},
                (("HAS_SKILL", "Skill", {"name": "python"}),),
            ),
            Candidate(
                "fail_overloaded",
                # threshold with the literal operand 85.0
                {"availablehours": 10.0, "currentutilizationpct": 90.0, "avgresponsetimehours": 2.0},
                (("HAS_SKILL", "Skill", {"name": "python"}),),
            ),
        ),
        scenarios=(
            Scenario(
                "python_task",
                {
                    "taskid": "t-1",
                    "category": "engineering",
                    "priority": "high",
                    "estimatedhours": 4.0,
                    # deadline_feasibility compares avgresponsetimehours (float) with deadline
                    # (datetime), a type mismatch Cypher evaluates to NULL; it is a pack defect
                    # on record in docs/FEATURE_GATES.md §16 and is exercised here only in its
                    # null-pass form so the other gates can be witnessed.
                    "deadline": None,
                    "requiredskills": "python",
                },
                frozenset({"pass"}),
            ),
        ),
        notes=("deadline_feasibility: float vs datetime comparison — witnessed in null-pass form only.",),
    ),
    "repo-as-agent": PackWitness(
        direction="issuetocontributor",
        candidates=(
            Candidate("pass", {"activeprs": 2}, (("OWNS_MODULE", "CodeModule", {"modulepath": "engine/gates"}),)),
            Candidate(
                "fail_other_module", {"activeprs": 2}, (("OWNS_MODULE", "CodeModule", {"modulepath": "chassis"}),)
            ),
            # threshold with the literal operand 5
            Candidate("fail_busy", {"activeprs": 9}, (("OWNS_MODULE", "CodeModule", {"modulepath": "engine/gates"}),)),
        ),
        scenarios=(
            Scenario(
                "gates_issue",
                {
                    "issueid": "i-1",
                    "title": "traversal gate",
                    "labels": "bug",
                    "affectedfiles": "engine/gates",
                    "priority": "high",
                    "estimatedcomplexity": "medium",
                },
                frozenset({"pass"}),
            ),
        ),
    ),
    "roofing-company": PackWitness(
        direction="projecttocontractor",
        candidates=(
            Candidate(
                "pass",
                {
                    "specialties": ["shingle", "tile"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": False,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
            Candidate(
                "pass_emergency_ready",
                {
                    "specialties": ["shingle"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
            Candidate(
                "fail_zip",
                {
                    "specialties": ["shingle"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90001"}),),
            ),
            Candidate(
                "fail_specialty",
                {
                    "specialties": ["metal"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
            Candidate(
                "fail_capacity",
                # selfrange with the literal operand 1
                {
                    "specialties": ["shingle"],
                    "currentprojectcount": 2,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
            Candidate(
                "fail_uninsured",
                {
                    "specialties": ["shingle"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": False,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 10, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
            Candidate(
                "fail_late",
                {
                    "specialties": ["shingle"],
                    "currentprojectcount": 0,
                    "maxconcurrentprojects": 3,
                    "insurancelicensed": True,
                    "emergencyavailable": True,
                    "nextavailabledate": _d(2026, 12, 1),
                },
                (("SERVICES_ZIP", "ZipCode", {"zipcode": "90210"}),),
            ),
        ),
        scenarios=(
            Scenario(
                "routine",
                {
                    "projectid": "r-1",
                    "zipcode": "90210",
                    "rooftype": "shingle",
                    "squarefeet": 2000,
                    "urgency": "routine",
                    "hasinsuranceclaim": True,
                    "budgetusd": 15000.0,
                    "desiredstartdate": _d(2026, 11, 1),
                },
                frozenset({"pass", "pass_emergency_ready"}),
            ),
            Scenario(
                "emergency",
                # strictwhen `$query.urgency = 'emergency'` holds: emergency_availability enforces.
                {
                    "projectid": "r-1",
                    "zipcode": "90210",
                    "rooftype": "shingle",
                    "squarefeet": 2000,
                    "urgency": "emergency",
                    "hasinsuranceclaim": True,
                    "budgetusd": 15000.0,
                    "desiredstartdate": _d(2026, 11, 1),
                },
                frozenset({"pass_emergency_ready"}),
            ),
        ),
    ),
    "aios-god-agent": PackWitness(
        direction="intenttotool",
        candidates=(
            Candidate(
                "pass", {"avgexecutiontimeseconds": 10}, (("HAS_CAPABILITY", "Capability", {"capabilityid": "search"}),)
            ),
            Candidate(
                "fail_slow",
                {"avgexecutiontimeseconds": 60},
                (("HAS_CAPABILITY", "Capability", {"capabilityid": "search"}),),
            ),
            Candidate(
                "fail_no_capability",
                {"avgexecutiontimeseconds": 10},
                (("HAS_CAPABILITY", "Capability", {"capabilityid": "email"}),),
            ),
        ),
        scenarios=(
            Scenario(
                "search_intent",
                {
                    "intentid": "n-1",
                    "userinput": "find papers",
                    "category": "research",
                    "requiredcapabilities": "search",
                    "maxexecutiontimeseconds": 30,
                },
                frozenset({"pass"}),
            ),
        ),
    ),
}


def test_every_witness_pending_pack_has_a_witness_record() -> None:
    """The ledger and this record move together (no Neo4j needed)."""
    assert set(PACK_WITNESSES) == set(WITNESS_PENDING_PACKS), (
        "WITNESS_PENDING_PACKS and PACK_WITNESSES disagree — a pack on the ledger needs a witness here, "
        "and a pack that left the ledger should keep its witness as a regression record only if it is still gated."
    )


def test_every_scenario_expects_a_declared_candidate() -> None:
    for pack, witness in PACK_WITNESSES.items():
        declared = {c.witness_id for c in witness.candidates}
        assert len(declared) == len(witness.candidates), f"{pack}: duplicate witness ids"
        for scenario in witness.scenarios:
            assert scenario.expect_pass <= declared, f"{pack}/{scenario.name} expects an undeclared candidate"
            assert scenario.expect_pass, f"{pack}/{scenario.name} expects nothing to pass"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _spec(pack: str) -> DomainSpec:
    raw = yaml.safe_load((DOMAINS_DIR / pack / SPEC_FILENAME).read_text(encoding="utf-8"))
    return DomainSpec.model_validate(raw)


def _candidate_label(spec: DomainSpec, direction: str) -> str:
    labels = [c.label for c in spec.matchentities.candidate if c.matchdirection == direction]
    assert len(labels) == 1, f"{spec.domain.id}: expected one candidate label for {direction}, got {labels}"
    return sanitize_label(labels[0])


def _query_parameters(spec: DomainSpec, query: dict[str, Any]) -> dict[str, Any]:
    """What handle_match supplies for the query: every schema field, defaulted."""
    params = dict(query)
    for field_spec in spec.queryschema.fields:
        params.setdefault(field_spec.name, field_spec.default)
    return params


async def _ensure_database(graph_driver, pack: str) -> str:
    database = sanitize_database_name(pack)
    await graph_driver.execute_query(
        cypher="CREATE DATABASE `" + database + "` IF NOT EXISTS WAIT",
        parameters={},
        database="system",
    )
    return database


async def _seed(graph_driver, database: str, label: str, witness: PackWitness, tag: str) -> None:
    for candidate in witness.candidates:
        props = {**candidate.props, WITNESS_TAG_PROP: tag, WITNESS_ID_PROP: candidate.witness_id}
        await graph_driver.execute_write(
            cypher="CREATE (c:" + label + ") SET c = $props",
            parameters={"props": props},
            database=database,
        )
        for edge_type, target_label, target_props in candidate.links:
            # A parameter map cannot be used inside a MERGE pattern, so the
            # taxonomy node's keys are spelled out (validated identifiers) and
            # only the values travel as parameters.
            target_keys = {key: sanitize_label(key) for key in target_props}
            target_map = ", ".join(f"{safe}: $target_{safe}" for safe in target_keys.values())
            await graph_driver.execute_write(
                cypher=(
                    "MATCH (c:" + label + " {" + WITNESS_TAG_PROP + ": $tag, " + WITNESS_ID_PROP + ": $id}) "
                    "MERGE (t:" + sanitize_label(target_label) + " {" + target_map + "}) "
                    "SET t." + WITNESS_TAG_PROP + " = $tag "
                    "MERGE (c)-[:" + sanitize_label(edge_type) + "]->(t)"
                ),
                parameters={
                    "tag": tag,
                    "id": candidate.witness_id,
                    **{f"target_{safe}": target_props[key] for key, safe in target_keys.items()},
                },
                database=database,
            )


async def _cleanup(graph_driver, database: str, tag: str) -> None:
    await graph_driver.execute_write(
        cypher="MATCH (n {" + WITNESS_TAG_PROP + ": $tag}) DETACH DELETE n",
        parameters={"tag": tag},
        database=database,
    )


def _candidate_witness_ids(result: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in result.get("candidates", []):
        node = item.get("candidate", item) if isinstance(item, dict) else {}
        if isinstance(node, dict) and WITNESS_ID_PROP in node:
            ids.add(str(node[WITNESS_ID_PROP]))
    return ids


# --------------------------------------------------------------------------
# Tier 1 — the compiled gates, executed
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("pack", sorted(PACK_WITNESSES))
async def test_compiled_gates_execute_and_filter_on_neo4j(graph_driver, pack: str) -> None:
    """Every gate of the pack runs on Neo4j 5 and rejects exactly its failing candidate."""
    witness = PACK_WITNESSES[pack]
    spec = _spec(pack)
    label = _candidate_label(spec, witness.direction)
    database = await _ensure_database(graph_driver, pack)
    tag = f"ceg009d-{uuid.uuid4().hex[:10]}"

    compiler = GateCompiler(spec)
    where_clause = compiler.compile_all_gates(witness.direction)
    assert where_clause != "true", f"{pack}: no gate applies to {witness.direction}; nothing to witness"

    await _seed(graph_driver, database, label, witness, tag)
    try:
        for scenario in witness.scenarios:
            parameters = {
                **_query_parameters(spec, scenario.query),
                **compiler.last_query_params,
                "witness_tag": tag,
            }
            rows = await graph_driver.execute_query(
                cypher=(
                    "MATCH (candidate:" + label + ") "
                    "WHERE candidate." + WITNESS_TAG_PROP + " = $witness_tag AND (" + where_clause + ") "
                    "RETURN candidate." + WITNESS_ID_PROP + " AS witness_id"
                ),
                parameters=parameters,
                database=database,
            )
            survivors = {str(row["witness_id"]) for row in rows}
            assert survivors == set(scenario.expect_pass), (
                f"{pack}/{scenario.name}: gates let through {sorted(survivors)}, "
                f"expected {sorted(scenario.expect_pass)}\nWHERE {where_clause}"
            )
    finally:
        await _cleanup(graph_driver, database, tag)


# --------------------------------------------------------------------------
# Tier 2 — the whole match pipeline (the un-gate criterion)
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("pack", sorted(PACK_WITNESSES))
async def test_handle_match_pipeline_serves_the_pack(graph_driver, domain_loader, pack: str, monkeypatch) -> None:
    """handle_match runs end to end for the pack and returns the passing candidate.

    This is the bar for removing a pack from WITNESS_PENDING_PACKS. Failures here
    that name a parameter or property the gates never reference are scoring or
    traversal-step defects; record them against the pack in docs/FEATURE_GATES.md
    §16 rather than loosening this assertion.
    """
    from engine.config.settings import settings
    from engine.handlers import handle_match, init_dependencies
    from engine.state import get_state

    witness = PACK_WITNESSES[pack]
    spec = _spec(pack)
    label = _candidate_label(spec, witness.direction)
    database = await _ensure_database(graph_driver, pack)
    tag = f"ceg009d-{uuid.uuid4().hex[:10]}"

    monkeypatch.setattr(settings, "unvalidated_domain_packs_enabled", True)
    domain_loader.invalidate(pack)
    get_state().reset()
    init_dependencies(graph_driver, domain_loader)

    await _seed(graph_driver, database, label, witness, tag)
    try:
        for scenario in witness.scenarios:
            result = await handle_match(
                pack,
                {"query": dict(scenario.query), "match_direction": witness.direction, "top_n": 50},
            )
            returned = _candidate_witness_ids(result)
            assert set(scenario.expect_pass) <= returned, (
                f"{pack}/{scenario.name}: handle_match returned {sorted(returned)}, "
                f"expected at least {sorted(scenario.expect_pass)}"
            )
            assert not (returned - set(scenario.expect_pass)), (
                f"{pack}/{scenario.name}: handle_match also returned {sorted(returned - set(scenario.expect_pass))}"
            )
    finally:
        await _cleanup(graph_driver, database, tag)
        get_state().reset()
        domain_loader.invalidate(pack)
