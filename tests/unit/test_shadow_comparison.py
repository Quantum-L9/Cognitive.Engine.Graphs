"""Unit tests for observational shadow comparison (TASK-055)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.shadow.compare import RankedCandidate, emit_shadow_comparison


@pytest.mark.unit
def test_emit_identical_rankings_no_mismatches() -> None:
    rows = [
        RankedCandidate("a", 0.9, 1),
        RankedCandidate("b", 0.5, 2),
    ]
    result = emit_shadow_comparison(packet_id="pkt-1", primary=rows, shadow=list(rows))
    assert result.observational is True
    assert result.replaces_primary is False
    assert result.mismatches == []
    d1 = result.to_dict()
    d2 = emit_shadow_comparison(packet_id="pkt-1", primary=rows, shadow=list(rows)).to_dict()
    assert d1["checksum"] == d2["checksum"]


@pytest.mark.unit
def test_rank_score_missing_extra_classes() -> None:
    primary = [
        RankedCandidate("a", 0.9, 1),
        RankedCandidate("b", 0.5, 2),
        RankedCandidate("c", 0.1, 3),
    ]
    shadow = [
        RankedCandidate("b", 0.8, 1),  # rank+score vs primary
        RankedCandidate("a", 0.9, 2),  # rank vs primary
        RankedCandidate("d", 0.05, 3),  # extra
    ]
    result = emit_shadow_comparison(packet_id="pkt-2", primary=primary, shadow=shadow)
    classes = {m.mismatch_class for m in result.mismatches}
    assert "rank" in classes
    assert "score" in classes
    assert "missing" in classes  # c
    assert "extra" in classes  # d
    assert result.replaces_primary is False


@pytest.mark.unit
def test_cli_writes_deterministic_artifact(tmp_path: Path) -> None:
    from tools.shadow_comparison import main

    inp = tmp_path / "in.json"
    out = tmp_path / "out.json"
    inp.write_text(
        json.dumps(
            {
                "packet_id": "pkt-cli",
                "primary": [{"candidate_id": "x", "score": 1.0, "rank": 1}],
                "shadow": [{"candidate_id": "x", "score": 1.0, "rank": 1}],
            }
        )
    )
    rc = main(["--input", str(inp), "--output", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["observational"] is True
    assert data["replaces_primary"] is False
    assert data["checksum"].startswith("sha256:")


@pytest.mark.unit
def test_cli_rejects_non_numeric_score(tmp_path: Path) -> None:
    from tools.shadow_comparison import main

    inp = tmp_path / "in.json"
    inp.write_text(
        json.dumps(
            {
                "packet_id": "pkt-bad",
                "primary": [{"candidate_id": "x", "score": "high", "rank": 1}],
                "shadow": [],
            }
        )
    )
    with pytest.raises(ValueError, match="ranked item 0: score and rank must be numeric"):
        main(["--input", str(inp), "--output", str(tmp_path / "out.json")])


# ── RU-105 / CEG-COR-128: non-finite scores and duplicate ids are reported ──


@pytest.mark.unit
@pytest.mark.parametrize(
    ("primary_score", "shadow_score"),
    [
        (0.9, float("nan")),
        (float("nan"), 0.9),
        (float("nan"), float("nan")),
        (0.9, float("inf")),
        (float("-inf"), float("-inf")),
    ],
)
def test_non_finite_score_is_a_score_mismatch(primary_score: float, shadow_score: float) -> None:
    result = emit_shadow_comparison(
        packet_id="pkt-nan",
        primary=[RankedCandidate("a", primary_score, 1)],
        shadow=[RankedCandidate("a", shadow_score, 1)],
    )
    assert [m.mismatch_class for m in result.mismatches] == ["score"]


@pytest.mark.unit
def test_duplicate_candidate_id_is_reported_per_extra_occurrence() -> None:
    primary = [
        RankedCandidate("a", 0.9, 1),
        RankedCandidate("a", 0.4, 2),
        RankedCandidate("a", 0.3, 3),
        RankedCandidate("b", 0.2, 4),
    ]
    shadow = [RankedCandidate("a", 0.9, 1), RankedCandidate("b", 0.2, 2), RankedCandidate("b", 0.1, 3)]
    result = emit_shadow_comparison(packet_id="pkt-dup", primary=primary, shadow=shadow)
    duplicates = [m for m in result.mismatches if m.mismatch_class == "duplicate"]
    assert [(m.candidate_id, m.primary_rank, m.shadow_rank) for m in duplicates] == [
        ("a", 2, None),
        ("a", 3, None),
        ("b", None, 3),
    ]
    # The first (best-ranked) occurrence is the one compared: a@1 vs a@1 agrees.
    assert not any(m.candidate_id == "a" and m.mismatch_class in {"rank", "score"} for m in result.mismatches)
    again = emit_shadow_comparison(packet_id="pkt-dup", primary=primary, shadow=shadow)
    assert result.to_dict()["checksum"] == again.to_dict()["checksum"]
