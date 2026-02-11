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
    # Obtener tipo de documento desde extracted o desde doc
    doc_type = (extracted.get("doc_type") or doc.doc_type or "").lower()
    
    # Obtener fecha de vencimiento desde extracted o desde doc
    due_str = extracted.get("date_due")
    if not due_str and doc.due_date:
        # Si no está en extracted pero sí en doc, usar la de doc
        due_str = doc.due_date.isoformat() if hasattr(doc.due_date, 'isoformat') else str(doc.due_date)
    
    if not due_str or not doc.vehicle_id:
        return None

    kind_map = {
        "insurance_policy": ReminderKind.INSURANCE.value,
        "insurance": ReminderKind.INSURANCE.value,  # Variación
        "itv": ReminderKind.ITV.value,
        "tachograph": ReminderKind.TACHOGRAPH.value,
    }
    kind = kind_map.get(doc_type)
    if not kind:
        return None

    # Validar fecha - puede venir en formato YYYY-MM-DD o ser un objeto date
    try:
        if isinstance(due_str, date):
            due = due_str
        elif isinstance(due_str, str):
            # Intentar parsear fecha en formato YYYY-MM-DD
            if len(due_str) == 10 and '-' in due_str:
                due = date.fromisoformat(due_str)
            else:
                # Intentar otros formatos comunes
                from datetime import datetime
                for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d']:
                    try:
                        due = datetime.strptime(due_str, fmt).date()
                        break
                    except ValueError:
                        continue
                else:
                    return None
        else:
            return None
    except (ValueError, TypeError, AttributeError):
        return None

    # Verificar si ya existe un reminder para este documento y vehículo
    existing = Reminder.query.filter_by(
        vehicle_id=doc.vehicle_id,
        kind=kind,
        document_id=doc.id,
        status="active"
    ).first()
    
    if existing:
        # Actualizar fecha si es diferente
        if existing.due_date != due:
            existing.due_date = due
            db.session.commit()
        return existing

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
    """
    Crea o actualiza recordatorios según los datos extraídos y los persiste.
    También puede crear recordatorios desde documentos ya procesados.
    """
    reminder = create_reminder_from_document(doc, extracted)
    if reminder:
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            import logging
            logger = logging.getLogger(__name__)
            logger.error("Error al crear reminder: %s", str(e))


def create_reminder_from_processed_document(doc: Document) -> Reminder | None:
    """
    Crea un Reminder desde un documento ya procesado.
    Útil para documentos que fueron procesados antes de tener esta funcionalidad.
    """
    if not doc.vehicle_id or not doc.due_date:
        return None
    
    doc_type = (doc.doc_type or "").lower()
    
    kind_map = {
        "insurance_policy": ReminderKind.INSURANCE.value,
        "insurance": ReminderKind.INSURANCE.value,
        "itv": ReminderKind.ITV.value,
        "tachograph": ReminderKind.TACHOGRAPH.value,
    }
    kind = kind_map.get(doc_type)
    if not kind:
        return None
    
    # Verificar si ya existe un reminder para este documento
    existing = Reminder.query.filter_by(
        vehicle_id=doc.vehicle_id,
        kind=kind,
        document_id=doc.id,
        status="active"
    ).first()
    
    if existing:
        # Actualizar fecha si es diferente
        if existing.due_date != doc.due_date:
            existing.due_date = doc.due_date
            db.session.commit()
        return existing
    
    reminder = Reminder(
        vehicle_id=doc.vehicle_id,
        kind=kind,
        due_date=doc.due_date,
        status="active",
        document_id=doc.id,
    )
    db.session.add(reminder)
    try:
        db.session.commit()
        return reminder
    except Exception as e:
        db.session.rollback()
        import logging
        logger = logging.getLogger(__name__)
        logger.error("Error al crear reminder desde documento procesado: %s", str(e))
        return None
