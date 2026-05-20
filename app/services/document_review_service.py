"""
Estado de revisión de documentos (corrección en panel web).
"""
from __future__ import annotations

import json
from typing import Any

from app.models import Document, DocumentStatus, DocumentType, FuelEntry, db
from app.services.extraction_service import get_pending_document_fields

FIELD_LABELS = {
    "vehicle": "vehículo",
    "fuel_liters": "litros",
    "date_issue": "fecha",
    "date_due": "vencimiento",
    "kilometers": "kilómetros",
}


def _load_extracted(doc: Document) -> dict:
    if not doc.extracted_json:
        return {}
    try:
        return json.loads(doc.extracted_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def collect_correction_issues(doc: Document, extracted: dict | None = None) -> list[str]:
    """Lista corta de lo que falta o está mal en un documento."""
    if extracted is None:
        extracted = _load_extracted(doc)

    if doc.status == DocumentStatus.ERROR.value:
        return ["error de procesado"]

    if doc.status != DocumentStatus.PROCESSED.value:
        return []

    issues: list[str] = []
    for pf in get_pending_document_fields(extracted, doc.doc_type, doc.vehicle_id, doc):
        label = FIELD_LABELS.get(pf["field"], pf["field"])
        if label not in issues:
            issues.append(label)

    if doc.doc_type == DocumentType.FUEL_TICKET.value:
        if doc.vehicle_id and not FuelEntry.query.filter_by(document_id=doc.id).first():
            if "consumos" not in issues:
                issues.append("registro en consumos")
        if extracted.get("km_needs_confirmation"):
            if "kilómetros" not in issues:
                issues.append("kilómetros")
        elif doc.kilometers is None and not extracted.get("kilometers"):
            if "kilómetros" not in issues:
                issues.append("kilómetros")

    return issues


def refresh_document_correction_status(doc: Document, extracted: dict | None = None) -> list[str]:
    """Actualiza needs_correction / correction_summary en el documento."""
    issues = collect_correction_issues(doc, extracted)
    doc.needs_correction = bool(issues)
    doc.correction_summary = ", ".join(issues) if issues else None
    return issues


def count_documents_needing_correction() -> int:
    return Document.query.filter(Document.needs_correction.is_(True)).count()


def ensure_document_review_columns() -> None:
    """Añade columnas de revisión si la BD es anterior (SQLite)."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "document" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("document")}
    stmts = []
    if "needs_correction" not in cols:
        stmts.append("ALTER TABLE document ADD COLUMN needs_correction INTEGER DEFAULT 0")
    if "correction_summary" not in cols:
        stmts.append("ALTER TABLE document ADD COLUMN correction_summary VARCHAR(500)")
    for sql in stmts:
        db.session.execute(text(sql))
    if stmts:
        db.session.commit()
