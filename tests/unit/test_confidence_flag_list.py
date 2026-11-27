"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, confidence, match]
owner: engine-team
status: active
--- /L9_META ---

SCR-13 remainder (ceg-scoring-audit L10, operator decision 2026-11-27).

``confidence_flag`` was a bare string for one flag, a list for two, and absent
for none, so a consumer had to branch on its type. Under
``confidence_flag_list_enabled`` both producers (``ConfidenceChecker.annotate_candidates``
and ``engine.handlers._annotate_weighted_confidence``, the weighted-monoculture
path) emit ``list[str]`` on every candidate they annotate: ``[]``, ``[x]`` or
``[x, y]``, monoculture before low dimensional agreement. Flag vocabulary and
``confidence_detail`` are unchanged; ``confidence_detail`` still appears only
when a flag does. With ``confidence_check_enabled`` off nothing is evaluated
and the key stays absent (an ``[]`` would claim a clean check).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.settings import settings
from engine.handlers import handle_match, init_dependencies
from engine.scoring.confidence import (
    FLAG_LOW_DIMENSIONAL_AGREEMENT,
    FLAG_MONOCULTURE,
    ConfidenceChecker,
)
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"

# Raw dimension values; the orbital weights are 0.6 / 0.3.
CLEAN = {"depot_reliability": 0.5, "port_capacity": 0.5}  # share 0.667, agreeing
MONO = {"depot_reliability": 0.9, "port_capacity": 0.5}  # share 0.78, agreeing
BOTH = {"depot_reliability": 0.9, "port_capacity": 0.16}  # share 0.92, disagreeing

EXPECTED = {"clean": [], "mono": [FLAG_MONOCULTURE], "both": [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT]}


def _checker() -> ConfidenceChecker:
    return ConfidenceChecker(monoculture_threshold=0.70)


class TestShippedDefault:
    def test_flag_ships_on(self) -> None:
        assert settings.confidence_flag_list_enabled is True


class TestAnnotateCandidates:
    """The unweighted producer (``monoculture_weighted_contribution`` off)."""

    def test_zero_one_and_two_flags_are_lists(self) -> None:
        rows = [
            {"dimension_scores": {"geo": 0.4, "structural": 0.6}},
            {"dimension_scores": {"geo": 0.85, "structural": 0.35}},
            {"dimension_scores": {"geo": 0.95, "structural": 0.05}},
        ]
        _checker().annotate_candidates(rows)
        assert [r["confidence_flag"] for r in rows] == [
            [],
            [FLAG_MONOCULTURE],
            [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT],
        ]
        assert "confidence_detail" not in rows[0]
        assert rows[2]["confidence_detail"]["dominant_dimension"] == "geo"

    def test_every_value_is_a_list_of_strings(self) -> None:
        rows = [{"dimension_scores": {"a": v, "b": 1 - v}} for v in (0.1, 0.3, 0.5, 0.7, 0.9)]
        _checker().annotate_candidates(rows)
        for row in rows:
            assert isinstance(row["confidence_flag"], list)
            assert all(isinstance(f, str) for f in row["confidence_flag"])

    def test_flag_off_restores_string_list_or_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
        rows = [
            {"dimension_scores": {"geo": 0.4, "structural": 0.6}},
            {"dimension_scores": {"geo": 0.85, "structural": 0.35}},
            {"dimension_scores": {"geo": 0.95, "structural": 0.05}},
        ]
        _checker().annotate_candidates(rows)
        assert "confidence_flag" not in rows[0]
        assert rows[1]["confidence_flag"] == FLAG_MONOCULTURE
        assert rows[2]["confidence_flag"] == [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT]


class _Driver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        return [
            {"candidate": {"depot_id": name}, "score": 0.5, "dimension_scores": dict(dims), "absent_sources": []}
            for name, dims in self.rows
        ]


async def _match_flags() -> dict[str, Any]:
    loader = MagicMock()
    loader.load_domain.return_value = synthetic_loader().load_domain(DOMAIN)
    loader.list_domains.return_value = [DOMAIN]
    init_dependencies(_Driver([("clean", CLEAN), ("mono", MONO), ("both", BOTH)]), loader)  # type: ignore[arg-type]
    result = await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": 10})
    return {c["candidate"]["depot_id"]: c for c in result["candidates"]}


class TestHandleMatchProducers:
    @pytest.mark.parametrize("weighted", [True, False], ids=["weighted", "unweighted"])
    async def test_both_producers_agree_on_the_list_shape(
        self, weighted: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "monoculture_weighted_contribution", weighted)
        rows = await _match_flags()
        assert set(rows) == set(EXPECTED)
        for name, row in rows.items():
            assert isinstance(row["confidence_flag"], list)
            if weighted:
                assert row["confidence_flag"] == EXPECTED[name]

    async def test_weighted_flag_off_restores_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
        rows = await _match_flags()
        assert "confidence_flag" not in rows["clean"]
        assert rows["mono"]["confidence_flag"] == FLAG_MONOCULTURE
        assert rows["both"]["confidence_flag"] == [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT]

    async def test_confidence_check_off_leaves_the_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "confidence_check_enabled", False)
        rows = await _match_flags()
        assert all("confidence_flag" not in row for row in rows.values())
