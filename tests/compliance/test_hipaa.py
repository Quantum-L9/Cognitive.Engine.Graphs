"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, compliance, hipaa]
owner: engine-team
status: active
--- /L9_META ---

HIPAA compliance tests (PII handling, prohibited factors).
Target Coverage: 95%+
"""

import pytest

from engine.compliance.pii import PIICategory, PIIHandler, PIISensitivity


@pytest.mark.compliance
@pytest.mark.unit
class TestPIIDetection:
    """Test PII detection capabilities."""

    def test_detect_email_by_field_name(self) -> None:
        """Email fields detected by field name hint."""
        handler = PIIHandler()
        payload = {"contact_email": "test@example.com", "facility_id": 42}

        detections = handler.detect_pii(payload)

        assert len(detections) == 1
        assert detections[0].field_path == "contact_email"
        assert detections[0].category == PIICategory.EMAIL
        assert detections[0].detected_by == "field_name"

    def test_detect_ssn_by_pattern(self) -> None:
        """SSN detected by regex pattern in value."""
        handler = PIIHandler()
        payload = {"notes": "SSN is 123-45-6789", "id": 1}

        detections = handler.detect_pii(payload)

        assert len(detections) == 1
        assert detections[0].category == PIICategory.SSN
        assert detections[0].detected_by == "pattern_match"

    def test_phone_pattern_does_not_match_inside_opaque_identifier(self) -> None:
        """A digit run inside a longer token is not a phone number.

        Regression: handle_match builds query_id as `q_<12 hex chars>`. Roughly
        1.6% of those contain a 10-digit run, which the unguarded phone pattern
        matched — ComplianceEngine.redact_response then deleted query_id from
        the match response for those requests.
        """
        handler = PIIHandler()

        for opaque in ("q_5c9382647927", "q_a90333567456", "q_6281353751e8"):
            assert handler.detect_pii({"query_id": opaque}) == [], f"{opaque} must not be treated as PII"

    def test_detect_phone_by_pattern_still_matches_real_numbers(self) -> None:
        """Guarding the phone pattern must not cost real phone detection."""
        handler = PIIHandler()

        for number in ("555-123-4567", "+1 (555) 123-4567", "5551234567", "call 555.123.4567 now"):
            detections = handler.detect_pii({"notes": number})
            assert [d.category for d in detections] == [PIICategory.PHONE], f"{number} must be detected as a phone"

    def test_detect_nested_pii(self) -> None:
        """PII detected in nested dicts."""
        handler = PIIHandler()
        payload = {
            "patient": {
                "first_name": "John",
                "contact": {"phone": "555-123-4567"},
            }
        }

        detections = handler.detect_pii(payload)
        paths = [d.field_path for d in detections]

        assert "patient.first_name" in paths
        assert "patient.contact.phone" in paths

    def test_get_pii_field_paths(self) -> None:
        """get_pii_field_paths returns tuple for TransportPacket."""
        handler = PIIHandler()
        payload = {"email": "a@b.com", "ssn": "111-22-3333", "id": 1}

        pii_fields = handler.get_pii_field_paths(payload)

        assert isinstance(pii_fields, tuple)
        assert "email" in pii_fields
        assert "ssn" in pii_fields


@pytest.mark.compliance
@pytest.mark.unit
class TestPIIMasking:
    """Test PII masking capabilities."""

    def test_mask_email(self) -> None:
        """Email masked with domain hint preserved."""
        handler = PIIHandler()
        payload = {"email": "john.doe@example.com"}

        masked = handler.mask_fields(payload, fields=["email"])

        assert masked["email"] != "john.doe@example.com"
        assert "@" in masked["email"]
        assert "com" in masked["email"]

    def test_mask_ssn(self) -> None:
        """SSN masked with last 4 visible."""
        handler = PIIHandler()
        payload = {"ssn": "123-45-6789"}

        masked = handler.mask_fields(payload, fields=["ssn"])

        assert masked["ssn"].endswith("6789")
        assert "*" in masked["ssn"]

    def test_mask_all_detected(self) -> None:
        """mask_all_detected auto-detects and masks PII."""
        handler = PIIHandler()
        payload = {"email": "test@test.com", "phone": "555-1234", "id": 42}

        masked = handler.mask_fields(payload, mask_all_detected=True)

        assert masked["email"] != "test@test.com"
        assert masked["id"] == 42  # Non-PII unchanged


@pytest.mark.compliance
@pytest.mark.unit
class TestPIIRedaction:
    """Test PII redaction (removal)."""

    def test_redact_fields(self) -> None:
        """Redact removes specified fields entirely."""
        handler = PIIHandler()
        payload = {"ssn": "123-45-6789", "name": "John", "age": 30}

        redacted = handler.redact(payload, fields=["ssn"])

        assert "ssn" not in redacted
        assert redacted["name"] == "John"
        assert redacted["age"] == 30

    def test_redact_all_detected(self) -> None:
        """redact_all_detected removes all PII fields."""
        handler = PIIHandler()
        payload = {"email": "a@b.com", "phone": "555-1234", "facility_id": 42}

        redacted = handler.redact(payload, redact_all_detected=True)

        assert "email" not in redacted
        assert "phone" not in redacted
        assert redacted["facility_id"] == 42


@pytest.mark.compliance
@pytest.mark.unit
class TestPIIHashing:
    """Test PII hashing for pseudonymization."""

    def test_hash_deterministic(self) -> None:
        """Same input produces same hash."""
        h1 = PIIHandler.hash_value("test@example.com")
        h2 = PIIHandler.hash_value("test@example.com")

        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex length

    def test_hash_with_salt(self) -> None:
        """Salt changes hash output."""
        h1 = PIIHandler.hash_value("test@example.com", salt="")
        h2 = PIIHandler.hash_value("test@example.com", salt="secret")

        assert h1 != h2


@pytest.mark.compliance
@pytest.mark.unit
class TestCustomPIIFields:
    """Test custom PII field registration."""

    def test_additional_pii_fields(self) -> None:
        """Custom PII fields are detected."""
        handler = PIIHandler(
            additional_pii_fields={
                "mrn": (PIICategory.CUSTOM, PIISensitivity.HIGH),
                "patient_id": (PIICategory.CUSTOM, PIISensitivity.HIGH),
            }
        )

        payload = {"mrn": "MRN12345", "facility": "Hospital A"}
        detections = handler.detect_pii(payload)

        assert len(detections) == 1
        assert detections[0].field_path == "mrn"
        assert detections[0].sensitivity == PIISensitivity.HIGH


# ── RU-062 / CEG-COR-009: lists are traversed, input is never mutated ──


@pytest.mark.compliance
@pytest.mark.unit
class TestPIIRecursiveTraversal:
    def test_detect_pii_descends_into_list_of_dicts(self) -> None:
        handler = PIIHandler()
        paths = handler.get_pii_field_paths({"entity_id": "b1", "coborrowers": [{"email": "x@y.com"}]})
        assert paths == ("coborrowers[0].email",)

    def test_detect_pii_descends_into_list_of_strings(self) -> None:
        handler = PIIHandler()
        detections = handler.detect_pii({"notes": ["clean", "SSN is 123-45-6789"]})
        assert [(d.field_path, d.category) for d in detections] == [("notes[1]", PIICategory.SSN)]

    def test_detect_pii_nested_lists_and_tuples(self) -> None:
        handler = PIIHandler()
        payload = {"a": [[{"phone": "555-123-4567"}]], "b": ({"contact_email": "a@b.com"},)}
        assert handler.get_pii_field_paths(payload) == ("a[0][0].phone", "b[0].contact_email")

    def test_dict_only_paths_unchanged(self) -> None:
        handler = PIIHandler()
        payload = {"patient": {"first_name": "John", "contact": {"phone": "555-123-4567"}}, "id": 1}
        assert handler.get_pii_field_paths(payload) == ("patient.first_name", "patient.contact.phone")

    def test_redact_does_not_mutate_nested_input(self) -> None:
        import copy

        handler = PIIHandler()
        payload = {"customer": {"email": "a@b.com", "tier": "gold"}, "rows": [{"ssn": "123-45-6789"}], "id": 7}
        before = copy.deepcopy(payload)
        out = handler.redact(payload, ["customer.email", "rows[0].ssn"])
        assert payload == before
        assert out == {"customer": {"tier": "gold"}, "rows": [{}], "id": 7}

    def test_mask_fields_does_not_mutate_nested_input(self) -> None:
        import copy

        handler = PIIHandler()
        payload = {"customer": {"email": "a@b.com"}, "rows": [{"phone": "555-123-4567"}]}
        before = copy.deepcopy(payload)
        out = handler.mask_fields(payload, mask_all_detected=True)
        assert payload == before
        assert out["customer"]["email"] == "***@***.com"
        assert out["rows"][0]["phone"] != "555-123-4567"

    def test_redact_all_detected_reaches_list_elements(self) -> None:
        handler = PIIHandler()
        out = handler.redact({"notes": ["keep", "SSN 123-45-6789", "also keep"]}, redact_all_detected=True)
        assert out == {"notes": ["keep", "also keep"]}

    def test_query_id_not_detected_inside_lists(self) -> None:
        handler = PIIHandler()
        assert handler.detect_pii({"ids": ["q_5c9382647927"], "query_id": "q_5c9382647927"}) == []

    def test_flag_off_restores_dict_only_traversal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "pii_redaction_recursive", False)
        handler = PIIHandler()
        assert handler.get_pii_field_paths({"entity_id": "b1", "coborrowers": [{"email": "x@y.com"}]}) == ()
