"""
Servicio de procesamiento de documentos - Orquesta OpenAI, extracción y persistencia.
"""
import json
import logging
from datetime import date, datetime
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
from app.services.extraction_service import normalize_date, validate_and_enrich
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
    
    # Intentar asociar vehículo automáticamente si OpenAI extrajo una matrícula
    # SIEMPRE usar la matrícula extraída por OpenAI si está disponible, ya que es más confiable
    if extracted.get("vehicle_identifier_guess"):
        from app.services.extraction_service import normalize_plate
        from app.models import Vehicle
        
        extracted_plate = normalize_plate(extracted.get("vehicle_identifier_guess"))
        if extracted_plate:
            # Buscar o crear el vehículo con la matrícula extraída
            vehicle = Vehicle.query.filter_by(plate=extracted_plate).first()
            if not vehicle:
                # Crear vehículo automáticamente si no existe
                vehicle = Vehicle(plate=extracted_plate, active=True)
                db.session.add(vehicle)
                db.session.flush()  # Para obtener el ID
                logger.info("Vehículo creado automáticamente: %s", extracted_plate)
            
            # Si el documento ya tenía un vehículo asociado diferente, actualizarlo
            if doc.vehicle_id and doc.vehicle_id != vehicle.id:
                current_vehicle = Vehicle.query.get(doc.vehicle_id)
                if current_vehicle:
                    logger.warning("Documento %s tenía vehículo %s pero el documento muestra %s. Actualizando al vehículo correcto.", 
                                 document_id, current_vehicle.plate, extracted_plate)
            
            # Asociar el documento con el vehículo correcto (siempre usar la matrícula extraída)
            doc.vehicle_id = vehicle.id
            logger.info("Documento %s asociado automáticamente al vehículo %s (matrícula extraída del documento)", document_id, extracted_plate)

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

    conf = extracted.get("confidence", 0)
    lines.append(f"✓ Confianza: {int(conf * 100)}%")

    return "\n".join(lines)
