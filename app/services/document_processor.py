"""
Servicio de procesamiento de documentos - Orquesta OpenAI, extracción y persistencia.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.models import (
    Document,
    DocumentStatus,
    DocumentType,
    ExpenseCategory,
    ExpenseEntry,
    FuelEntry,
    db,
)
from app.services.extraction_service import validate_and_enrich
from app.services.openai_service import analyze_document_image
from app.services.reminders_service import update_reminders_from_extraction

logger = logging.getLogger(__name__)

DOC_TYPE_TO_EXPENSE_CATEGORY = {
    "insurance_policy": ExpenseCategory.INSURANCE.value,
    "itv": ExpenseCategory.ITV.value,
    "tachograph": ExpenseCategory.ITV.value,  # Tacógrafo va a ITV
    "workshop_invoice": ExpenseCategory.WORKSHOP.value,
    "tires_invoice": ExpenseCategory.TIRES.value,
}


def process_document(document_id: int) -> tuple[bool, str]:
    """
    Procesa un documento pendiente: llama a OpenAI, extrae datos, persiste.

    Returns:
        (success, message)
    """
    from flask import current_app

    doc = Document.query.get(document_id)
    if not doc:
        return False, "Documento no encontrado"
    if doc.status == DocumentStatus.PROCESSED.value:
        return True, "Ya estaba procesado"

    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    file_path = upload_dir / Path(doc.file_path).name
    if not file_path.exists():
        doc.status = DocumentStatus.ERROR.value
        doc.error_message = "Archivo no encontrado"
        db.session.commit()
        return False, "Archivo no encontrado"

    try:
        image_bytes = file_path.read_bytes()
    except Exception as e:
        doc.status = DocumentStatus.ERROR.value
        doc.error_message = str(e)
        db.session.commit()
        return False, str(e)

    # Inferir mime_type
    ext = file_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    mime_type = mime_map.get(ext, "image/jpeg")

    vehicle_plate = doc.vehicle.plate if doc.vehicle else None

    try:
        extracted = analyze_document_image(image_bytes, mime_type)
    except Exception as e:
        logger.error("Error OpenAI en doc %s: %s", document_id, str(e))
        doc.status = DocumentStatus.ERROR.value
        doc.error_message = str(e)
        db.session.commit()
        return False, str(e)

    extracted = validate_and_enrich(extracted, vehicle_plate)

    # Persistir en Document
    amounts = extracted.get("amounts") or {}
    fuel = extracted.get("fuel") or {}
    doc.doc_type = extracted.get("doc_type", "other")
    doc.vendor = extracted.get("vendor_name") or extracted.get("vendor")
    doc.issue_date = extracted.get("date_issue")
    doc.due_date = extracted.get("date_due")
    doc.total_amount = amounts.get("total")
    doc.currency = (amounts.get("currency") or "EUR")
    doc.odometer_km = extracted.get("odometer_km")
    doc.extracted_json = json.dumps(extracted, indent=2)
    doc.processed_at = datetime.utcnow()
    doc.status = DocumentStatus.PROCESSED.value
    doc.error_message = None

    # Crear FuelEntry si es fuel_ticket
    if doc.doc_type == DocumentType.FUEL_TICKET.value and doc.vehicle_id:
        liters = fuel.get("liters")
        price = fuel.get("price_per_liter")
        total = fuel.get("total_amount") or amounts.get("total")
        if liters and (total or (liters and price)):
            fuel_entry = FuelEntry(
                document_id=doc.id,
                vehicle_id=doc.vehicle_id,
                date=doc.issue_date or datetime.utcnow().date(),
                liters=Decimal(str(liters)),
                price_per_liter=Decimal(str(price)) if price else Decimal("0"),
                total_amount=Decimal(str(total)) if total else Decimal("0"),
                station=doc.vendor,
                fuel_type=fuel.get("fuel_type"),
            )
            db.session.add(fuel_entry)

    # Crear ExpenseEntry si es gasto
    category = DOC_TYPE_TO_EXPENSE_CATEGORY.get(doc.doc_type)
    if category and doc.vehicle_id and doc.total_amount:
        expense = ExpenseEntry(
            document_id=doc.id,
            vehicle_id=doc.vehicle_id,
            date=doc.issue_date or datetime.utcnow().date(),
            category=category,
            total_amount=doc.total_amount,
            vendor=doc.vendor,
        )
        db.session.add(expense)

    # Recordatorios
    update_reminders_from_extraction(doc, extracted)

    db.session.commit()
    return True, "Documento procesado correctamente"


def build_summary_for_telegram(extracted: dict, doc_type_labels: dict) -> str:
    """Construye un resumen legible para enviar por Telegram."""
    lines = []

    doc_type = extracted.get("doc_type", "other")
    lines.append(f"📄 Tipo: {doc_type_labels.get(doc_type, doc_type)}")

    if extracted.get("date_issue"):
        lines.append(f"📅 Fecha: {extracted['date_issue']}")
    if extracted.get("date_due"):
        lines.append(f"⏰ Vencimiento: {extracted['date_due']}")
    if extracted.get("vendor_name"):
        lines.append(f"🏢 Proveedor: {extracted['vendor_name']}")

    amounts = extracted.get("amounts") or {}
    if amounts.get("total"):
        curr = amounts.get("currency", "EUR")
        lines.append(f"💰 Total: {amounts['total']} {curr}")

    fuel = extracted.get("fuel") or {}
    if fuel.get("liters"):
        lines.append(f"⛽ Litros: {fuel['liters']} | Precio/L: {fuel.get('price_per_liter', '-')}")
    if extracted.get("odometer_km"):
        lines.append(f"🔢 Odómetro: {extracted['odometer_km']} km")

    conf = extracted.get("confidence", 0)
    lines.append(f"✓ Confianza: {int(conf * 100)}%")

    return "\n".join(lines)
