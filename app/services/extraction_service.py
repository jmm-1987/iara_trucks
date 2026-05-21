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

    Acepta: "01/02/2024", "01-02-2024", "2024-02-01", "01.02.2024", "27-01-2026 21:05:05"
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Si viene con hora (ej. "27-01-2026 21:05:05"), tomar solo la parte de fecha
    if " " in s:
        s = s.split()[0]

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
    """
    Normaliza matrícula española: mayúsculas, sin espacios ni guiones ni puntos.
    Formato canónico para comparar y guardar (ej. 3130-LDW -> 3130LDW).
    """
    if not plate:
        return None
    s = str(plate).strip().upper()
    for ch in (" ", "-", "_", ".", "/"):
        s = s.replace(ch, "")
    if len(s) >= 6 and s.isalnum():
        return s
    return None


def find_vehicle_by_plate(plate: Any):
    """
    Busca un vehículo existente comparando matrículas normalizadas.
    Nunca crea registros nuevos.
    """
    from app.models import Vehicle

    key = normalize_plate(plate)
    if not key:
        return None

    exact = Vehicle.query.filter_by(plate=key).first()
    if exact:
        return exact

    for vehicle in Vehicle.query.all():
        if normalize_plate(vehicle.plate) == key:
            return vehicle
    return None


def get_or_create_vehicle_by_plate(plate: Any, *, create: bool = True):
    """
    Resuelve matrícula contra la flota existente; solo crea si no hay coincidencia.
    La matrícula guardada nunca lleva guiones.
    """
    from app.models import Vehicle, db

    key = normalize_plate(plate)
    if not key:
        return None

    existing = find_vehicle_by_plate(key)
    if existing:
        return existing

    if not create:
        return None

    vehicle = Vehicle(plate=key, active=True)
    db.session.add(vehicle)
    db.session.flush()
    return vehicle


def apply_insurance_date_rules(result: dict) -> dict:
    """
    Corrige fechas en pólizas: vencimiento = fin de vigencia (hasta), no fecha de valor.
    """
    doc_type = (result.get("doc_type") or "").lower()
    if doc_type not in ("insurance_policy", "insurance"):
        return result

    period = result.get("policy_period") or {}
    if isinstance(period, dict):
        valid_from = normalize_date(period.get("valid_from"))
        valid_to = normalize_date(period.get("valid_to"))
        if valid_from:
            result["date_issue"] = valid_from
        if valid_to:
            result["date_due"] = valid_to

    d_issue = normalize_date(result.get("date_issue"))
    d_due = normalize_date(result.get("date_due"))
    if d_issue and d_due:
        from datetime import date as date_cls

        di = date_cls.fromisoformat(d_issue)
        dd = date_cls.fromisoformat(d_due)
        if dd < di:
            result["date_issue"], result["date_due"] = d_due, d_issue
    elif d_due and not d_issue:
        result["date_issue"] = None

    return result


COMMERCIAL_INVOICE_DOC_TYPES = frozenset(
    {"workshop_invoice", "invoice", "tires_invoice", "delivery_note"}
)


def apply_commercial_invoice_date_rules(result: dict) -> dict:
    """
    Corrige fechas en facturas: emisión (issue) vs vencimiento de pago (due).
    Si el modelo las invierte, intercambia (el vencimiento suele ser >= fecha factura).
    """
    doc_type = (result.get("doc_type") or "").lower()
    if doc_type not in COMMERCIAL_INVOICE_DOC_TYPES:
        return result

    d_issue = normalize_date(result.get("date_issue"))
    d_due = normalize_date(result.get("date_due"))

    if d_issue and d_due:
        from datetime import date as date_cls

        di = date_cls.fromisoformat(d_issue)
        dd = date_cls.fromisoformat(d_due)
        if dd < di:
            logger.info(
                "Factura %s: fechas invertidas (issue=%s, due=%s), corrigiendo",
                doc_type,
                d_issue,
                d_due,
            )
            d_issue, d_due = d_due, d_issue

    result["date_issue"] = d_issue
    result["date_due"] = d_due
    if d_issue:
        result["invoice_date"] = d_issue
    if d_due:
        result["payment_due_date"] = d_due

    return result


