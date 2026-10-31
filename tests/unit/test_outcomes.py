import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import ValidationError, handle_admin, handle_outcomes, handle_sync, init_dependencies
from engine.outcomes.engine import OutcomeEngine
from engine.outcomes.schema import OutcomeEvent
from engine.scoring.assembler import ScoringAssembler


def test_outcome_engine_is_idempotent() -> None:
    """Outcome engine idempotency — skip if required methods not implemented."""
    loader = DomainPackLoader()
    if not hasattr(loader, "allowed_canonical_labels"):
        pytest.skip("DomainPackLoader.allowed_canonical_labels not implemented")
    graph = GraphDriver(loader.allowed_canonical_labels())
    scoring = ScoringAssembler(loader)
    engine = OutcomeEngine(graph, scoring)

    event = OutcomeEvent(
        event_id="event-1",
        entity_id="entity-1",
        action_name="admin",
        canonical_label="company",
        outcome_state="success",
    )
    result1 = asyncio.run(engine.process("tenant-a", event))
    result2 = asyncio.run(engine.process("tenant-a", event))

    assert result1.graph_applied is True
    assert result2.graph_applied is True
    assert graph.edge_weights[("company", "entity-1")] == 1.0


# ── RU-006 / CEG-COR-015 (RG-008): one outcome record schema ────────────────
#
# The sync ``outcome`` entity type wrote ``:Outcome`` nodes with
# positive|negative|neutral and a HAS_OUTCOME edge, while handle_outcomes wrote
# the domain's ``feedbackloop.outcome_node_label`` with success|failure|partial
# and RESULTED_IN. With ``outcome_schema_unified`` both go through one writer.


_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


