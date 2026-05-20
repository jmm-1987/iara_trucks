"""
Servicio de procesamiento de documentos - Orquesta OpenAI, extracción y persistencia.
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import io

from app.models import (
    Document,
    DocumentStatus,
    DocumentType,
    ExpenseCategory,
    ExpenseEntry,
    FuelEntry,
    MaintenanceEntry,
    db,
)
from app.services.extraction_service import (
    normalize_amount,
    normalize_date,
    validate_and_enrich,
)
from app.services.openai_service import analyze_document_image

logger = logging.getLogger(__name__)


def _sync_document_fuelentry_kilometers(doc: Document) -> None:
    """
    Sincroniza kilómetros entre Document.kilometers y FuelEntry.kilometers asociado.
    Si doc tiene kilómetros y fuel_entry no, copia a fuel_entry.
    Si fuel_entry tiene kilómetros y doc no, copia a doc.
    """
    if not doc.id:
        return  # Documento aún no guardado
    fuel_entry = FuelEntry.query.filter_by(document_id=doc.id).first()
    if fuel_entry:
        if doc.kilometers is not None and fuel_entry.kilometers is None:
            fuel_entry.kilometers = doc.kilometers
            logger.debug("Sincronizado doc.kilometers (%s) -> fuel_entry.kilometers", doc.kilometers)
        elif fuel_entry.kilometers is not None and doc.kilometers is None:
            doc.kilometers = fuel_entry.kilometers
            logger.debug("Sincronizado fuel_entry.kilometers (%s) -> doc.kilometers", fuel_entry.kilometers)
from app.services.document_review_service import refresh_document_correction_status
from app.services.reminders_service import update_reminders_from_extraction

logger = logging.getLogger(__name__)

DOC_TYPE_TO_EXPENSE_CATEGORY = {
    "invoice": ExpenseCategory.OTHER.value,  # Factura genérica va a OTHER
    "delivery_note": ExpenseCategory.OTHER.value,  # Albarán va a OTHER
    "insurance_policy": ExpenseCategory.INSURANCE.value,
    "itv": ExpenseCategory.ITV.value,
    "tachograph": ExpenseCategory.ITV.value,  # Tacógrafo va a ITV
    "workshop_invoice": ExpenseCategory.WORKSHOP.value,
    "tires_invoice": ExpenseCategory.TIRES.value,
}


def _is_truck_vehicle(doc: Document) -> bool:
    if not doc.vehicle:
        return False
    if not doc.vehicle.category:
        # Si no está categorizado, permitimos registrar mantenimiento.
        return True
    category = (doc.vehicle.category or "").strip().lower()
    if category in {"turismo", "furgoneta", "remolque"}:
        return False
    return category in {"camion", "tractora"} or "camion" in category


def _is_maintenance_document_type(doc_type: str | None) -> bool:
    return doc_type in {
        DocumentType.INVOICE.value,
        DocumentType.WORKSHOP_INVOICE.value,
        DocumentType.TIRES_INVOICE.value,
    }


def _maintenance_concept_for_doc(doc: Document, extracted: dict | None = None) -> str:
    extracted = extracted or {}
    ai_concept = (extracted.get("maintenance_concept") or "").strip()
    if ai_concept:
        return ai_concept
    if doc.doc_type == DocumentType.WORKSHOP_INVOICE.value:
        return "Factura de taller"
    if doc.doc_type == DocumentType.TIRES_INVOICE.value:
        return "Factura de neumáticos"
    return "Factura"


def _validate_km_consistency(
    vehicle_id: int, issue_date: date | None, kilometers: int | None, document_id: int | None = None
) -> tuple[bool, str | None]:
    """
    Valida coherencia del odómetro contra tickets del mismo vehículo.
    Regla: para fechas posteriores, los km deben ser mayores o iguales.
    """
    if not vehicle_id or kilometers is None:
        return False, "No hay kilómetros para validar."

    effective_date = issue_date or datetime.utcnow().date()
    q = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.kilometers.isnot(None),
    )
    if document_id:
        q = q.filter(FuelEntry.document_id != document_id)
    entries = q.order_by(FuelEntry.date.asc(), FuelEntry.id.asc()).all()

    for entry in entries:
        entry_date = entry.date or effective_date
        if entry_date <= effective_date and entry.kilometers > kilometers:
            return (
                False,
                f"El km ({kilometers}) es menor que otro anterior ({entry.kilometers}, {entry_date}).",
            )
        if entry_date >= effective_date and entry.kilometers < kilometers:
            return (
                False,
                f"El km ({kilometers}) es mayor que otro posterior ({entry.kilometers}, {entry_date}).",
            )
    return True, None


def process_document(document_id: int, force_doc_type: str | None = None) -> tuple[bool, str]:
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

    # Preparar bytes de imagen para la API de visión.
    # Si es PDF, convertimos automáticamente la primera página a JPEG.
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        try:
            from pdf2image import convert_from_path

            # Convertir solo la primera página para rapidez
            pages = convert_from_path(str(file_path), dpi=200, first_page=1, last_page=1)
            if not pages:
                raise RuntimeError("No se pudo convertir el PDF a imagen")
            buf = io.BytesIO()
            pages[0].save(buf, format="JPEG")
            image_bytes = buf.getvalue()
            mime_type = "image/jpeg"
        except Exception as e:
            msg = (
                "No se pudo convertir el PDF a imagen. "
                "Instala pdf2image y Poppler en el entorno para habilitar conversión de PDF."
            )
            logger.error("Error convirtiendo PDF a imagen para doc %s: %s", document_id, e)
            doc.status = DocumentStatus.ERROR.value
            doc.error_message = msg
            db.session.commit()
            return False, msg
    else:
        try:
            image_bytes = file_path.read_bytes()
        except Exception as e:
            doc.status = DocumentStatus.ERROR.value
            doc.error_message = str(e)
            db.session.commit()
            return False, str(e)

        # Inferir mime_type para imágenes normales
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
    if force_doc_type:
        extracted["doc_type"] = force_doc_type

    # Intentar asociar vehículo automáticamente si OpenAI extrajo una matrícula
    # SIEMPRE usar la matrícula extraída por OpenAI si está disponible, ya que es más confiable
    if extracted.get("vehicle_identifier_guess"):
        from app.services.extraction_service import (
            get_or_create_vehicle_by_plate,
            normalize_plate,
        )

        extracted_plate = normalize_plate(extracted.get("vehicle_identifier_guess"))
        if extracted_plate:
            vehicle = get_or_create_vehicle_by_plate(extracted_plate, create=True)
            if vehicle:
                if doc.vehicle_id and doc.vehicle_id != vehicle.id:
                    from app.models import Vehicle

                    current_vehicle = Vehicle.query.get(doc.vehicle_id)
                    if current_vehicle:
                        logger.warning(
                            "Documento %s tenía vehículo %s pero el documento muestra %s. "
                            "Actualizando al vehículo correcto.",
                            document_id,
                            current_vehicle.plate,
                            vehicle.plate,
                        )
                doc.vehicle_id = vehicle.id
                extracted["vehicle_identifier_guess"] = vehicle.plate
                logger.info(
                    "Documento %s asociado al vehículo %s (matrícula normalizada)",
                    document_id,
                    vehicle.plate,
                )

    # Persistir en Document
    amounts = extracted.get("amounts") or {}
    fuel = extracted.get("fuel") or {}
    doc.doc_type = extracted.get("doc_type", "other")
    doc.vendor = extracted.get("vendor_name") or extracted.get("vendor")
    
    # Convertir fechas de string a objetos date
    date_issue_str = extracted.get("date_issue")
    if date_issue_str:
        normalized_date = normalize_date(date_issue_str)
        if normalized_date:
            try:
                doc.issue_date = datetime.strptime(normalized_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                logger.warning("Fecha de emisión inválida: %s", date_issue_str)
                doc.issue_date = None
        else:
            doc.issue_date = None
    else:
        doc.issue_date = None
    
    date_due_str = extracted.get("date_due")
    if date_due_str:
        normalized_date = normalize_date(date_due_str)
        if normalized_date:
            try:
                doc.due_date = datetime.strptime(normalized_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                logger.warning("Fecha de vencimiento inválida: %s", date_due_str)
                doc.due_date = None
        else:
            doc.due_date = None
    else:
        doc.due_date = None
    
    # Convertir Decimal a float para JSON y guardar como Decimal en BD
    # Manejo diferente según el tipo de documento
    
    # Subtotal (base imponible)
    subtotal_amount = amounts.get("subtotal")
    if subtotal_amount is not None:
        if isinstance(subtotal_amount, Decimal):
            doc.subtotal_amount = subtotal_amount
        else:
            try:
                doc.subtotal_amount = Decimal(str(subtotal_amount))
            except (ValueError, TypeError):
                doc.subtotal_amount = None
    else:
        doc.subtotal_amount = None
    
    # IVA
    tax_amount = amounts.get("tax")
    if tax_amount is not None:
        if isinstance(tax_amount, Decimal):
            doc.tax_amount = tax_amount
        else:
            try:
                doc.tax_amount = Decimal(str(tax_amount))
            except (ValueError, TypeError):
                doc.tax_amount = None
    else:
        doc.tax_amount = None
    
    # Total (con IVA)
    total_amount = amounts.get("total")
    if total_amount is not None:
        if isinstance(total_amount, Decimal):
            doc.total_amount = total_amount
        else:
            try:
                doc.total_amount = Decimal(str(total_amount))
            except (ValueError, TypeError):
                doc.total_amount = None
    else:
        # Si no hay total pero hay subtotal e IVA, calcular total
        if doc.subtotal_amount is not None and doc.tax_amount is not None:
            doc.total_amount = doc.subtotal_amount + doc.tax_amount
        else:
            doc.total_amount = None
    
    # Lógica especial según tipo de documento
    # Para tickets de gasoil: calcular IVA si no está presente
    # Para otros documentos (seguros, recibos bancarios, facturas sin IVA): no calcular IVA
    if doc.doc_type == DocumentType.FUEL_TICKET.value:
        # Para tickets de gasoil, si no hay IVA desglosado, calcularlo
        if doc.total_amount and doc.tax_amount is None:
            # Si hay total pero no hay IVA, calcular base e IVA
            # IVA del 21% para combustible en España
            if doc.subtotal_amount is None:
                # Calcular base imponible desde el total (que incluye IVA)
                doc.subtotal_amount = doc.total_amount / Decimal("1.21")
                doc.tax_amount = doc.total_amount - doc.subtotal_amount
            else:
                # Si hay subtotal pero no IVA, calcular IVA
                doc.tax_amount = doc.total_amount - doc.subtotal_amount
    else:
        # Para otros documentos (seguros, recibos bancarios, facturas sin IVA)
        # Si no hay subtotal ni IVA pero hay total, el total es la base imponible
        if doc.total_amount and doc.subtotal_amount is None and doc.tax_amount is None:
            # El total es la base imponible (sin IVA)
            doc.subtotal_amount = doc.total_amount
            doc.tax_amount = Decimal("0")  # Sin IVA
    
    doc.currency = (amounts.get("currency") or "EUR")
    doc.kilometers = extracted.get("kilometers")
    km_needs_confirmation = False
    km_confirmation_reason = None

    if doc.doc_type == DocumentType.FUEL_TICKET.value:
        km_valid, km_reason = _validate_km_consistency(
            doc.vehicle_id, doc.issue_date, doc.kilometers, doc.id
        )
        if not km_valid:
            km_needs_confirmation = True
            km_confirmation_reason = km_reason
            doc.kilometers = None
            extracted["kilometers"] = None
            extracted["km_confirmation_reason"] = km_reason
        else:
            extracted["km_confirmation_reason"] = None
        extracted["km_needs_confirmation"] = km_needs_confirmation
    # Sincronizar kilómetros con FuelEntry si existe
    _sync_document_fuelentry_kilometers(doc)
    
    # Convertir Decimal a float para serialización JSON
    def decimal_to_float(obj):
        """Convierte Decimal a float para JSON serialization."""
        if isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, dict):
            return {k: decimal_to_float(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [decimal_to_float(item) for item in obj]
        return obj
    
    # IMPORTANTE: Guardar cambios del documento ANTES de crear FuelEntry/ExpenseEntry
    # para asegurar que vehicle_id esté actualizado
    db.session.flush()
    
    extracted_for_json = decimal_to_float(extracted)
    doc.extracted_json = json.dumps(extracted_for_json, indent=2, default=str)
    doc.processed_at = datetime.utcnow()
    doc.status = DocumentStatus.PROCESSED.value
    doc.error_message = None
    
    # Guardar cambios del documento para que vehicle_id esté disponible
    db.session.flush()

    # Crear FuelEntry si es fuel_ticket
    if doc.doc_type == DocumentType.FUEL_TICKET.value and doc.vehicle_id:
        liters = fuel.get("liters")
        price = fuel.get("price_per_liter")
        total = fuel.get("total_amount") or amounts.get("total")
        if liters and (total or (liters and price)):
            # Asegurar que liters, price y total sean Decimal
            liters_decimal = liters if isinstance(liters, Decimal) else Decimal(str(liters))
            price_decimal = Decimal(str(price)) if price else Decimal("0")
            total_decimal = total if isinstance(total, Decimal) else (Decimal(str(total)) if total else Decimal("0"))
            
            # Usar los valores ya calculados en doc (que ya tienen la lógica de IVA aplicada)
            subtotal = doc.subtotal_amount
            tax = doc.tax_amount
            
            # Si aún no se calcularon, calcularlos ahora (para tickets de gasoil siempre debe haber IVA)
            if subtotal is None and total_decimal:
                # Calcular base e IVA desde el total (IVA 21% para combustible)
                subtotal = total_decimal / Decimal("1.21")
                tax = total_decimal - subtotal
            elif tax is None and subtotal and total_decimal:
                # Si hay subtotal pero no IVA, calcular IVA
                tax = total_decimal - subtotal
            
            # Intentar obtener kilómetros desde extracted o desde doc
            kilometers = None
            if fuel.get("kilometers") is not None:
                try:
                    kilometers = int(fuel.get("kilometers"))
                except (ValueError, TypeError):
                    pass
            elif fuel.get("odometer_km") is not None:
                try:
                    kilometers = int(fuel.get("odometer_km"))
                except (ValueError, TypeError):
                    pass
            elif doc.kilometers is not None:
                kilometers = doc.kilometers
            
            # Asegurar que doc.vehicle_id esté disponible y no sea None
            if not doc.vehicle_id:
                logger.error("No se puede crear FuelEntry: documento %s no tiene vehicle_id después de procesar", doc.id)
            else:
                fuel_entry = FuelEntry(
                    document_id=doc.id,
                    vehicle_id=doc.vehicle_id,
                    date=doc.issue_date or datetime.utcnow().date(),
                    liters=liters_decimal,
                    price_per_liter=price_decimal,
                    subtotal_amount=subtotal,
                    tax_amount=tax,
                    total_amount=total_decimal,
                    station=doc.vendor,
                    fuel_type=fuel.get("fuel_type"),
                    kilometers=kilometers,
                )
                db.session.add(fuel_entry)
                db.session.flush()  # Para que fuel_entry tenga ID antes de sincronizar
                # Sincronizar kilómetros bidireccionalmente
                _sync_document_fuelentry_kilometers(doc)
                logger.info("FuelEntry creado para documento %s, vehículo ID %s", doc.id, doc.vehicle_id)

    # Crear ExpenseEntry si es gasto
    category = DOC_TYPE_TO_EXPENSE_CATEGORY.get(doc.doc_type)
    if category and doc.vehicle_id and doc.total_amount:
        expense = ExpenseEntry(
            document_id=doc.id,
            vehicle_id=doc.vehicle_id,
            date=doc.issue_date or datetime.utcnow().date(),
            category=category,
            subtotal_amount=doc.subtotal_amount,
            tax_amount=doc.tax_amount,
            total_amount=doc.total_amount,
            vendor=doc.vendor,
        )
        db.session.add(expense)
        if _is_maintenance_document_type(doc.doc_type):
            resolved_concept = _maintenance_concept_for_doc(doc, extracted)
            existing_maintenance = MaintenanceEntry.query.filter_by(document_id=doc.id).first()
            if existing_maintenance:
                # Si ya existía (por una carga/procesado anterior), actualizar con el concepto más fiel extraído.
                existing_maintenance.vehicle_id = doc.vehicle_id
                existing_maintenance.date = doc.issue_date or datetime.utcnow().date()
                existing_maintenance.concept = resolved_concept
                existing_maintenance.vendor = doc.vendor
                existing_maintenance.subtotal_amount = doc.subtotal_amount
                existing_maintenance.tax_amount = doc.tax_amount
                existing_maintenance.total_amount = doc.total_amount
            else:
                maintenance = MaintenanceEntry(
                    document_id=doc.id,
                    vehicle_id=doc.vehicle_id,
                    date=doc.issue_date or datetime.utcnow().date(),
                    concept=resolved_concept,
                    vendor=doc.vendor,
                    subtotal_amount=doc.subtotal_amount,
                    tax_amount=doc.tax_amount,
                    total_amount=doc.total_amount,
                )
                db.session.add(maintenance)

    # Recordatorios
    update_reminders_from_extraction(doc, extracted)

    db.session.commit()

    # Seguridad: si es ticket y aún no hay FuelEntry, intentar crearlo ahora.
    if doc.doc_type == DocumentType.FUEL_TICKET.value:
        if ensure_fuel_entry_for_document(doc):
            db.session.commit()

    refresh_document_correction_status(doc, extracted)
    db.session.commit()

    return True, "Documento procesado correctamente"


def apply_user_field_to_document(doc: Document, field: str, value: str) -> tuple[bool, str]:
    """
    Aplica un dato introducido por el usuario (Telegram) al documento y extracted_json.
    """
    extracted: dict = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except (json.JSONDecodeError, TypeError):
            extracted = {}

    raw = (value or "").strip()
    if not raw:
        return False, "❌ Escribe un valor o /start para cancelar."

    if field == "date_due":
        normalized = normalize_date(raw)
        if not normalized:
            return False, "❌ Fecha no válida. Usa dd/mm/aaaa (ej: 31/12/2026)."
        extracted["date_due"] = normalized
        doc.due_date = datetime.strptime(normalized, "%Y-%m-%d").date()
    elif field == "date_issue":
        normalized = normalize_date(raw)
        if not normalized:
            return False, "❌ Fecha no válida. Usa dd/mm/aaaa (ej: 15/03/2026)."
        extracted["date_issue"] = normalized
        doc.issue_date = datetime.strptime(normalized, "%Y-%m-%d").date()
    elif field == "fuel_liters":
        liters = normalize_amount(raw)
        if liters is None or liters <= 0:
            return False, "❌ Litros no válidos. Escribe solo el número (ej: 85.5)."
        fuel = extracted.setdefault("fuel", {})
        fuel["liters"] = float(liters)
        extracted["fuel"] = fuel
    else:
        return False, "❌ Campo no reconocido."

    doc.extracted_json = json.dumps(extracted, indent=2, default=str)
    update_reminders_from_extraction(doc, extracted)
    if doc.doc_type == DocumentType.FUEL_TICKET.value and doc.vehicle_id:
        ensure_fuel_entry_for_document(doc)
    refresh_document_correction_status(doc, extracted)
    db.session.commit()
    return True, ""


def ensure_document_records_after_vehicle_assigned(doc: Document) -> None:
    """
    Tras asignar vehículo manualmente (Telegram), crea FuelEntry/Expense/Reminder
    que no se generaron en el procesado inicial sin vehicle_id.
    """
    if not doc or not doc.vehicle_id:
        return

    extracted: dict = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except (json.JSONDecodeError, TypeError):
            extracted = {}

    if doc.doc_type == DocumentType.FUEL_TICKET.value:
        ensure_fuel_entry_for_document(doc)
    else:
        category = DOC_TYPE_TO_EXPENSE_CATEGORY.get(doc.doc_type or "")
        if category and doc.total_amount and not ExpenseEntry.query.filter_by(document_id=doc.id).first():
            expense = ExpenseEntry(
                document_id=doc.id,
                vehicle_id=doc.vehicle_id,
                date=doc.issue_date or datetime.utcnow().date(),
                category=category,
                subtotal_amount=doc.subtotal_amount,
                tax_amount=doc.tax_amount,
                total_amount=doc.total_amount,
                vendor=doc.vendor,
            )
            db.session.add(expense)
            if _is_maintenance_document_type(doc.doc_type):
                if not MaintenanceEntry.query.filter_by(document_id=doc.id).first():
                    maintenance = MaintenanceEntry(
                        document_id=doc.id,
                        vehicle_id=doc.vehicle_id,
                        date=doc.issue_date or datetime.utcnow().date(),
                        concept=_maintenance_concept_for_doc(doc, extracted),
                        vendor=doc.vendor,
                        subtotal_amount=doc.subtotal_amount,
                        tax_amount=doc.tax_amount,
                        total_amount=doc.total_amount,
                    )
                    db.session.add(maintenance)
        if extracted:
            update_reminders_from_extraction(doc, extracted)


def sync_missing_fuel_entries(limit: int = 100) -> int:
    """
    Repara tickets procesados sin registro en consumos (FuelEntry).
    Devuelve cuántos se han creado.
    """
    linked_ids = db.session.query(FuelEntry.document_id).filter(FuelEntry.document_id.isnot(None))
    docs = (
        Document.query.filter(
            Document.doc_type == DocumentType.FUEL_TICKET.value,
            Document.status == DocumentStatus.PROCESSED.value,
            Document.vehicle_id.isnot(None),
            ~Document.id.in_(linked_ids),
        )
        .order_by(Document.id.desc())
        .limit(limit)
        .all()
    )
    created = 0
    for doc in docs:
        if ensure_fuel_entry_for_document(doc):
            created += 1
    if created:
        db.session.commit()
    return created


def ensure_fuel_entry_for_document(doc: Document) -> FuelEntry | None:
    """
    Crea (o devuelve) el FuelEntry de un ticket ya procesado.
    Útil cuando el vehículo se asigna después del OCR (Telegram).
    """
    if not doc or doc.doc_type != DocumentType.FUEL_TICKET.value or not doc.vehicle_id:
        return None

    existing = FuelEntry.query.filter_by(document_id=doc.id).first()
    if existing:
        if existing.vehicle_id != doc.vehicle_id:
            existing.vehicle_id = doc.vehicle_id
        return existing

    extracted: dict = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except (json.JSONDecodeError, TypeError):
            extracted = {}

    fuel = extracted.get("fuel") or {}
    amounts = extracted.get("amounts") or {}

    liters = fuel.get("liters")
    price = fuel.get("price_per_liter")
    total = fuel.get("total_amount") or amounts.get("total") or doc.total_amount

    if liters is not None:
        liters_decimal = liters if isinstance(liters, Decimal) else Decimal(str(liters))
    else:
        liters_decimal = None

    if liters_decimal is None or liters_decimal <= 0:
        return None

    if total is not None:
        total_decimal = total if isinstance(total, Decimal) else Decimal(str(total))
    else:
        total_decimal = Decimal("0")

    price_decimal = Decimal(str(price)) if price else Decimal("0")
    if price_decimal <= 0 and total_decimal > 0 and liters_decimal > 0:
        price_decimal = total_decimal / liters_decimal

    subtotal = doc.subtotal_amount
    tax = doc.tax_amount
    if subtotal is None and total_decimal:
        subtotal = total_decimal / Decimal("1.21")
        tax = total_decimal - subtotal
    elif tax is None and subtotal and total_decimal:
        tax = total_decimal - subtotal

    kilometers = doc.kilometers
    if kilometers is None:
        for key in ("kilometers", "odometer_km"):
            raw = fuel.get(key) if key in fuel else extracted.get(key)
            if raw is not None:
                try:
                    kilometers = int(raw)
                    break
                except (ValueError, TypeError):
                    pass

    fuel_entry = FuelEntry(
        document_id=doc.id,
        vehicle_id=doc.vehicle_id,
        date=doc.issue_date or datetime.utcnow().date(),
        liters=liters_decimal,
        price_per_liter=price_decimal,
        subtotal_amount=subtotal,
        tax_amount=tax,
        total_amount=total_decimal if total_decimal > 0 else (subtotal or Decimal("0")) + (tax or Decimal("0")),
        station=doc.vendor,
        fuel_type=fuel.get("fuel_type"),
        kilometers=kilometers,
    )
    db.session.add(fuel_entry)
    db.session.flush()
    _sync_document_fuelentry_kilometers(doc)
    logger.info("FuelEntry creado (ensure) para documento %s", doc.id)
    return fuel_entry


def build_summary_for_telegram(extracted: dict, doc_type_labels: dict) -> str:
    """Construye un resumen legible para enviar por Telegram."""
    lines = []

    doc_type = extracted.get("doc_type", "other")
    lines.append(f"📄 Tipo: {doc_type_labels.get(doc_type, doc_type)}")

    if extracted.get("date_issue"):
        # Convertir fecha de YYYY-MM-DD a dd/mm/aaaa
        date_issue = extracted['date_issue']
        if isinstance(date_issue, str) and len(date_issue) == 10 and '-' in date_issue:
            try:
                from datetime import datetime
                dt = datetime.strptime(date_issue, '%Y-%m-%d')
                date_issue = dt.strftime('%d/%m/%Y')
            except:
                pass
        lines.append(f"📅 Fecha: {date_issue}")
    if extracted.get("date_due"):
        # Convertir fecha de YYYY-MM-DD a dd/mm/aaaa
        date_due = extracted['date_due']
        if isinstance(date_due, str) and len(date_due) == 10 and '-' in date_due:
            try:
                from datetime import datetime
                dt = datetime.strptime(date_due, '%Y-%m-%d')
                date_due = dt.strftime('%d/%m/%Y')
            except:
                pass
        lines.append(f"⏰ Vencimiento: {date_due}")
    if extracted.get("vendor_name"):
        lines.append(f"🏢 Proveedor: {extracted['vendor_name']}")

    amounts = extracted.get("amounts") or {}
    if amounts.get("total"):
        curr = amounts.get("currency", "EUR")
        lines.append(f"💰 Total: {amounts['total']} {curr}")

    fuel = extracted.get("fuel") or {}
    if fuel.get("liters"):
        lines.append(f"⛽ Litros: {fuel['liters']} | Precio/L: {fuel.get('price_per_liter', '-')}")
    km = extracted.get("kilometers") or extracted.get("odometer_km")
    if km is not None:
        lines.append(f"🔢 Kilómetros: {km} km")
    if extracted.get("km_confirmation_reason"):
        lines.append(f"⚠️ Revisar km: {extracted.get('km_confirmation_reason')}")

    conf = extracted.get("confidence", 0)
    lines.append(f"✓ Confianza: {int(conf * 100)}%")

    return "\n".join(lines)