def validate_and_enrich(extracted: dict, vehicle_plate: str | None = None) -> dict:
    """
    Valida, normaliza y enriquece los datos extraídos.

    - Normaliza importes (coma/punto)
    - Normaliza fechas a YYYY-MM-DD
    - Completa vehicle_identifier si se pasa vehicle_plate
    """
    result = dict(extracted)

    # Fechas
    for key in (
        "date_issue",
        "date_due",
        "invoice_date",
        "payment_due_date",
    ):
        if result.get(key):
            result[key] = normalize_date(result[key])

    doc_type = (result.get("doc_type") or "").lower()
    if doc_type in COMMERCIAL_INVOICE_DOC_TYPES:
        # Campos explícitos tienen prioridad sobre date_issue/date_due genéricos
        if result.get("invoice_date"):
            result["date_issue"] = result["invoice_date"]
        if result.get("payment_due_date"):
            result["date_due"] = result["payment_due_date"]
        if result.get("date_issue"):
            result["invoice_date"] = result["date_issue"]
        if result.get("date_due"):
            result["payment_due_date"] = result["date_due"]

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

    # Kilómetros (cuentakilómetros)
    km_val = result.get("kilometers") or result.get("odometer_km")
    if km_val is not None:
        try:
            result["kilometers"] = int(float(km_val))
        except (ValueError, TypeError):
            result["kilometers"] = None
    if "odometer_km" in result:
        del result["odometer_km"]

    # Matrícula
    if vehicle_plate:
        result["vehicle_identifier_guess"] = normalize_plate(vehicle_plate)
    elif result.get("vehicle_identifier_guess"):
        result["vehicle_identifier_guess"] = normalize_plate(
            result["vehicle_identifier_guess"]
        )

    result = apply_insurance_date_rules(result)
    result = apply_commercial_invoice_date_rules(result)

    return result


def get_pending_document_fields(
    extracted: dict,
    doc_type: str | None,
    vehicle_id: int | None,
    doc=None,
) -> list[dict[str, str]]:
    """
    Campos pendientes que el usuario debe completar (en orden).
    Cada item: {"field": "date_due"|..., "prompt": "..."}
    """
    pending: list[dict[str, str]] = []
    doc_type_val = (doc_type or extracted.get("doc_type") or "other").lower()

    if not vehicle_id:
        pending.append(
            {
                "field": "vehicle",
                "prompt": "Selecciona el vehículo o escribe la matrícula:",
            }
        )
        return pending

    if doc_type_val == "fuel_ticket":
        liters_ok = False
        if doc is not None:
            fe = getattr(doc, "fuel_entry", None)
            if fe is not None and fe.liters is not None and float(fe.liters) > 0:
                liters_ok = True
        if not liters_ok:
            fuel = extracted.get("fuel") or {}
            liters_ok = bool(fuel.get("liters"))
        if not liters_ok:
            pending.append(
                {
                    "field": "fuel_liters",
                    "prompt": "¿Cuántos litros repostaste? (solo el número, ej: 85.5)",
                }
            )
        has_issue = extracted.get("date_issue") or (doc and getattr(doc, "issue_date", None))
        if not has_issue:
            pending.append(
                {
                    "field": "date_issue",
                    "prompt": "¿Qué fecha tiene el ticket? (dd/mm/aaaa)",
                }
            )

    if doc_type_val in ("insurance_policy", "itv", "tachograph"):
        has_due = extracted.get("date_due") or (doc and getattr(doc, "due_date", None))
        if not has_due:
            labels = {
                "insurance_policy": "seguro",
                "itv": "ITV",
                "tachograph": "tacógrafo",
            }
            label = labels.get(doc_type_val, doc_type_val)
            pending.append(
                {
                    "field": "date_due",
                    "prompt": (
                        f"¿Cuál es la fecha de vencimiento del {label}? "
                        "(dd/mm/aaaa — la del «hasta» / fin de vigencia)"
                    ),
                }
            )

    return pending


def get_missing_critical_fields(
    extracted: dict, doc_type: str, vehicle_id: int | None, doc=None
) -> list[str]:
    """
    Determina qué campos críticos faltan para poder guardar el documento.

    Returns:
        Lista de mensajes descriptivos para preguntar al usuario
    """
    return [p["prompt"] for p in get_pending_document_fields(extracted, doc_type, vehicle_id, doc)]
