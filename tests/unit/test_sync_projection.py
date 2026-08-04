"""TASK-028: sync-projection contract + tombstone/revision idempotency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.models.payloads import SyncProjection
from engine.sync.projection import ProjectionStore

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = ROOT / "contracts" / "payloads"
EXAMPLES = PAYLOADS / "examples"
NEGATIVES = PAYLOADS / "negative_examples"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.unit
def test_sync_projection_fixture_validates() -> None:
    SyncProjection.model_validate(_load(EXAMPLES / "sync-projection.json"))


@pytest.mark.unit
def test_tombstone_fixture_validates() -> None:
    SyncProjection.model_validate(_load(EXAMPLES / "sync-projection-tombstone.json"))


@pytest.mark.unit
def test_unknown_operation_rejected() -> None:
    data = _load(NEGATIVES / "sync-projection-unknown-operation.json")
    with pytest.raises(ValidationError):
        SyncProjection.model_validate(data)


@pytest.mark.unit
def test_transport_fields_forbidden() -> None:
    payload = _load(EXAMPLES / "sync-projection.json")
    payload["correlation_id"] = "nope"
    with pytest.raises(ValidationError):
        SyncProjection.model_validate(payload)


@pytest.mark.unit
def test_upsert_tombstone_revision_idempotent_and_rebuild() -> None:
    store = ProjectionStore()
    upsert = SyncProjection.model_validate(_load(EXAMPLES / "sync-projection.json"))
    first = store.apply(upsert)
    assert first.accepted == ["res.partner:102"]
    assert store.get("res.partner:102") is not None
    assert store.get("res.partner:102").tombstoned is False

    second = store.apply(upsert)
    assert second.reused == ["res.partner:102"]
    assert second.accepted == []

    stale = SyncProjection.model_validate(
        {
            "contract_version": "1.0.0-draft",
            "domain": "plasticos",
            "projection_version": "1.0.0",
            "records": [
                {
                    **upsert.records[0].model_dump(),
                    "revision": 1,
                    "source_record": {**upsert.records[0].source_record.model_dump(), "revision": 1},
                }
            ],
        }
    )
    stale_result = store.apply(stale)
    assert stale_result.rejected[0]["reason"] == "stale_revision"

    tomb = SyncProjection.model_validate(_load(EXAMPLES / "sync-projection-tombstone.json"))
    tomb_result = store.apply(tomb)
    assert tomb_result.accepted == ["res.partner:102"]
    assert store.get("res.partner:102").tombstoned is True

    # rebuild proof from authority stream
    rebuilt = ProjectionStore()
    rebuilt.rebuild_from([*upsert.records, *tomb.records])
    assert rebuilt.get("res.partner:102").tombstoned is True
    assert rebuilt.get("res.partner:102").revision == 9
