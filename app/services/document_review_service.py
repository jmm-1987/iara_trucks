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


def _document_kilometers(doc: Document) -> int | None:
    """Kilómetros efectivos: prioriza registro de combustible y luego el documento."""
    if doc.fuel_entry is not None and doc.fuel_entry.kilometers is not None:
        return doc.fuel_entry.kilometers
    return doc.kilometers


def _fuel_liters_present(doc: Document, extracted: dict) -> bool:
    if doc.fuel_entry is not None and doc.fuel_entry.liters is not None:
        try:
            if float(doc.fuel_entry.liters) > 0:
                return True
        except (TypeError, ValueError):
            pass
    fuel = extracted.get("fuel") or {}
    liters = fuel.get("liters")
    if liters is not None:
        try:
            return float(liters) > 0
        except (TypeError, ValueError):
            pass
    return False


def sync_extracted_from_document(doc: Document) -> dict:
    """Refleja en extracted_json los datos ya corregidos en el panel web."""
    extracted = _load_extracted(doc)
    if doc.issue_date:
        extracted["date_issue"] = doc.issue_date.isoformat()
    if doc.due_date:
        extracted["date_due"] = doc.due_date.isoformat()
    km = _document_kilometers(doc)
    if km is not None:
        extracted["kilometers"] = km
        extracted.pop("km_needs_confirmation", None)
        fuel = extracted.setdefault("fuel", {})
        fuel["kilometers"] = km
    if doc.fuel_entry is not None:
        fuel = extracted.setdefault("fuel", {})
        if doc.fuel_entry.liters is not None:
            fuel["liters"] = float(doc.fuel_entry.liters)
        if doc.fuel_entry.price_per_liter is not None:
            fuel["price_per_liter"] = float(doc.fuel_entry.price_per_liter)
    doc.extracted_json = json.dumps(extracted, ensure_ascii=False, default=str)
    return extracted


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
        if not _fuel_liters_present(doc, extracted):
            if "litros" not in issues:
                issues.append("litros")
        if doc.vehicle_id and not doc.fuel_entry and not _fuel_liters_present(doc, extracted):
            if "registro en consumos" not in issues:
                issues.append("registro en consumos")
        if _document_kilometers(doc) is None and not extracted.get("kilometers"):
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
