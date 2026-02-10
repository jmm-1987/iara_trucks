"""Tests de parsing del JSON de OpenAI (mock)."""
import json

from app.services.openai_service import _normalize_response


def test_normalize_response_full():
    data = {
        "doc_type": "fuel_ticket",
        "vehicle_identifier_guess": "1234ABC",
        "vendor_name": "Repsol",
        "date_issue": "2024-02-01",
        "date_due": None,
        "amounts": {"total": 45.99, "currency": "EUR"},
        "fuel": {"liters": 50, "price_per_liter": 1.45},
        "odometer_km": 125000,
        "confidence": 0.9,
    }
    result = _normalize_response(data)
    assert result["doc_type"] == "fuel_ticket"
    assert result["vehicle_identifier_guess"] == "1234ABC"
    assert result["vendor_name"] == "Repsol"
    assert result["date_issue"] == "2024-02-01"
    assert result["confidence"] == 0.9


def test_normalize_response_empty():
    result = _normalize_response({})
    assert result["doc_type"] == "other"
    assert result["confidence"] == 0


def test_normalize_response_invalid():
    result = _normalize_response("not a dict")
    assert result["doc_type"] == "other"
