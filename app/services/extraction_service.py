"""
Servicio de extracción - Validación y normalización de datos extraídos por OpenAI.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import logging

logger = logging.getLogger(__name__)


def normalize_amount(value: Any) -> Decimal | None:
    """
    Normaliza un importe: acepta string con coma/punto, devuelve Decimal.

    Ejemplos:
        "45,99" -> Decimal('45.99')
        "45.99" -> Decimal('45.99')
        "1.234,56" -> Decimal('1234.56')
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # Quitar símbolos de moneda y espacios
    s = re.sub(r"[^\d,.\-]", "", s)
    # Formato europeo: 1.234,56
    if re.match(r"^\d{1,3}(\.\d{3})*,\d+$", s):
        s = s.replace(".", "").replace(",", ".")
    # Formato inglés: 1,234.56
    elif re.match(r"^\d{1,3}(,\d{3})*\.\d+$", s):
        s = s.replace(",", "")
    # Coma como decimal
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def normalize_date(value: Any) -> str | None:
    """
    Normaliza una fecha a formato YYYY-MM-DD.

    Acepta: "01/02/2024", "01-02-2024", "2024-02-01", "01.02.2024"
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Intentar extraer con regex
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if match:
        return match.group(0)
    match = re.search(r"(\d{2})[/.-](\d{2})[/.-](\d{4})", s)
    if match:
        d, m, y = match.groups()
        return f"{y}-{m}-{d}"
    return None


def normalize_plate(plate: Any) -> str | None:
    """Normaliza matrícula española: quita espacios, mayúsculas."""
    if not plate:
        return None
    s = str(plate).strip().upper().replace(" ", "")
    if len(s) >= 6:
        return s
    return None


def validate_and_enrich(extracted: dict, vehicle_plate: str | None = None) -> dict:
    """
    Valida, normaliza y enriquece los datos extraídos.

    - Normaliza importes (coma/punto)
    - Normaliza fechas a YYYY-MM-DD
    - Completa vehicle_identifier si se pasa vehicle_plate
    """
    result = dict(extracted)

    # Fechas
    if result.get("date_issue"):
        result["date_issue"] = normalize_date(result["date_issue"])
    if result.get("date_due"):
        result["date_due"] = normalize_date(result["date_due"])

    # Importes en amounts
    amounts = result.get("amounts") or {}
    for key in ("subtotal", "tax", "total"):
        if key in amounts and amounts[key] is not None:
            amounts[key] = normalize_amount(amounts[key])
    result["amounts"] = amounts

    # Fuel
    fuel = result.get("fuel") or {}
    for key in ("liters", "price_per_liter"):
        if key in fuel and fuel[key] is not None:
            fuel[key] = normalize_amount(fuel[key])
    if fuel.get("total_amount") is None and fuel.get("liters") and fuel.get("price_per_liter"):
        fuel["total_amount"] = fuel["liters"] * fuel["price_per_liter"]
    result["fuel"] = fuel

    # Odómetro
    if result.get("odometer_km") is not None:
        try:
            result["odometer_km"] = int(float(result["odometer_km"]))
        except (ValueError, TypeError):
            result["odometer_km"] = None

    # Matrícula
    if vehicle_plate:
        result["vehicle_identifier_guess"] = normalize_plate(vehicle_plate)
    elif result.get("vehicle_identifier_guess"):
        result["vehicle_identifier_guess"] = normalize_plate(
            result["vehicle_identifier_guess"]
        )

    return result


def get_missing_critical_fields(
    extracted: dict, doc_type: str, vehicle_id: int | None
) -> list[str]:
    """
    Determina qué campos críticos faltan para poder guardar el documento.

    Returns:
        Lista de mensajes descriptivos para preguntar al usuario
    """
    missing = []

    if not vehicle_id:
        missing.append("No hay vehículo seleccionado. Por favor, selecciona uno con /vehiculo")

    doc_type_val = (extracted.get("doc_type") or "other").lower()

    # Para fuel_ticket: litros y fecha suelen ser críticos
    if doc_type_val == "fuel_ticket":
        fuel = extracted.get("fuel") or {}
        if not fuel.get("liters"):
            missing.append("No se detectó la cantidad de litros. ¿Cuántos litros repostaste?")
        if not extracted.get("date_issue"):
            missing.append("No se detectó la fecha. ¿Qué fecha tiene el ticket?")

    # Para seguros/ITV/tacógrafo: fecha de vencimiento
    if doc_type_val in ("insurance_policy", "itv", "tachograph"):
        if not extracted.get("date_due"):
            missing.append(
                f"No se detectó fecha de vencimiento para {doc_type_val}. ¿Cuándo vence?"
            )

    return missing
