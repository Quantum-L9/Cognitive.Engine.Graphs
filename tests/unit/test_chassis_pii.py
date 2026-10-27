"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [test]
tags: [test, unit, chassis, pii]
owner: platform-team
status: active
--- /L9_META ---

RU-071 / CEG-COR-089, CEG-COR-090 (RG-092): chassis/pii.py PIIHandler is
non-mutating, list-aware, flags hinted keys whatever their value type, and
addresses every detected field unambiguously.
"""

from __future__ import annotations

import copy

import pytest

from chassis.pii import PIIHandler


@pytest.mark.unit
class TestChassisPIIHandler:
    def test_mask_does_not_mutate_nested_input(self) -> None:
        payload = {"customer": {"email": "a@b.com", "tier": "gold"}, "id": 7}
        before = copy.deepcopy(payload)
        out = PIIHandler().mask(payload)
        assert payload == before
        assert out["customer"]["email"] == "*******"
        assert out["customer"]["tier"] == "gold"

    def test_redact_does_not_mutate_nested_input(self) -> None:
        payload = {"customer": {"ssn": "123-45-6789", "tier": "gold"}}
        before = copy.deepcopy(payload)
        out = PIIHandler().redact(payload)
        assert payload == before
        assert out == {"customer": {"tier": "gold"}}

    def test_list_payload_detected_and_masked(self) -> None:
        payload = {"contacts": [{"email": "x@y.com"}, {"ssn": "123-45-6789"}, {"phone": "555-123-4567"}]}
        handler = PIIHandler()
        paths = handler.get_pii_paths(payload)
        assert paths == ("contacts[0].email", "contacts[1].ssn", "contacts[2].phone")
        before = copy.deepcopy(payload)
        out = handler.mask(payload)
        assert payload == before
        assert "x@y.com" not in str(out)
        assert "123-45-6789" not in str(out)
        assert "555-123-4567" not in str(out)

    def test_top_level_list_payload(self) -> None:
        payload = [{"email": "x@y.com"}, "SSN 123-45-6789"]
        handler = PIIHandler()
        assert handler.get_pii_paths(payload) == ("[0].email", "[1]")
        assert "x@y.com" not in str(handler.mask(payload))

    def test_dotted_key_is_detected_and_masked(self) -> None:
        payload = {"user.email": "a@b.com", "user": {"name": "n"}}
        handler = PIIHandler()
        (path,) = handler.get_pii_paths(payload)
        out = handler.mask(payload)
        assert out["user.email"] == "*******"
        assert out["user"] == {"name": "n"}
        assert handler.redact(payload, [path]) == {"user": {"name": "n"}}

    def test_dict_valued_hint_key_is_flagged(self) -> None:
        payload = {"address": {"line1": "1 Main", "zip": "02139"}, "id": 1}
        handler = PIIHandler()
        assert handler.get_pii_paths(payload) == ("address",)
        assert handler.redact(payload) == {"id": 1}

    def test_simple_nested_paths_unchanged(self) -> None:
        payload = {"patient": {"first_name": "John", "contact": {"phone": "555-123-4567"}}}
        assert PIIHandler().get_pii_paths(payload) == ("patient.first_name", "patient.contact.phone")

    def test_mask_format_is_capped_at_twelve(self) -> None:
        out = PIIHandler().mask({"email": "averyveryverylongaddress@example.com"})
        assert out["email"] == "*" * 12
