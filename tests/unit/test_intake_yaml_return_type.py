"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, intake]
owner: engine-team
status: active
--- /L9_META ---

F006: the YAML return-type correction must not change serialization.
"""

from __future__ import annotations

import pytest
import yaml

from engine.config.schema import DomainMetadata, DomainSpec
from engine.intake import intake_compiler
from engine.intake.intake_schema import FieldMapping, FieldOrigin

pytestmark = pytest.mark.unit


# Frozen from engine/intake/intake_compiler.py at
# f8d933d83a11244a2ada79c7f0d0dea74fbbe6ce; only the helper name is changed.
def _pinned_original_build_domain_yaml(
    mappings: list[FieldMapping],
    spec: DomainSpec,
    gate_proposals: list[dict[str, str]],
    scoring_proposals: list[str],
) -> str:
    """Build a domain spec YAML string from compiled mappings."""
    nodes_by_label: dict[str, list[dict[str, object]]] = {}
    for mapping in mappings:
        label = mapping.node_label
        if label not in nodes_by_label:
            nodes_by_label[label] = []
        entry: dict[str, object] = {
            "name": mapping.canonical_name,
            "origin": mapping.origin.value,
        }
        if mapping.derived_from:
            entry["derived_from"] = mapping.derived_from
        if mapping.is_gate_critical:
            entry["gate_critical"] = True
        if mapping.scoring_weight > 0:
            entry["scoring_weight"] = mapping.scoring_weight
        nodes_by_label[label].append(entry)

    output: dict[str, object] = {
        "domain": {
            "id": spec.domain.id,
            "name": spec.domain.name,
            "version": spec.domain.version,
        },
        "intake_compilation": {
            "nodes": {label: {"fields": fields} for label, fields in nodes_by_label.items()},
        },
    }

    if gate_proposals:
        output["gate_proposals"] = gate_proposals
    if scoring_proposals:
        output["scoring_proposals"] = scoring_proposals

    return yaml.dump(output, default_flow_style=False, sort_keys=False)


@pytest.mark.parametrize(
    ("name", "include_fields", "include_proposals"),
    [
        ("Empty intake", False, False),
        ("Mapped intake", True, False),
        ("Full intake", True, True),
        ("Café: intake\n東京", True, True),
    ],
    ids=["empty", "fields-only", "full", "unicode-and-multiline"],
)
def test_yaml_matches_pinned_original_type_and_bytes(
    name: str,
    include_fields: bool,
    include_proposals: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This helper reads only domain metadata, not the rest of the schema.
    spec = DomainSpec.model_construct(domain=DomainMetadata(id="typed-intake", name=name, version="1.0.0"))
    mappings = (
        [
            FieldMapping(
                crm_field_name="source",
                canonical_name="z_field",
                origin=FieldOrigin.CUSTOMER_PROVIDED,
                node_label="ZNode",
                is_gate_critical=True,
                scoring_weight=0.25,
            ),
            FieldMapping(
                crm_field_name="derived",
                canonical_name="a_field",
                origin=FieldOrigin.L9_ENRICHABLE,
                node_label="ANode",
                derived_from=["z_field", "source: café\n東京"],
            ),
            FieldMapping(
                crm_field_name="vertical",
                canonical_name="another_field",
                origin=FieldOrigin.VERTICAL_STANDARD,
                node_label="ZNode",
                scoring_weight=-0.5,
            ),
        ]
        if include_fields
        else []
    )
    gates = [{"field": "z_field", "gate_type": "range"}] if include_proposals else []
    scoring = ["z_field", "a_field"] if include_proposals else []
    expected = _pinned_original_build_domain_yaml(mappings, spec, gates, scoring)
    original_dump = yaml.dump
    emitted: list[str] = []
    parameters: list[tuple[bool, bool]] = []

    def recording_dump(data: object, *, default_flow_style: bool, sort_keys: bool) -> str:
        serialized = original_dump(data, default_flow_style=default_flow_style, sort_keys=sort_keys)
        emitted.append(serialized)
        parameters.append((default_flow_style, sort_keys))
        return serialized

    monkeypatch.setattr(intake_compiler.yaml, "dump", recording_dump)
    actual = intake_compiler._build_domain_yaml(mappings, spec, gates, scoring)

    assert type(expected) is str
    assert type(actual) is str
    assert actual.encode("utf-8") == expected.encode("utf-8")
    assert actual is emitted[0]
    assert parameters == [(False, False)]


def test_yaml_return_object_is_not_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typing-only correction must preserve even a str-subclass object."""

    class DumpText(str):
        pass

    returned = DumpText("domain: {id: typed-intake}\n")
    spec = DomainSpec.model_construct(domain=DomainMetadata(id="typed-intake", name="Typed Intake", version="1.0.0"))

    def dump(data: object, *, default_flow_style: bool, sort_keys: bool) -> str:
        assert default_flow_style is False
        assert sort_keys is False
        return returned

    monkeypatch.setattr(intake_compiler.yaml, "dump", dump)
    assert intake_compiler._build_domain_yaml([], spec, [], []) is returned
