"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, scoring, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_handle_score.py
Unit tests for the local ``engine.handlers.handle_score`` and the Gate-routed
``handle_score_remote`` adapter (SCORING-GATE-01) that builds its binding.

The bindings here are synthetic, in-memory and report ``connected=False``. They
exercise the handler's validation, tenant confinement and composition only.
The real Neo4j tenant-isolation witness (acceptance 6, 7, 10) is NOT RUN: no
admitted EIE -> Gate -> CEG projection with relationship evidence exists yet.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine import handlers
from engine.handlers import ACTION_HANDLERS, ValidationError, handle_score
from engine.scoring.evidence_priority import (
    EvidenceObservation,
    EvidenceRead,
    approved_relevance_map,
)
from engine.state import get_state

ROOT = Path(__file__).resolve().parents[2]
REL = "SYNTHETIC_POSITIVE_REL"
A_REF = "hubspot.company:101"
B_REF = "hubspot.company:201"


def _observation(entity_ref: str, evidence_ref: str, confidence: float) -> EvidenceObservation:
    return EvidenceObservation(
        entity_ref=entity_ref,
        evidence_ref=evidence_ref,
        relationship_type=REL,
        source_ref=f"source:{evidence_ref}",
        confidence=confidence,
        admission="admitted",
        polarity="positive",
    )


class TwoTenantBinding:
    """In-memory evidence keyed by database; records every read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, tuple[str, ...]]] = []
        self._graphs = {
            "tenant-a": (_observation(A_REF, "evidence:a1", 0.8),),
            "tenant-b": (_observation(B_REF, "evidence:b1", 0.9),),
        }

    @property
    def approved_relevance(self) -> Mapping[str, float]:
        return approved_relevance_map({REL: 1.0})

    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        self.reads.append((database, tuple(entity_refs)))
        observations = tuple(o for o in self._graphs.get(database, ()) if o.entity_ref in entity_refs)
        return EvidenceRead(connected=False, database=database, observations=observations)


class WrongDatabaseBinding(TwoTenantBinding):
    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        return EvidenceRead(connected=False, database="tenant-b", observations=())


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


async def test_without_binding_result_is_blocked_and_nothing_is_scored() -> None:
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1})
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False
    assert result["scored"] == []
    assert result["abstained"] == []
    assert result["source_revision"] is None


async def test_binding_reads_only_the_tenant_database() -> None:
    binding = TwoTenantBinding()
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == [("tenant-a", (A_REF,))]
    assert result["scored"][0]["score"] == 80.0
    assert result["scored"][0]["factors"][0]["evidence_refs"] == ["evidence:a1"]


async def test_tenant_a_cannot_score_or_see_tenant_b_ref() -> None:
    binding = TwoTenantBinding()
    result = await handle_score("tenant-a", {"entity_refs": [A_REF, B_REF], "top_n": 2}, binding=binding)
    assert binding.reads == [("tenant-a", (A_REF, B_REF))]
    assert [e["entity_ref"] for e in result["scored"]] == [A_REF]
    assert result["abstained"][0]["entity_ref"] == B_REF
    assert result["abstained"][0]["score"] is None
    assert "evidence:b1" not in repr(result)


async def test_binding_that_reads_another_database_is_refused() -> None:
    with pytest.raises(handlers.ExecutionError, match="not the tenant database"):
        await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=WrongDatabaseBinding())


@pytest.mark.parametrize(
    "payload",
    [
        {"entity_refs": [A_REF], "top_n": 1, "tenant": "tenant-b"},
        {"entity_refs": [A_REF], "top_n": 1, "database": "tenant-b"},
        {"entity_refs": [A_REF], "top_n": 1, "weights": {REL: 1.0}},
        {"entity_refs": [A_REF], "top_n": 1, "cypher": "MATCH (n) DETACH DELETE n"},
        {"entity_refs": [A_REF, A_REF], "top_n": 1},
        {"entity_refs": [f"hubspot.company:{i}" for i in range(26)], "top_n": 1},
    ],
)
async def test_invalid_requests_are_refused_before_any_read(payload: dict[str, Any]) -> None:
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError):
        await handle_score("tenant-a", payload, binding=binding)
    assert binding.reads == []


async def test_illegal_tenant_id_is_refused_before_any_read() -> None:
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError, match="not a legal tenant id"):
        await handle_score("Tenant A", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == []


async def test_tenant_outside_allowlist_is_refused_before_any_read() -> None:
    get_state()._tenant_allowlist = {"tenant-b"}
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError, match="allowlist"):
        await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == []


async def test_handler_works_with_match_path_symbols_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 8: nothing on the match path is on the score call graph."""

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        msg = "match-path symbol used by handle_score"
        raise AssertionError(msg)

    for symbol in (
        "DomainPackLoader",
        "GateCompiler",
        "TraversalAssembler",
        "ScoringAssembler",
        "handle_match",
        "_require_deps",
        "_enforce_capability",
    ):
        monkeypatch.setattr(handlers, symbol, unavailable)
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=TwoTenantBinding())
    assert result["scored"][0]["entity_ref"] == A_REF


