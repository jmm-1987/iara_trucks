"""Tests del servicio de extracción."""
from decimal import Decimal

import pytest

from app.services.extraction_service import (
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
