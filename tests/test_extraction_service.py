"""Tests del servicio de extracción."""
from decimal import Decimal

import pytest

from app.services.extraction_service import (
    get_pending_document_fields,
    normalize_amount,
    normalize_date,
    normalize_plate,
    validate_and_enrich,
)


class TestNormalizeAmount:
    def test_decimal_string_comma(self):
        assert normalize_amount("45,99") == Decimal("45.99")

    def test_decimal_string_point(self):
        assert normalize_amount("45.99") == Decimal("45.99")

    def test_european_format(self):
        assert normalize_amount("1.234,56") == Decimal("1234.56")

    def test_none(self):
        assert normalize_amount(None) is None

    def test_empty_string(self):
        assert normalize_amount("") is None

    def test_integer(self):
        assert normalize_amount(100) == Decimal("100")


class TestNormalizeDate:
    def test_iso_format(self):
        assert normalize_date("2024-02-01") == "2024-02-01"

    def test_slash_format(self):
        assert normalize_date("01/02/2024") == "2024-02-01"

    def test_dash_format(self):
        assert normalize_date("01-02-2024") == "2024-02-01"

    def test_none(self):
        assert normalize_date(None) is None

    def test_invalid(self):
        assert normalize_date("invalid") is None


class TestNormalizePlate:
    def test_uppercase(self):
        assert normalize_plate("1234abc") == "1234ABC"

    def test_spaces_removed(self):
        assert normalize_plate(" 1234 ABC ") == "1234ABC"

    def test_hyphens_removed(self):
        assert normalize_plate("3130-LDW") == "3130LDW"
        assert normalize_plate("1234-ABC") == "1234ABC"

    def test_too_short(self):
        assert normalize_plate("123") is None

    def test_none(self):
        assert normalize_plate(None) is None


class TestValidateAndEnrich:
    def test_normalizes_dates(self):
        extracted = {"date_issue": "01/02/2024", "date_due": "15-03-2024"}
        result = validate_and_enrich(extracted)
        assert result["date_issue"] == "2024-02-01"
        assert result["date_due"] == "2024-03-15"

    def test_normalizes_amounts(self):
        extracted = {"amounts": {"total": "45,99", "subtotal": 40}}
        result = validate_and_enrich(extracted)
        assert result["amounts"]["total"] == Decimal("45.99")
        assert result["amounts"]["subtotal"] == Decimal("40")

    def test_adds_vehicle_plate(self):
        extracted = {"vehicle_identifier_guess": None}
        result = validate_and_enrich(extracted, "1234ABC")
        assert result["vehicle_identifier_guess"] == "1234ABC"

    def test_insurance_uses_policy_period_hasta(self):
        extracted = {
            "doc_type": "insurance_policy",
            "date_issue": "2025-06-15",
            "date_due": "2025-06-15",
            "policy_period": {
                "valid_from": "2025-01-01",
                "valid_to": "2025-12-31",
            },
        }
        result = validate_and_enrich(extracted)
        assert result["date_issue"] == "2025-01-01"
        assert result["date_due"] == "2025-12-31"

    def test_insurance_swaps_inverted_dates(self):
        extracted = {
            "doc_type": "insurance_policy",
            "date_issue": "2026-12-31",
            "date_due": "2026-01-01",
        }
        result = validate_and_enrich(extracted)
        assert result["date_issue"] == "2026-01-01"
        assert result["date_due"] == "2026-12-31"

    def test_workshop_invoice_uses_explicit_invoice_dates(self):
        extracted = {
            "doc_type": "workshop_invoice",
            "invoice_date": "2026-05-03",
            "payment_due_date": "2026-06-05",
            "date_issue": "2026-06-05",
            "date_due": None,
        }
        result = validate_and_enrich(extracted)
        assert result["date_issue"] == "2026-05-03"
        assert result["date_due"] == "2026-06-05"

    def test_workshop_invoice_swaps_inverted_dates(self):
        extracted = {
            "doc_type": "workshop_invoice",
            "date_issue": "2026-06-05",
            "date_due": "2026-05-03",
        }
        result = validate_and_enrich(extracted)
        assert result["date_issue"] == "2026-05-03"
        assert result["date_due"] == "2026-06-05"


class TestPendingDocumentFields:
    def test_itv_pending_date_due(self):
        pending = get_pending_document_fields(
            {"doc_type": "itv", "date_issue": "2026-12-12"},
            "itv",
            vehicle_id=1,
        )
        assert len(pending) == 1
        assert pending[0]["field"] == "date_due"

    def test_no_pending_when_due_present(self):
        pending = get_pending_document_fields(
            {"doc_type": "itv", "date_due": "2027-12-12"},
            "itv",
            vehicle_id=1,
        )
        assert pending == []