def test_only_the_secured_adapter_is_registered_and_advertised() -> None:
    """SCORING-GATE-01: ``score`` routes to ``handle_score_remote``, never to the local handler.

    The local ``handle_score`` takes a caller-built binding; registering it
    would let the chassis call it with none. The remote adapter builds its own.
    """
    assert ACTION_HANDLERS["score"] is handlers.handle_score_remote
    assert handle_score not in ACTION_HANDLERS.values()
    spec = yaml.safe_load((ROOT / "engine" / "spec.yaml").read_text(encoding="utf-8"))
    assert spec["node"]["actions"] == ["match", "sync", "outcomes", "resolve", "health-assess", "score"]


# ── SCORING-GATE-01: the Gate-routed adapter ─────────────────────────────────

REMOTE_TENANT = "it-cheese-aging"
REMOTE_EDGE = "TENDED_BY"
REMOTE_SUBJECT = "creamery.cave:101"
REMOTE_REQUEST = {"entity_refs": [REMOTE_SUBJECT], "top_n": 1}


def _remote_spec(
    root: Path,
    *,
    version: str = "1.0.0-reviewed",
    capabilities: list[dict[str, Any]] | None = None,
    relevance: float | None = 1.0,
    evidence_endpoint: bool = True,
) -> Path:
    """The synthetic cheese-aging domain under the tenant id, with one evidence endpoint."""
    from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

    spec = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text(encoding="utf-8"))
    spec["domain"]["id"] = REMOTE_TENANT
    spec["domain"]["version"] = version
    for edge in spec["ontology"]["edges"]:
        if edge["type"] == REMOTE_EDGE and relevance is not None:
            edge["evidencerelevance"] = relevance
    spec["sync"]["endpoints"].append(
        {"path": "/v1/sync/affineurs", "method": "POST", "batchstrategy": "UNWINDMERGE"}
        | {"targetnode": "Affineur", "idproperty": "affineur_id"}
    )
    if evidence_endpoint:
        spec["sync"]["endpoints"].append(
            {"path": "/v1/sync/tended-by", "method": "POST", "batchstrategy": "UNWINDMERGE"}
            | {"targetedge": REMOTE_EDGE, "fromnode": "AgingCave", "tonode": "Affineur"}
        )
    if capabilities is not None:
        spec["capabilities"] = capabilities
    (root / REMOTE_TENANT).mkdir(parents=True)
    (root / REMOTE_TENANT / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return root


SCORE_GRANT = [{"name": "score_read", "actions": ["score:read"], "allowed_subjects": [REMOTE_TENANT]}]


class RecordingDriver:
    """GraphDriver stand-in: records every read, refuses every other graph call."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, exc: Exception | None = None) -> None:
        self.reads: list[tuple[str, dict[str, Any], str]] = []
        self._rows = rows or []
        self._exc = exc

    def get_driver(self) -> object:
        return object()

    async def execute_read(self, cypher: str, parameters: dict[str, Any], *, database: str) -> list[dict[str, Any]]:
        self.reads.append((cypher, parameters, database))
        if self._exc is not None:
            raise self._exc
        return self._rows

    async def execute_query(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("score must not use execute_query")

    async def execute_write(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("score must never write")


def _edge(evidence_ref: str = "evidence:1", **props: Any) -> dict[str, Any]:
    base = {"evidence_ref": evidence_ref, "source_ref": "source:crm-note:42", "confidence": 0.8}
    base |= {"assertion_status": "observed", "polarity": "positive", "source_revision": "rev-2026-10-25"}
    return {"entity_ref": REMOTE_SUBJECT, "relationship_type": REMOTE_EDGE, "props": base | props}


@pytest.fixture
def remote_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "remote_score_enabled", True)
    monkeypatch.setattr(settings, "evidence_identity_per_type_enabled", True)


def _wire(root: Path, driver: RecordingDriver) -> None:
    from engine.config.loader import DomainPackLoader

    handlers.init_dependencies(driver, DomainPackLoader(config_path=str(root)))  # type: ignore[arg-type]


async def test_remote_score_off_by_default_reads_nothing(tmp_path: Path) -> None:
    """AT-01: the default refuses before any spec load or graph read."""
    from engine.config.settings import Settings

    assert Settings().remote_score_enabled is False
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), driver)
    with pytest.raises(handlers.FeatureNotEnabled, match="REMOTE_SCORE_ENABLED"):
        await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert driver.reads == []


@pytest.mark.usefixtures("remote_on")
async def test_remote_score_refuses_ref_only_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-11: identity rollback never reintroduces a ref-only rank through the remote route."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "evidence_identity_per_type_enabled", False)
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), driver)
    with pytest.raises(handlers.FeatureNotEnabled, match="EVIDENCE_IDENTITY_PER_TYPE_ENABLED"):
        await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert driver.reads == []


@pytest.mark.usefixtures("remote_on")
@pytest.mark.parametrize("capability_auth", [True, False], ids=["cap-auth-on", "cap-auth-off"])
@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        [],
        [{"name": "wildcard", "actions": ["score:read"], "allowed_subjects": ["*"]}],
        [{"name": "match_only", "actions": ["match:read"], "allowed_subjects": [REMOTE_TENANT]}],
        [{"name": "other", "actions": ["score:read"], "allowed_subjects": ["it-orbital-logistics"]}],
        [{"name": "action_name", "actions": ["score"], "allowed_subjects": [REMOTE_TENANT]}],
    ],
    ids=["undeclared", "empty", "wildcard", "match-read-only", "other-tenant", "action-not-permission"],
)
async def test_remote_score_needs_an_explicit_tenant_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability_auth: bool,
    capabilities: list[dict[str, Any]] | None,
) -> None:
    """AT-04 / AT-05: no explicit score:read, no read, whatever CAPABILITY_AUTH_ENABLED says."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "capability_auth_enabled", capability_auth)
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=capabilities), driver)
    with pytest.raises(handlers.AuthorizationError, match="score:read"):
        await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert driver.reads == []


