"""
Servicio de deduplicación de documentos.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from app.models import Document


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def find_duplicate_by_hash(file_hash: str, vehicle_id: int | None = None) -> Document | None:
    """
    Busca documento duplicado por hash.
    Prioriza el mismo vehículo si se indica.
    """
    if not file_hash:
        return None
    q = Document.query.filter(Document.file_hash == file_hash).order_by(Document.uploaded_at.desc())
    if vehicle_id:
        same_vehicle = q.filter(Document.vehicle_id == vehicle_id).first()
        if same_vehicle:
            return same_vehicle
    return q.first()


def find_duplicate_manual_entry(
    vehicle_id: int,
    doc_type: str | None,
    issue_date,
    total_amount: Decimal | None,
    vendor: str | None,
) -> Document | None:
    """
    Duplicado lógico para entradas manuales sin fichero adjunto.
    """
    if not vehicle_id or not issue_date or total_amount is None:
        return None
    q = Document.query.filter(
        Document.vehicle_id == vehicle_id,
        Document.file_path == "manual",
        Document.issue_date == issue_date,
        Document.total_amount == total_amount,
        Document.doc_type == (doc_type or "other"),
    )
    normalized_vendor = (vendor or "").strip().lower()
    if normalized_vendor:
        q = q.filter(Document.vendor.isnot(None))
        candidates = q.all()
        for c in candidates:
            if (c.vendor or "").strip().lower() == normalized_vendor:
                return c
        return None
    return q.first()
