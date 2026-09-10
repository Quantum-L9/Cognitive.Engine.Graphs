from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.config.loader import DomainNotFoundError, DomainPackLoader
from engine.config.settings import settings
from engine.gates.compiler import GateCompiler
from engine.scoring.assembler import ScoringAssembler
from engine.sync.idea_portfolio import (
    IdeaGraphProjection,
    IdeaPortfolioHydrationError,
    IdeaPortfolioHydrator,
    build_portfolio_match_query,
    compile_hydration_plan,
    compile_upsert_command,
    projection_digest,
)

ROOT = Path(__file__).resolve().parents[2]


def _digest(char: str = "a") -> str:
    return "sha256:" + char * 64


def _projection(idea_id: str = "idea-alpha") -> dict[str, Any]:
    def assertion(kind: str, relation: str, key: str, state: str) -> dict[str, Any]:
        refs = [] if state == "UNKNOWN" else [f"Ideas/{idea_id}.md#{key}"]
        return {"kind": kind, "relation": relation, "key": key, "evidence_state": state, "source_refs": refs}

    return {
        "schema": "ideaos.idea-graph-projection/v1",
        "idea_id": idea_id,
        "source_refs": [f"Ideas/{idea_id}.md"],
        "source_digest": _digest(),
        "lifecycle": {"stage": "expanded", "decision": None, "proof_state": "P1", "execution_state": None},
        "assertions": [
            assertion("capability", "produces", "shared-capability", "VERIFIED"),
            assertion("capability", "requires", "required-capability", "SUPPORTED_INFERENCE"),
            assertion("substrate", "uses", "shared-substrate", "VERIFIED"),
            assertion("market", "targets", "industrial-ai", "HYPOTHESIS"),
            assertion("dependency", "depends_on", "idea-foundation", "VERIFIED"),
        ],
        "unknowns": ["external demand not yet proven"],
    }


def _envelope(expected: str | None = None) -> dict[str, Any]:
    return {
        "schema": "ceg.idea-portfolio-hydration/v1",
        "source_snapshot_ref": "Quantum-L9/IdeaOS@deadbeef",
        "source_snapshot_digest": _digest("b"),
        "expected_graph_revision": expected,
        "records": [
            {
                "schema": "ceg.idea-portfolio-sync-record/v1",
                "operation": "upsert",
                "projection": _projection(),
            }
        ],
    }


@pytest.mark.unit
def test_domain_is_dormant_until_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = DomainPackLoader(config_path=str(ROOT / "domains"))
    monkeypatch.setattr(settings, "idea_portfolio_enabled", False)
    assert "idea-portfolio" not in loader.list_domains()
    with pytest.raises(DomainNotFoundError, match="disabled"):
        loader.load_domain("idea-portfolio")

    monkeypatch.setattr(settings, "idea_portfolio_enabled", True)
    spec = loader.load_domain("idea-portfolio")
    assert spec.domain.id == "idea-portfolio"
    assert spec.sync.endpoints == []
    assert sum(d.defaultweight for d in spec.scoring.dimensions) == pytest.approx(1.0)
    assert "candidate.active" in GateCompiler(spec).compile_all_gates("portfolio_context_for_idea")
    scoring, _ = ScoringAssembler(spec).assemble_scoring_clause("portfolio_context_for_idea", {})
    assert "SUPPORTED_INFERENCE" in scoring
    assert "rel.evidence_state" in scoring


@pytest.mark.unit
def test_projection_admission_and_rank_filtering() -> None:
    model = IdeaGraphProjection.model_validate(_projection())
    assert model.wire_schema == "ideaos.idea-graph-projection/v1"
    query = build_portfolio_match_query(model)
    assert query["requires_count"] == query["produces_count"] == query["uses_count"] == 1
    assert query["targets_count"] == 0
    assert query["depends_on_facets"].startswith("|facet:")

    raw = _projection()
    raw["assertions"][0]["source_refs"] = []
    with pytest.raises(ValueError, match="source_ref"):
        IdeaGraphProjection.model_validate(raw)

    wrong_schema = _projection()
    wrong_schema["schema"] = "ideaos.idea-graph-projection/v0"
    with pytest.raises(ValueError, match="schema must equal"):
        IdeaGraphProjection.model_validate(wrong_schema)


@pytest.mark.unit
def test_upsert_preserves_all_assertions_and_wire_digest() -> None:
    model = IdeaGraphProjection.model_validate(_projection())
    command = compile_upsert_command(model, graph_revision=_digest("c"))
    assert "DELETE old" in command.cypher
    assert command.parameters["projection_digest"] == projection_digest(model)
    assert len(command.parameters["targets"]) == 1
    assert command.parameters["targets"][0]["evidence_state"] == "HYPOTHESIS"


class _Result:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    async def data(self) -> list[dict[str, Any]]:
        return self.rows

    async def consume(self) -> None:
        return None


class _Tx:
    def __init__(self, revision: str | None) -> None:
        self.revision = revision
        self.calls: list[str] = []

    async def run(self, cypher: str, parameters: dict[str, Any]) -> _Result:
        self.calls.append(cypher)
        if "RETURN state.current_revision AS current_revision" in cypher:
            return _Result([{"current_revision": self.revision}])
        return _Result()


class _Writer:
    def __init__(self, revision: str | None = None) -> None:
        self.tx = _Tx(revision)
        self.calls = 0
        self.database: str | None = None

    async def execute_write(
        self,
        fn: Any = None,
        *args: Any,
        database: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls += 1
        self.database = database
        if fn is None:
            raise AssertionError("hydrator must use one managed transaction")
        return await fn(self.tx, *args, **kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrator_feature_gate_and_atomic_revision_chain() -> None:
    disabled = _Writer()
    with pytest.raises(IdeaPortfolioHydrationError, match="disabled"):
        await IdeaPortfolioHydrator(disabled).apply(_envelope())
    assert disabled.calls == 0

    writer = _Writer()
    receipt = await IdeaPortfolioHydrator(writer, enabled=True).apply(_envelope())
    assert receipt["status"] == "applied"
    assert writer.calls == 1
    assert writer.database == "idea-portfolio"
    assert len(writer.tx.calls) == 3

    plan = compile_hydration_plan(_envelope())
    replay = _Writer(plan.graph_revision)
    receipt = await IdeaPortfolioHydrator(replay, enabled=True).apply(_envelope())
    assert receipt["status"] == "reused"
    assert len(replay.tx.calls) == 1

    conflict = _Writer(_digest("e"))
    with pytest.raises(IdeaPortfolioHydrationError, match="expected parent"):
        await IdeaPortfolioHydrator(conflict, enabled=True).apply(_envelope())
    assert len(conflict.tx.calls) == 1
