"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, fixtures, match, contract]
owner: engine-team
status: active
--- /L9_META ---

Compare a live ``handle_match`` payload with the draft-unpublished response schema.

The checked-in example fixture only proves the schema file is internally usable.
It is not a published interface and it is not the subject of the proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_CEG = Path(__file__).resolve().parents[2]
_PAYLOADS = _CEG / "contracts" / "payloads"
_SCHEMA = _PAYLOADS / "match-response.schema.yaml"
_OPPORTUNITY = _CEG.parent / "odoo" / "contracts" / "schemas" / "examples" / "supply-opportunity.json"
_REQUEST_FIXTURE = _PAYLOADS / "examples" / "match-request.json"

UNPUBLISHED_REQUIRED = (
    "contract_version",
    "domain",
    "domain_spec_version",
    "model_version",
)


def schema_text() -> str:
    """The schema file as stored. Callers must not rewrite it."""
    return _SCHEMA.read_text(encoding="utf-8")


def frozen_opportunity_quantity() -> dict[str, Any]:
    """The approved supply-opportunity example. Odoo owns this file; it is read only."""
    payload = json.loads(_OPPORTUNITY.read_text(encoding="utf-8"))
    quantity = payload["quantity"]
    if not isinstance(quantity, dict):
        msg = "frozen supply opportunity has no quantity object"
        raise TypeError(msg)
    return quantity


def diagnostic_request_quantity_lbs() -> float:
    """The draft match-request fixture's quantity, used only to compare with the frozen payload."""
    payload = json.loads(_REQUEST_FIXTURE.read_text(encoding="utf-8"))
    return float(payload["query"]["facts"]["quantity_lbs"])


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"schema root must be an object: {path}"
        raise TypeError(msg)
    return data


def _retrieve(uri: str) -> Resource[Any]:
    path = Path(urlparse(uri).path)
    if not path.is_file():
        msg = f"unresolvable schema ref: {uri}"
        raise FileNotFoundError(msg)
    return Resource.from_contents(_load_yaml(path))


def draft_response_validator() -> Draft202012Validator:
    """A validator for the draft schema. The file's ``$id`` and publication marker stay put.

    Relative ``./common.schema.yaml`` refs resolve against the file URI of an in-memory
    copy. That copy is not written back.
    """
    schema = _load_yaml(_SCHEMA)
    schema["$id"] = _SCHEMA.resolve().as_uri()
    registry = Registry(retrieve=_retrieve)
    return Draft202012Validator(schema, registry=registry)


def schema_messages(payload: dict[str, Any]) -> list[str]:
    return [error.message for error in draft_response_validator().iter_errors(payload)]


def example_response_matches_draft_schema() -> bool:
    """The checked-in example is a diagnostic of the schema file, not a live payload."""
    example = _PAYLOADS / "examples" / "match-response.json"
    return bool(draft_response_validator().is_valid(json.loads(example.read_text(encoding="utf-8"))))
