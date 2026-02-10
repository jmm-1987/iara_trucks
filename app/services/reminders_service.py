"""
Servicio de recordatorios - Creación y actualización de vencimientos.
"""
from datetime import date
from typing import Any

from app.models import Document, Reminder, db
from app.models import ReminderKind


def create_reminder_from_document(doc: Document, extracted: dict) -> Reminder | None:
    """
    Crea un Reminder si el documento tiene fecha de vencimiento
    (seguro, ITV, tacógrafo).
    """
    doc_type = (extracted.get("doc_type") or "").lower()
    due_str = extracted.get("date_due")
    if not due_str or not doc.vehicle_id:
        return None

    kind_map = {
        "insurance_policy": ReminderKind.INSURANCE.value,
        "itv": ReminderKind.ITV.value,
        "tachograph": ReminderKind.TACHOGRAPH.value,
    }
    kind = kind_map.get(doc_type)
    if not kind:
        return None

    # Validar fecha
    try:
        due = date.fromisoformat(due_str)
    except (ValueError, TypeError):
        return None

    reminder = Reminder(
        vehicle_id=doc.vehicle_id,
        kind=kind,
        due_date=due,
        status="active",
        document_id=doc.id,
    )
    db.session.add(reminder)
    return reminder


def update_reminders_from_extraction(doc: Document, extracted: dict) -> None:
    """Crea recordatorios según los datos extraídos y los persiste."""
    reminder = create_reminder_from_document(doc, extracted)
    if reminder:
        db.session.commit()
