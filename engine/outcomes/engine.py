"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [scoring]
tags: [intelligence, feedback]
status: active
--- /L9_META ---
"""

from __future__ import annotations

from engine.graph.driver import GraphDriver
from engine.outcomes.schema import OutcomeEvent, ReinforcementResult
from engine.scoring.assembler import ScoringAssembler


class OutcomeEngine:
    def __init__(self, graph_driver: GraphDriver, scoring_assembler: ScoringAssembler):
        self.graph_driver = graph_driver
        self.scoring_assembler = scoring_assembler

    async def process(self, tenant: str, event: OutcomeEvent) -> ReinforcementResult:
        # NOTE: apply_outcome_edge_update/apply_outcome_feedback are not (yet)
        # implemented on GraphDriver/ScoringAssembler — this path is dormant.
        # tests/unit/test_outcomes.py skips exercising it for the same reason.
        graph_record = self.graph_driver.apply_outcome_edge_update(  # type: ignore[attr-defined]
            event_id=event.event_id,
            entity_id=event.entity_id,
            outcome_state=event.outcome_state,
            canonical_label=event.canonical_label,
        )
        adjustment = self.scoring_assembler.apply_outcome_feedback(  # type: ignore[attr-defined]
            entity_id=event.entity_id,
            outcome_state=event.outcome_state,
            event_id=event.event_id,
        )
        return ReinforcementResult(
            event_id=event.event_id,
            graph_applied=graph_record.applied,
            scoring_adjustment=adjustment,
        )
