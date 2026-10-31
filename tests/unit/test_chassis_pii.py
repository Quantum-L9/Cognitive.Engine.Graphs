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


@pytest.mark.unit
class TestChassisPIIEmptyKeyPaths:
    """RU-071 follow-up: empty key components stay addressable through string paths."""

    def test_top_level_empty_key_round_trips(self) -> None:
        payload = {"": "secret@example.com", "id": 1}
        handler = PIIHandler()
        paths = list(handler.get_pii_paths(payload))
        assert paths == ['""']
        assert handler.mask(payload, fields=paths) == {"": "************", "id": 1}
        assert handler.redact(payload, fields=paths) == {"id": 1}

    def test_nested_empty_key_does_not_resolve_to_parent(self) -> None:
        payload = {"outer": {"": "secret@example.com", "keep": 1}}
        handler = PIIHandler()
        paths = list(handler.get_pii_paths(payload))
        assert paths == ['outer.""']
        assert handler.mask(payload, fields=paths) == {"outer": {"": "************", "keep": 1}}
        assert handler.redact(payload, fields=paths) == {"outer": {"keep": 1}}

    def test_empty_key_holding_list_is_not_a_list_root(self) -> None:
        payload = {"": ["x@y.com"]}
        handler = PIIHandler()
        paths = list(handler.get_pii_paths(payload))
        assert paths == ['""[0]']
        assert handler.redact(payload, fields=paths) == {"": []}

    def test_literal_quote_keys_are_escaped(self) -> None:
        payload = {'""': "a@b.com", '"': "c@d.com", "": "e@f.com"}
        handler = PIIHandler()
        paths = list(handler.get_pii_paths(payload))
        assert paths == ['\\"\\"', '\\"', '""']
        assert handler.redact(payload, fields=paths[:1]) == {'"': "c@d.com", "": "e@f.com"}
        assert handler.redact(payload, fields=paths[2:]) == {'""': "a@b.com", '"': "c@d.com"}

    @pytest.mark.parametrize("path", [".email", "outer.", "a..b", "a.[0]"])
    def test_empty_component_paths_rejected(self, path: str) -> None:
        with pytest.raises(ValueError, match="empty key component"):
            PIIHandler().mask({"outer": {"email": "a@b.com"}}, fields=[path])