class _OutcomeGraph:
    """Captures every Cypher statement; answers reads with the written outcome nodes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.nodes: list[dict[str, Any]] = []

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        parameters = parameters or {}
        if "CREATE (o:" in cypher or cypher.lstrip().startswith("MATCH (o:"):
            self.calls.append((cypher, parameters))
        else:
            # Downstream feedback-loop statements (signal weights, propagation).
            return []
        if "CREATE (o:" in cypher:
            label = cypher.split("CREATE (o:", 1)[1].split(" ", 1)[0]
            self.nodes.append({"label": label, **parameters})
            return [{"outcome_id": parameters["outcome_id"]}]
        if cypher.lstrip().startswith("MATCH (o:"):
            return [
                {
                    "outcome": n["outcome"],
                    "candidate_id": n["candidate_id"],
                    "match_id": n["match_id"],
                    "value": n.get("value"),
                    "dimension_scores": n.get("dimension_scores"),
                    "was_selected": n.get("was_selected"),
                }
                for n in self.nodes
            ]
        return []


def _plasticos() -> DomainSpec:
    from engine.config.loader import DomainPackLoader as _Loader

    return _Loader(config_path=str(_DOMAINS)).load_domain("plasticos")


def _wire(spec: DomainSpec) -> _OutcomeGraph:
    graph = _OutcomeGraph()
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [spec.domain.id]
    init_dependencies(graph, loader)  # type: ignore[arg-type]
    return graph


@pytest.fixture
def feedback_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feedback_enabled", True)
    monkeypatch.setattr(settings, "unvalidated_domain_packs_enabled", True)
    monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)
    # The post-write convergence loop (signal weights, propagation) is not the
    # schema under test; its own suites cover it.
    from engine.feedback.convergence import ConvergenceLoop

    monkeypatch.setattr(ConvergenceLoop, "on_outcome_recorded", AsyncMock(return_value={}))


async def _sync_outcomes(*values: str) -> dict[str, Any]:
    return await handle_sync(
        "plasticos",
        {
            "entity_type": "outcome",
            "batch": [
                {"match_id": f"m{i}", "chosen_candidate_id": f"F{i}", "outcome": v} for i, v in enumerate(values)
            ],
        },
    )


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-015")
class TestUnifiedOutcomeSchema:
    async def test_sync_outcome_writes_spec_label_and_canonical_vocabulary(self, feedback_on: None) -> None:
        graph = _wire(_plasticos())
        result = await _sync_outcomes("positive", "negative", "neutral")
        assert result == {"status": "success", "entity_type": "outcome", "synced_count": 3}
        assert [(n["label"], n["outcome"]) for n in graph.nodes] == [
            ("TransactionOutcome", "success"),
            ("TransactionOutcome", "failure"),
            ("TransactionOutcome", "partial"),
        ]
        cypher = graph.calls[0][0]
        assert "CREATE (c)-[:RESULTED_IN]->(o)" in cypher
        assert "HAS_OUTCOME" not in cypher
        assert "(c:Facility AND c.facility_id = $candidate_id)" in cypher

    async def test_every_writer_produces_one_label_and_vocabulary(self, feedback_on: None) -> None:
        """Repro A/a04: distinct (label, outcome) pairs come from one schema only."""
        graph = _wire(_plasticos())
        await _sync_outcomes("positive", "negative", "neutral", "success", "bogus")
        for value in ("success", "failure", "partial"):
            await handle_outcomes("plasticos", {"match_id": "mx", "candidate_id": "F9", "outcome": value})
        pairs = {(n["label"], n["outcome"]) for n in graph.nodes}
        assert pairs == {
            ("TransactionOutcome", "success"),
            ("TransactionOutcome", "failure"),
            ("TransactionOutcome", "partial"),
        }
        # The sync and outcomes writers emit the same statement shape.
        sync_cypher = graph.calls[0][0]
        outcomes_cypher = graph.calls[-1][0]
        assert sync_cypher == outcomes_cypher

    async def test_outcomes_persist_dimension_scores_and_selection(self, feedback_on: None) -> None:
        graph = _wire(_plasticos())
        await handle_outcomes(
            "plasticos",
            {
                "match_id": "m1",
                "candidate_id": "F1",
                "outcome": "success",
                "fingerprint": {"dimension_scores": {"geo": 0.9, "price": 0.4}, "was_selected": True},
            },
        )
        cypher, params = graph.calls[0]
        assert "dimension_scores: $dimension_scores" in cypher
        assert "was_selected: $was_selected" in cypher
        assert json.loads(params["dimension_scores"]) == {"geo": 0.9, "price": 0.4}
        assert params["was_selected"] is True

    async def test_outcomes_without_scores_store_null(self, feedback_on: None) -> None:
        graph = _wire(_plasticos())
        await handle_outcomes("plasticos", {"match_id": "m1", "candidate_id": "F1", "outcome": "failure"})
        _cypher, params = graph.calls[0]
        assert params["dimension_scores"] is None
        assert params["was_selected"] is None

    @pytest.mark.parametrize(
        "fingerprint",
        [
            {"dimension_scores": [0.1]},
            {"dimension_scores": {"geo": "high"}},
            {"dimension_scores": {"geo": True}},
            {"dimension_scores": {"geo": float("nan")}},
            {"was_selected": "yes"},
        ],
    )
    async def test_malformed_feedback_fields_rejected(self, feedback_on: None, fingerprint: dict[str, Any]) -> None:
        graph = _wire(_plasticos())
        with pytest.raises(ValidationError, match="fingerprint"):
            await handle_outcomes(
                "plasticos", {"match_id": "m1", "candidate_id": "F1", "outcome": "success", "fingerprint": fingerprint}
            )
        assert graph.calls == []

    async def test_written_records_feed_score_feedback(self, feedback_on: None) -> None:
        """12 outcomes with dimension_scores -> non-empty proposed_weights."""
        graph = _wire(_plasticos())
        for i in range(6):
            await handle_outcomes(
                "plasticos",
                {
                    "match_id": f"s{i}",
                    "candidate_id": "F1",
                    "outcome": "success",
                    "fingerprint": {"dimension_scores": {"geo": 0.9}},
                },
            )
            await handle_outcomes(
                "plasticos",
                {
                    "match_id": f"f{i}",
                    "candidate_id": "F2",
                    "outcome": "failure",
                    "fingerprint": {"dimension_scores": {"geo": 0.2}},
                },
            )
        result = await handle_admin("plasticos", {"subaction": "score_feedback", "domain_id": "plasticos"})
        assert result["status"] == "feedback_computed"
        assert "o.dimension_scores AS dimension_scores" in graph.calls[-1][0]
        assert result["proposed_weights"]["geo"] > 0

    async def test_written_records_feed_discover_weights(
        self, feedback_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """discover_weights reads the dimension_scores / was_selected the writer persists."""
        monkeypatch.setattr(settings, "pareto_weight_discovery_enabled", True)
        from engine.config.schema import DecisionArbitrationSpec, ParetoConfigSpec, ParetoObjectiveSpec

        arbitration = DecisionArbitrationSpec(
            enabled=True,
            pareto_config=ParetoConfigSpec(objectives=[ParetoObjectiveSpec(dimension="revenue")]),
        )
        spec = _plasticos().model_copy(update={"decision_arbitration": arbitration})
        graph = _wire(spec)
        for i in range(10):
            await handle_outcomes(
                "plasticos",
                {
                    "match_id": f"m{i}",
                    "candidate_id": "F1",
                    "outcome": "success",
                    "fingerprint": {"dimension_scores": {"revenue": 0.1 * i}, "was_selected": i % 2 == 0},
                },
            )
        discover = AsyncMock(return_value=[])
        with patch("engine.scoring.weight_discovery.adaptive_weight_discovery", discover):
            result = await handle_admin("plasticos", {"subaction": "discover_weights", "domain_id": "plasticos"})
        assert result["outcome_history_size"] == 10
        history = discover.await_args.kwargs["outcome_history"]
        assert history[2] == {"dimension_scores": {"revenue": pytest.approx(0.2)}, "was_selected": True}
        assert history[3]["was_selected"] is False


@pytest.mark.unit
class TestUnifiedOutcomeSchemaOff:
    """outcome_schema_unified=False restores the three legacy schemas."""

    async def test_off_sync_writes_legacy_outcome_node(
        self, feedback_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "outcome_schema_unified", False)
        graph = _wire(_plasticos())
        await _sync_outcomes("positive")
        cypher, params = graph.calls[0]
        assert "CREATE (o:Outcome {" in cypher
        assert "HAS_OUTCOME" in cypher
        assert params["outcome"] == "positive"

    async def test_off_outcomes_cypher_has_no_feedback_fields(
        self, feedback_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "outcome_schema_unified", False)
        graph = _wire(_plasticos())
        await handle_outcomes(
            "plasticos",
            {
                "match_id": "m1",
                "candidate_id": "F1",
                "outcome": "success",
                "fingerprint": {"dimension_scores": {"geo": 0.9}},
            },
        )
        cypher, params = graph.calls[0]
        assert "dimension_scores" not in cypher
        assert "dimension_scores" not in params
        assert "was_selected" not in params

    async def test_off_score_feedback_does_not_project_scores(
        self, feedback_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "outcome_schema_unified", False)
        graph = _wire(_plasticos())
        result = await handle_admin("plasticos", {"subaction": "score_feedback", "domain_id": "plasticos"})
        assert result["status"] == "feedback_computed"
        assert "dimension_scores" not in graph.calls[-1][0]
