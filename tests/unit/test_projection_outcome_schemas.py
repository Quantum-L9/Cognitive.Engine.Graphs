"""TASK-061: canonical-projection + outcome-feedback schema producer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.models.payloads import (
    CanonicalProjection,
    OutcomeFeedback,
    OutcomeFeedbackStore,
)

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = ROOT / "contracts" / "payloads"
EXAMPLES = PAYLOADS / "examples"
NEGATIVES = PAYLOADS / "negative_examples"

SCHEMA_FILES = (
    "common.schema.yaml",
    "canonical-projection.schema.yaml",
    "outcome-feedback.schema.yaml",
    "sync-projection.schema.yaml",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_schema_files_exist() -> None:
    for name in SCHEMA_FILES:
        assert (PAYLOADS / name).is_file(), name


@pytest.mark.unit
def test_canonical_projection_fixture_validates() -> None:
    CanonicalProjection.model_validate(_load(EXAMPLES / "canonical-projection.json"))


@pytest.mark.unit
def test_outcome_feedback_fixture_validates() -> None:
    OutcomeFeedback.model_validate(_load(EXAMPLES / "outcome-feedback.json"))


@pytest.mark.unit
def test_canonical_rejects_transport_field() -> None:
    data = _load(NEGATIVES / "canonical-projection-transport-field.json")
    with pytest.raises(ValidationError):
        CanonicalProjection.model_validate(data)


@pytest.mark.unit
def test_outcome_rejects_unknown_type() -> None:
    data = _load(NEGATIVES / "outcome-feedback-unknown-type.json")
    with pytest.raises(ValidationError):
        OutcomeFeedback.model_validate(data)


@pytest.mark.unit
def test_outcome_feedback_idempotent_replay() -> None:
    feedback = OutcomeFeedback.model_validate(_load(EXAMPLES / "outcome-feedback.json"))
    store = OutcomeFeedbackStore()
    first = store.apply(feedback)
    assert first.accepted == ["match-501-accepted-20260802"]
    assert first.reused == []
    second = store.apply(feedback)
    assert second.accepted == []
    assert second.reused == ["match-501-accepted-20260802"]
    assert store.get("match-501-accepted-20260802") is not None
