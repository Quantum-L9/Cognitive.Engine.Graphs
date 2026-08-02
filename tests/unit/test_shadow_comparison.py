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