@pytest.mark.usefixtures("remote_on")
@pytest.mark.parametrize("capability_auth", [True, False], ids=["cap-auth-on", "cap-auth-off"])
async def test_explicit_grant_scores_through_the_spec_bound_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability_auth: bool
) -> None:
    """AT-02 (unit layer) / AT-06 / AT-13: the grant is checked either way; the read is confined."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "capability_auth_enabled", capability_auth)
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), driver)
    result = await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)

    assert len(driver.reads) == 1
    cypher, params, database = driver.reads[0]
    assert database == REMOTE_TENANT
    assert params["tenant"] == REMOTE_TENANT
    assert params["refs"] == [REMOTE_SUBJECT]
    assert f":{REMOTE_EDGE}" in cypher
    assert result["policy_version"] == "graph_evidence_priority_v1"
    assert result["source_revision"] == "rev-2026-10-25"
    assert [e["entity_ref"] for e in result["scored"]] == [REMOTE_SUBJECT]
    assert result["scored"][0]["factors"][0]["evidence_refs"] == ["evidence:1"]


@pytest.mark.usefixtures("remote_on")
@pytest.mark.parametrize(
    "spec_overrides",
    [
        {"version": "1.0.0-proposed"},
        {"version": "1.0.0"},
        {"relevance": None},
        {"relevance": None, "evidence_endpoint": False},
    ],
    ids=["proposed", "unstaged", "no-relevance", "no-evidence-type"],
)
async def test_inadmissible_spec_is_blocked_without_a_read(tmp_path: Path, spec_overrides: dict[str, Any]) -> None:
    """AT-07: a draft spec or one with no approved relevance / evidence type never ranks."""
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT, **spec_overrides), driver)
    result = await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert result["status"] == "blocked"
    assert result["scored"] == []
    assert driver.reads == []


@pytest.mark.usefixtures("remote_on")
@pytest.mark.parametrize(
    "payload",
    [
        REMOTE_REQUEST | {"database": "it-orbital-logistics"},
        REMOTE_REQUEST | {"relevance": {REMOTE_EDGE: 1.0}},
        REMOTE_REQUEST | {"binding": "caller"},
        REMOTE_REQUEST | {"gate_verified": True},
        {"entity_refs": [], "top_n": 1},
    ],
    ids=["database", "relevance", "binding", "gate-verified-flag", "empty-cohort"],
)
async def test_caller_cannot_supply_server_owned_fields(tmp_path: Path, payload: dict[str, Any]) -> None:
    driver = RecordingDriver([_edge()])
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), driver)
    with pytest.raises(ValidationError):
        await handlers.handle_score_remote(REMOTE_TENANT, payload)
    assert driver.reads == []


@pytest.mark.usefixtures("remote_on")
@pytest.mark.parametrize(
    "rows",
    [
        [_edge(assertion_status="inferred")],
        [_edge(assertion_status="reported")],
        [_edge(expires_at="2020-01-01T00:00:00+00:00")],
    ],
    ids=["inferred", "reported", "expired"],
)
async def test_unqualified_evidence_abstains(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    """AT-09 (unit layer): no qualifying rank, and no fabricated zero."""
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), RecordingDriver(rows))
    result = await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert result["scored"] == []
    assert [a["entity_ref"] for a in result["abstained"]] == [REMOTE_SUBJECT]
    assert result["abstained"][0]["score"] is None


@pytest.mark.usefixtures("remote_on")
async def test_same_ref_conflicting_within_one_type_is_not_corroboration(tmp_path: Path) -> None:
    """AT-10 (unit layer): two incompatible observations of one typed ref abstain."""
    rows = [_edge(polarity="positive"), _edge(polarity="negative")]
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), RecordingDriver(rows))
    result = await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert result["scored"] == []
    assert result["abstained"][0]["detail"] == "conflicting_evidence"


@pytest.mark.usefixtures("remote_on")
async def test_missing_tenant_database_is_blocked_not_provisioned(tmp_path: Path) -> None:
    """AT-12 (unit layer): an absent database is ``blocked``; nothing is created."""
    from engine.graph.driver import DatabaseNotProvisionedError

    driver = RecordingDriver(exc=DatabaseNotProvisionedError(REMOTE_TENANT))
    _wire(_remote_spec(tmp_path, capabilities=SCORE_GRANT), driver)
    result = await handlers.handle_score_remote(REMOTE_TENANT, REMOTE_REQUEST)
    assert result["status"] == "blocked"
    assert "never creates it" in result["evidence_reader"]["reason"]
