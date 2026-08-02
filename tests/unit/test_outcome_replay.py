"""Unit tests for deterministic CEG outcome replay (TASK-033)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.replay.outcome import (
    ODOO_INPUT_SCHEMA,
    ReplayError,
    replay_outcomes,
)

ROOT = Path(__file__).resolve().parents[2]


def _fixture() -> dict:
    return {
        "schema": ODOO_INPUT_SCHEMA,
        "schema_version": "1.0.0",
        "producer": "odoo.outcome_replay_export",
        "producer_task": "TASK-057",
        "gate_mutation": False,
        "event_count": 2,
        "events": [
            {
                "tenant": "plasticos",
                "action": "match",
                "packet_id": "pkt-b",
                "payload": {"score": 0.2},
            },
            {
                "tenant": "plasticos",
                "action": "match",
                "packet_id": "pkt-a",
                "payload": {"score": 0.9},
            },
        ],
        "content_hash": "sha256:fixture",
    }


@pytest.mark.unit
def test_replay_deterministic_across_two_runs() -> None:
    a = replay_outcomes(_fixture())
    b = replay_outcomes(_fixture())
    assert a == b
    assert a["network"] is False
    assert a["gate_calls"] == 0
    assert a["outcome_set_hash"].startswith("sha256:")
    assert len(a["outcomes"]) == 2


@pytest.mark.unit
def test_event_order_does_not_change_hash() -> None:
    doc = _fixture()
    doc["events"] = list(reversed(doc["events"]))
    assert replay_outcomes(doc)["outcome_set_hash"] == replay_outcomes(_fixture())["outcome_set_hash"]


@pytest.mark.unit
def test_rejects_gate_mutation_and_wrong_schema() -> None:
    bad = _fixture()
    bad["gate_mutation"] = True
    with pytest.raises(ReplayError):
        replay_outcomes(bad)
    bad2 = _fixture()
    bad2["schema"] = "other"
    with pytest.raises(ReplayError):
        replay_outcomes(bad2)


@pytest.mark.unit
def test_cli_two_runs_identical(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ceg_outcome_replay_cli", ROOT / "tools" / "outcome_replay.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    inp = tmp_path / "in.json"
    out1 = tmp_path / "o1.json"
    out2 = tmp_path / "o2.json"
    inp.write_text(json.dumps(_fixture()), encoding="utf-8")
    for dest in (out1, out2):
        assert mod.main(["--input", str(inp), "--output", str(dest)]) == 0
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")


@pytest.mark.unit
def test_source_has_no_network_imports() -> None:
    src = (ROOT / "engine" / "replay" / "outcome.py").read_text(encoding="utf-8")
    assert "neo4j" not in src.lower()
    assert "httpx" not in src
    assert "GateClient" not in src
    assert "requests" not in src
