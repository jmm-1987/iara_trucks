"""
Rutas web - Panel de gestión de flotas.
"""
import calendar
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Response,
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename
from sqlalchemy import func

from decimal import Decimal

from app.models import (
    Document,
    DocumentStatus,
    DocumentType,
    ExpenseCategory,
    ExpenseEntry,
    FuelEntry,
    MaintenanceEntry,
    Reminder,
    ReminderKind,
    Vehicle,
    db,
)
from app.services.document_processor import process_document, ensure_fuel_entry_for_document, sync_missing_fuel_entries
from app.services.dedup_service import (
    find_duplicate_by_hash,
    find_duplicate_manual_entry,
    sha256_bytes,
)
from app.services.reminders_service import get_reminder_days_before, set_reminder_days_before
from app.services.reporting_service import (
    calculate_fuel_consumption_stats,
    dashboard_kpis,
    expenses_by_category,
    export_csv_report,
    fuel_consumption_by_vehicle,
    fuel_consumption_summary_by_vehicle,
    get_vehicle_statistics,
    upcoming_due_dates,
)

web_bp = Blueprint("web", __name__)

PER_PAGE = 20
DOC_TYPE_LABELS = {
    "fuel_ticket": "Ticket combustible",
    "invoice": "Factura",
    "delivery_note": "Albarán",
    "insurance_policy": "Póliza seguro",
    "itv": "ITV",
    "tachograph": "Tacógrafo",
    "workshop_invoice": "Factura taller",
    "tires_invoice": "Factura neumáticos",
    "other": "Otro",
}


def _is_truck_vehicle(vehicle: Vehicle | None) -> bool:
    if not vehicle:
        return False
    if not vehicle.category:
        # Si no está categorizado, no bloqueamos el registro de mantenimiento.
        return True
    category = (vehicle.category or "").strip().lower()
    if category in {"turismo", "furgoneta", "remolque"}:
        return False
    return category in {"camion", "tractora"} or "camion" in category


def _is_maintenance_document_type(doc_type: str | None) -> bool:
    return doc_type in {
        DocumentType.INVOICE.value,
        DocumentType.WORKSHOP_INVOICE.value,
        DocumentType.TIRES_INVOICE.value,
    }


def _ensure_maintenance_entries_for_invoices() -> int:
    """
    Garantiza que toda factura con vehículo asociado tenga MaintenanceEntry.
    Regla de negocio: factura + matrícula => mantenimiento.
    """
    docs = (
        Document.query.filter(
            Document.vehicle_id.isnot(None),
            Document.doc_type.in_(
                [
                    DocumentType.INVOICE.value,
                    DocumentType.WORKSHOP_INVOICE.value,
                    DocumentType.TIRES_INVOICE.value,
                ]
            ),
        )
        .order_by(Document.id.asc())
        .all()
    )
    created = 0
    for doc in docs:
        exists = MaintenanceEntry.query.filter_by(document_id=doc.id).first()
        if exists:
            continue
        concept = DOC_TYPE_LABELS.get(doc.doc_type, "Factura")
        if doc.extracted_json:
            try:
                extracted = json.loads(doc.extracted_json)
                concept = (extracted.get("maintenance_concept") or "").strip() or concept
            except (ValueError, TypeError):
                pass
        maintenance = MaintenanceEntry(
            document_id=doc.id,
            vehicle_id=doc.vehicle_id,
            date=doc.issue_date or datetime.utcnow().date(),
            concept=concept,
            vendor=doc.vendor,
            subtotal_amount=doc.subtotal_amount,
            tax_amount=doc.tax_amount,
            total_amount=doc.total_amount or Decimal("0"),
        )
        db.session.add(maintenance)
        created += 1
    if created:
        db.session.commit()
    return created


def allowed_file(filename: str, allowed: set) -> bool:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return ext in allowed


def _dashboard_period_from_request():
    """Calcula period_start y period_end desde request (period=month|year, year, month)."""
    today = date.today()
    period_type = (request.args.get("period") or "year").strip().lower()
    if period_type not in ("month", "year"):
        period_type = "year"

    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month

    month_names = ("", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
    if period_type == "year":
        year = max(2000, min(2100, year))
        period_start = date(year, 1, 1)
        period_end = date(year, 12, 31)
        period_label = str(year)
    else:
        year = max(2000, min(2100, year))
        month = max(1, min(12, month))
        period_start = date(year, month, 1)
        _, last_day = calendar.monthrange(year, month)
        period_end = date(year, month, last_day)
        period_label = f"{month_names[month]} {year}"

    return {
        "period": period_type,
        "year": year,
        "month": month,
        "period_start": period_start,
        "period_end": period_end,
        "period_label": period_label,
    }


@web_bp.route("/")
def index():
    """Dashboard principal."""
    import logging
    logger = logging.getLogger(__name__)

    vehicle_id = request.args.get("vehicle_id", type=int)
    date_filter = _dashboard_period_from_request()
    period_start = date_filter["period_start"]
    period_end = date_filter["period_end"]

    kpis = dashboard_kpis(vehicle_id, period_start=period_start, period_end=period_end)
    reminders_all = upcoming_due_dates(30)
    reminders = reminders_all if not vehicle_id else [r for r in reminders_all if r["vehicle_id"] == vehicle_id]
    # Primer vencimiento por vehículo (para la tabla de vehículos)
    next_reminder_by_vid = {}
    for r in reminders_all:
        vid = r["vehicle_id"]
        if vid not in next_reminder_by_vid:
            next_reminder_by_vid[vid] = r

    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    logger.debug("Dashboard: %d vehículos activos encontrados", len(vehicles))

    # Gastos de taller por vehículo en el periodo (para €/km gasoil+taller)
    workshop_totals = (
        db.session.query(
            ExpenseEntry.vehicle_id,
            # Usar SIEMPRE base (subtotal) para los gastos de taller; si no hubiera base en algún registro antiguo, usar total como fallback.
            func.coalesce(
                func.sum(func.coalesce(ExpenseEntry.subtotal_amount, ExpenseEntry.total_amount)),
                0,
            ).label("total"),
        )
        .filter(
            ExpenseEntry.vehicle_id.in_([v.id for v in vehicles]),
            ExpenseEntry.date >= period_start,
            ExpenseEntry.date <= period_end,
            ExpenseEntry.category == "workshop",
        )
        .group_by(ExpenseEntry.vehicle_id)
        .all()
    )
    workshop_by_vid = {r.vehicle_id: float(r.total) for r in workshop_totals}

    vehicles_with_kpis = []
    for vehicle in vehicles:
        vehicle_kpis = dashboard_kpis(
            vehicle.id, period_start=period_start, period_end=period_end
        )
        consumption_stats = calculate_fuel_consumption_stats(
            vehicle.id, date_from=period_start, date_to=period_end
        )
        total_km = (consumption_stats or {}).get("total_km")
        fuel_cost = (consumption_stats or {}).get("total_cost") or 0
        workshop_total = workshop_by_vid.get(vehicle.id, 0)
        if total_km and total_km > 0:
            cost_per_km_with_workshop = round((fuel_cost + workshop_total) / total_km, 4)
        else:
            cost_per_km_with_workshop = None
        vehicles_with_kpis.append({
            "vehicle": vehicle,
            "kpis": vehicle_kpis,
            "consumption_stats": consumption_stats,
            "next_reminder": next_reminder_by_vid.get(vehicle.id),
            "cost_per_km_with_workshop": cost_per_km_with_workshop,
            "workshop_amount": workshop_by_vid.get(vehicle.id, 0),
        })

    return render_template(
        "dashboard.html",
        kpis=kpis,
        reminders=reminders[:10],
        vehicles_with_kpis=vehicles_with_kpis,
        date_filter=date_filter,
    )


# --- Vehículos CRUD ---
@web_bp.route("/vehiculos")
def vehicle_list():
    page = request.args.get("page", 1, type=int)
    pagination = (
        Vehicle.query.order_by(Vehicle.plate)
        .paginate(page=page, per_page=PER_PAGE)
    )
    return render_template("vehicles/list.html", pagination=pagination)


@web_bp.route("/vehiculos/nuevo", methods=["GET", "POST"])
def vehicle_create():
    if request.method == "POST":
        from app.services.extraction_service import find_vehicle_by_plate, normalize_plate

        plate = normalize_plate(request.form.get("plate") or "")
        if not plate:
            flash("La matrícula es obligatoria (sin guiones, ej: 1234ABC).", "danger")
            return render_template("vehicles/form.html", vehicle=None)
        existing = find_vehicle_by_plate(plate)
        if existing:
            flash(f"Ya existe un vehículo con matrícula {existing.plate}.", "danger")
            return render_template("vehicles/form.html", vehicle=None)
        v = Vehicle(
            plate=plate,
            alias=request.form.get("alias", "").strip() or None,
            brand=request.form.get("brand", "").strip() or None,
            model=request.form.get("model", "").strip() or None,
            category=request.form.get("category", "").strip() or None,
            active=True,
        )
        db.session.add(v)
        db.session.commit()
        flash("Vehículo creado correctamente.", "success")
        return redirect(url_for("web.vehicle_list"))
    return render_template("vehicles/form.html", vehicle=None)


@web_bp.route("/vehiculos/<int:vid>/editar", methods=["GET", "POST"])
def vehicle_edit(vid):
    v = Vehicle.query.get_or_404(vid)
    if request.method == "POST":
        v.alias = request.form.get("alias", "").strip() or None
        v.brand = request.form.get("brand", "").strip() or None
        v.model = request.form.get("model", "").strip() or None
        v.category = request.form.get("category", "").strip() or None
        v.active = request.form.get("active") == "1"
        db.session.commit()
        flash("Vehículo actualizado.", "success")
        return redirect(url_for("web.vehicle_list"))
    return render_template("vehicles/form.html", vehicle=v)


@web_bp.route("/vehiculos/<int:vid>/eliminar", methods=["POST"])
def vehicle_delete(vid):
    v = Vehicle.query.get_or_404(vid)
    v.active = False
    db.session.commit()
    flash("Vehículo desactivado.", "info")
    return redirect(url_for("web.vehicle_list"))


@web_bp.route("/vehiculos/<int:vid>")
def vehicle_detail(vid):
    """Página de detalle del vehículo con todas sus estadísticas."""
    stats = get_vehicle_statistics(vid)
    if not stats.get("vehicle"):
        abort(404)
    
    return render_template("vehicles/detail.html", **stats)


# --- Documentos ---
@web_bp.route("/documentos")
def document_list():
    page = request.args.get("page", 1, type=int)
    doc_type = request.args.get("doc_type", "").strip()
    status = request.args.get("status", "").strip()
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    q_text = (request.args.get("q") or "").strip()
    corregir = request.args.get("corregir", "").strip() in ("1", "true", "yes")

    q = Document.query
    if corregir:
        q = q.filter(Document.needs_correction.is_(True))
    if doc_type:
        q = q.filter(Document.doc_type == doc_type)
    if status:
        q = q.filter(Document.status == status)
    if vehicle_id:
        q = q.filter(Document.vehicle_id == vehicle_id)
    if date_from:
        try:
            q = q.filter(Document.uploaded_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(Document.uploaded_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    if q_text:
        like = f"%{q_text}%"
        q = q.filter(
            (Document.vendor.ilike(like)) | (Document.extracted_json.ilike(like))
        )

    pagination = q.order_by(Document.uploaded_at.desc()).paginate(
        page=page, per_page=PER_PAGE
    )
    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    return render_template(
        "documents/list.html",
        pagination=pagination,
        vehicles=vehicles,
        doc_type_labels=DOC_TYPE_LABELS,
        filters={
            "doc_type": doc_type,
            "status": status,
            "vehicle_id": vehicle_id,
            "date_from": date_from,
            "date_to": date_to,
            "q": q_text,
            "corregir": corregir,
        },
    )


@web_bp.route("/mantenimientos")
def maintenance_list():
    _ensure_maintenance_entries_for_invoices()
    page = request.args.get("page", 1, type=int)
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    concept = (request.args.get("concept") or "").strip()
    kind = (request.args.get("kind") or "").strip()

    q = (
        MaintenanceEntry.query
        .join(Vehicle, MaintenanceEntry.vehicle_id == Vehicle.id)
        .join(Document, MaintenanceEntry.document_id == Document.id)
    )
    if vehicle_id:
        q = q.filter(MaintenanceEntry.vehicle_id == vehicle_id)
    if concept:
        q = q.filter(MaintenanceEntry.concept.ilike(f"%{concept}%"))
    if kind == "workshop":
        q = q.filter(Document.doc_type == DocumentType.WORKSHOP_INVOICE.value)
    if date_from:
        try:
            q = q.filter(MaintenanceEntry.date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(MaintenanceEntry.date <= date.fromisoformat(date_to))
        except ValueError:
            pass

    totals_row = q.with_entities(
        func.coalesce(func.sum(MaintenanceEntry.subtotal_amount), 0).label("subtotal"),
        func.coalesce(func.sum(MaintenanceEntry.tax_amount), 0).label("tax"),
        func.coalesce(func.sum(MaintenanceEntry.total_amount), 0).label("total"),
        func.count(MaintenanceEntry.id).label("count"),
    ).first()

    totals = {
        "subtotal": float(totals_row.subtotal or 0),
        "tax": float(totals_row.tax or 0),
        "total": float(totals_row.total or 0),
        "count": int(totals_row.count or 0),
    }

    by_vehicle_rows = (
        q.with_entities(
            MaintenanceEntry.vehicle_id.label("vehicle_id"),
            Vehicle.plate.label("plate"),
            func.coalesce(func.sum(MaintenanceEntry.subtotal_amount), 0).label("total"),
        )
        .group_by(MaintenanceEntry.vehicle_id, Vehicle.plate)
        .order_by(func.coalesce(func.sum(MaintenanceEntry.subtotal_amount), 0).desc())
        .all()
    )
    spend_by_vehicle = [
        {
            "vehicle_id": r.vehicle_id,
            "plate": r.plate or "-",
            "total": float(r.total or 0),
        }
        for r in by_vehicle_rows
    ]
    max_vehicle_total = max((item["total"] for item in spend_by_vehicle), default=0)

    pagination = q.order_by(MaintenanceEntry.date.desc(), MaintenanceEntry.id.desc()).paginate(
        page=page, per_page=PER_PAGE
    )
    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    return render_template(
        "maintenance/list.html",
        pagination=pagination,
        totals=totals,
        spend_by_vehicle=spend_by_vehicle,
        max_vehicle_total=max_vehicle_total,
        vehicles=vehicles,
        filters={
            "vehicle_id": vehicle_id,
            "date_from": date_from,
            "date_to": date_to,
            "concept": concept,
            "kind": kind,
        },
    )


@web_bp.route("/documentos/<int:did>")
def document_detail(did):
    doc = Document.query.get_or_404(did)
    fuel_entry = FuelEntry.query.filter_by(document_id=doc.id).first()
    if (
        doc.status == DocumentStatus.PROCESSED.value
        and doc.doc_type == DocumentType.FUEL_TICKET.value
        and doc.vehicle_id
        and not fuel_entry
    ):
        fuel_entry = ensure_fuel_entry_for_document(doc)
        if fuel_entry:
            db.session.commit()
            flash("Consumo creado en el listado de combustible.", "success")
    return render_template(
        "documents/detail.html",
        doc=doc,
        fuel_entry=fuel_entry,
        doc_type_labels=DOC_TYPE_LABELS,
    )


@web_bp.route("/documentos/<int:did>/editar", methods=["GET", "POST"])
def document_edit(did):
    doc = Document.query.get_or_404(did)
    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    next_url = request.args.get("next") or request.form.get("next") or url_for("web.document_detail", did=doc.id)

    if request.method == "POST":
        vehicle_id = request.form.get("vehicle_id", type=int)
        doc_type = (request.form.get("doc_type") or "").strip() or "other"
        vendor = (request.form.get("vendor") or "").strip() or None
        issue_date = _parse_date(request.form.get("issue_date"))
        due_date = _parse_date(request.form.get("due_date"))
        subtotal = _parse_decimal(request.form.get("subtotal_amount"))
        tax = _parse_decimal(request.form.get("tax_amount"))
        total = _parse_decimal(request.form.get("total_amount"))
        km_raw = (request.form.get("kilometers") or "").strip()
        kilometers = int(km_raw) if km_raw.isdigit() else None

        if not vehicle_id:
            flash("Selecciona un vehículo.", "danger")
            return render_template("documents/form.html", doc=doc, vehicles=vehicles, doc_type_labels=DOC_TYPE_LABELS, next_url=next_url)
        if total is None:
            flash("El total es obligatorio.", "danger")
            return render_template("documents/form.html", doc=doc, vehicles=vehicles, doc_type_labels=DOC_TYPE_LABELS, next_url=next_url)
        if subtotal is None and tax is None:
            subtotal = total
            tax = Decimal("0")
        elif subtotal is None:
            subtotal = total - (tax or Decimal("0"))
        elif tax is None:
            tax = total - subtotal

        doc.vehicle_id = vehicle_id
        doc.doc_type = doc_type
        doc.vendor = vendor
        doc.issue_date = issue_date
        doc.due_date = due_date
        doc.subtotal_amount = subtotal
        doc.tax_amount = tax
        doc.total_amount = total
        doc.kilometers = kilometers

        # Sincronización con registro de combustible (ticket gasoil)
        if doc_type == DocumentType.FUEL_TICKET.value:
            liters = _parse_decimal(request.form.get("fuel_liters"))
            ppl = _parse_decimal(request.form.get("fuel_price_per_liter"))
            fuel = FuelEntry.query.filter_by(document_id=doc.id).first()
            if not fuel and liters is not None and liters > 0:
                fuel = FuelEntry(
                    document_id=doc.id,
                    vehicle_id=vehicle_id,
                    date=issue_date or datetime.utcnow().date(),
                    liters=liters,
                    price_per_liter=ppl or Decimal("0"),
                    subtotal_amount=subtotal,
                    tax_amount=tax,
                    total_amount=total,
                    station=vendor,
                    kilometers=kilometers,
                )
                if fuel.price_per_liter <= 0 and total and liters > 0:
                    fuel.price_per_liter = total / liters
                db.session.add(fuel)
            elif fuel:
                if liters is not None and liters > 0:
                    fuel.liters = liters
                if ppl is not None and ppl > 0:
                    fuel.price_per_liter = ppl
                fuel.vehicle_id = vehicle_id
                fuel.date = issue_date or fuel.date
                fuel.station = vendor
                fuel.subtotal_amount = subtotal
                fuel.tax_amount = tax
                fuel.total_amount = total
                fuel.kilometers = kilometers

        # Sincronización con gasto (si existe)
        expense = ExpenseEntry.query.filter_by(document_id=doc.id).first()
        if expense:
            expense.vehicle_id = vehicle_id
            expense.date = issue_date or expense.date
            expense.subtotal_amount = subtotal
            expense.tax_amount = tax
            expense.total_amount = total
            expense.vendor = vendor
            expense.category = DOC_TYPE_TO_EXPENSE_CATEGORY.get(doc_type, ExpenseCategory.OTHER.value)

        # Sincronización con mantenimiento (si existe)
        maintenance = MaintenanceEntry.query.filter_by(document_id=doc.id).first()
        if maintenance:
            maintenance.vehicle_id = vehicle_id
            maintenance.date = issue_date or maintenance.date
            maintenance.vendor = vendor
            maintenance.subtotal_amount = subtotal
            maintenance.tax_amount = tax
            maintenance.total_amount = total
            concept = (request.form.get("maintenance_concept") or "").strip()
            if concept:
                maintenance.concept = concept

        from app.services.document_review_service import (
            refresh_document_correction_status,
            sync_extracted_from_document,
        )

        if doc_type == DocumentType.FUEL_TICKET.value:
            ensure_fuel_entry_for_document(doc)
        sync_extracted_from_document(doc)
        refresh_document_correction_status(doc)
        db.session.commit()
        flash("Documento actualizado correctamente.", "success")
        return redirect(next_url)

    return render_template("documents/form.html", doc=doc, vehicles=vehicles, doc_type_labels=DOC_TYPE_LABELS, next_url=next_url)


@web_bp.route("/documentos/<int:did>/reprocesar", methods=["POST"])
def document_reprocess(did):
    doc = Document.query.get_or_404(did)
    doc.status = DocumentStatus.PENDING.value
    doc.error_message = None
    doc.extracted_json = None
    doc.processed_at = None
    db.session.commit()
    success, msg = process_document(did)
    if success:
        flash(msg, "success")
    else:
        flash(msg, "danger")
    return redirect(url_for("web.document_detail", did=did))


@web_bp.route("/documentos/<int:did>/crear-recordatorio", methods=["POST"])
def document_create_reminder(did):
    """Crea un recordatorio manualmente desde un documento procesado."""
    from app.services.reminders_service import create_reminder_from_processed_document
    
    doc = Document.query.get_or_404(did)
    if not doc.due_date:
        flash("El documento no tiene fecha de vencimiento.", "danger")
        return redirect(url_for("web.document_detail", did=did))
    
    if not doc.vehicle_id:
        flash("El documento no está asociado a un vehículo.", "danger")
        return redirect(url_for("web.document_detail", did=did))
    
    reminder = create_reminder_from_processed_document(doc)
    if reminder:
        flash("Recordatorio creado correctamente.", "success")
    else:
        flash("No se pudo crear el recordatorio. Verifica que el tipo de documento sea seguro, ITV o tacógrafo.", "warning")
    
    return redirect(url_for("web.document_detail", did=did))


@web_bp.route("/documentos/<int:did>/eliminar", methods=["POST"])
def document_delete(did):
    """Borra un documento y todos sus registros relacionados."""
    from app.models import FuelEntry, ExpenseEntry, Reminder
    import os
    
    doc = Document.query.get_or_404(did)
    vehicle_id = doc.vehicle_id  # Guardar para redirigir después
    
    try:
        # Borrar FuelEntry asociado
        fuel_entry = FuelEntry.query.filter_by(document_id=doc.id).first()
        if fuel_entry:
            db.session.delete(fuel_entry)
        
        # Borrar ExpenseEntry asociado
        expense_entry = ExpenseEntry.query.filter_by(document_id=doc.id).first()
        if expense_entry:
            db.session.delete(expense_entry)
        
        # Borrar Reminder asociado
        reminder = Reminder.query.filter_by(document_id=doc.id).first()
        if reminder:
            db.session.delete(reminder)
        
        # Borrar archivo físico
        if doc.file_path:
            upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
            file_path = upload_dir / doc.file_path
            if file_path.exists():
                try:
                    os.remove(str(file_path))
                except OSError:
                    pass  # Si no se puede borrar, continuar
        
        # Borrar el documento
        db.session.delete(doc)
        db.session.commit()
        
        flash("Documento y registros relacionados borrados correctamente.", "success")
        
        # Redirigir a la lista de documentos o al vehículo si existe
        if vehicle_id:
            return redirect(url_for("web.document_list", vehicle_id=vehicle_id))
        else:
            return redirect(url_for("web.document_list"))
            
    except Exception as e:
        db.session.rollback()
        flash(f"Error al borrar el documento: {str(e)}", "danger")
        return redirect(url_for("web.document_detail", did=did))


@web_bp.route("/uploads/<path:filename>")
def serve_upload(filename):
    """Sirve archivos del directorio uploads."""
    from flask import current_app, send_file
    # Sanitizar: solo basename para evitar path traversal
    safe_name = Path(filename).name
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    path = (upload_dir / safe_name).resolve()
    upload_resolved = upload_dir.resolve()
    if not path.is_file() or not str(path).startswith(str(upload_resolved)):
        return {"error": "Archivo no encontrado"}, 404
    return send_file(path, as_attachment=False)


DOC_TYPE_TO_EXPENSE_CATEGORY = {
    "invoice": ExpenseCategory.OTHER.value,
    "delivery_note": ExpenseCategory.OTHER.value,
    "insurance_policy": ExpenseCategory.INSURANCE.value,
    "itv": ExpenseCategory.ITV.value,
    "tachograph": ExpenseCategory.ITV.value,
    "workshop_invoice": ExpenseCategory.WORKSHOP.value,
    "tires_invoice": ExpenseCategory.TIRES.value,
}


def _parse_decimal(value, default=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return Decimal(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return default


def _parse_date(value):
    if not value or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, date):
            return value
        s = str(value).strip()
        if len(s) == 10 and "-" in s:
            return date.fromisoformat(s)
        from datetime import datetime as dt
        for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
            try:
                return dt.strptime(s, fmt).date()
            except ValueError:
                continue
    except (ValueError, TypeError):
        pass
    return None


def _is_km_consistent_for_vehicle(
    vehicle_id: int,
    ticket_date: date,
    kilometers: int | None,
    exclude_fuel_entry_id: int | None = None,
) -> bool:
    """
    Valida coherencia de odómetro contra los tickets más cercanos.

    Nota: usamos vecino anterior/posterior (no todo el histórico) para permitir
    corregir datos en cadena cuando ya existen inconsistencias antiguas.
    """
    if kilometers is None:
        return True
    base_q = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.kilometers.isnot(None),
    )
    if exclude_fuel_entry_id:
        base_q = base_q.filter(FuelEntry.id != exclude_fuel_entry_id)

    prev_entry = (
        base_q.filter(FuelEntry.date < ticket_date)
        .order_by(FuelEntry.date.desc(), FuelEntry.id.desc())
        .first()
    )
    next_entry = (
        base_q.filter(FuelEntry.date > ticket_date)
        .order_by(FuelEntry.date.asc(), FuelEntry.id.asc())
        .first()
    )

    if prev_entry and prev_entry.kilometers is not None and prev_entry.kilometers > kilometers:
        return False
    if next_entry and next_entry.kilometers is not None and next_entry.kilometers < kilometers:
        return False
    return True


@web_bp.route("/documentos/subir", methods=["GET", "POST"])
def document_upload():
    if request.method == "POST":
        entry_mode = request.form.get("entry_mode", "").strip()
        vehicle_id = request.form.get("vehicle_id", type=int)
        if not vehicle_id:
            flash("Selecciona un vehículo.", "danger")
            return redirect(url_for("web.document_upload"))

        if entry_mode == "manual":
            # Entrada manual: crear documento sin archivo y crear Fuel/Expense/Reminder según tipo
            doc_type = (request.form.get("doc_type") or "").strip() or "other"
            issue_date = _parse_date(request.form.get("issue_date"))
            if not issue_date:
                flash("La fecha de emisión es obligatoria.", "danger")
                return redirect(url_for("web.document_upload"))

            total_amount = _parse_decimal(request.form.get("total_amount"))
            if total_amount is None:
                flash("El importe total es obligatorio.", "danger")
                return redirect(url_for("web.document_upload"))

            subtotal_amount = _parse_decimal(request.form.get("subtotal_amount"))
            tax_amount = _parse_decimal(request.form.get("tax_amount"))
            if subtotal_amount is None and tax_amount is None:
                subtotal_amount = total_amount / Decimal("1.21")
                tax_amount = total_amount - subtotal_amount
            elif subtotal_amount is None:
                subtotal_amount = total_amount - (tax_amount or Decimal("0"))
            elif tax_amount is None:
                tax_amount = total_amount - subtotal_amount

            vendor = (request.form.get("vendor") or "").strip() or None
            maintenance_concept = (request.form.get("maintenance_concept") or "").strip()
            due_date = _parse_date(request.form.get("due_date"))

            manual_file = request.files.get("manual_file")
            stored_file_path = "manual"
            file_hash = None
            if manual_file and manual_file.filename:
                allowed = {"jpg", "jpeg", "png", "pdf"}
                if not allowed_file(manual_file.filename, allowed):
                    flash("El archivo manual debe ser jpg, png o pdf.", "danger")
                    return redirect(url_for("web.document_upload"))
                content = manual_file.read()
                if not content:
                    flash("El archivo está vacío.", "danger")
                    return redirect(url_for("web.document_upload"))
                file_hash = sha256_bytes(content)
                existing_dup = find_duplicate_by_hash(file_hash, vehicle_id=vehicle_id)
                if existing_dup:
                    flash(
                        f"Documento duplicado detectado (doc #{existing_dup.id}). No se ha vuelto a crear.",
                        "warning",
                    )
                    return redirect(url_for("web.document_detail", did=existing_dup.id))
                raw_name = secure_filename(manual_file.filename) or "manual"
                ext = (raw_name.rsplit(".", 1)[-1] or "jpg").lower()
                upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
                upload_dir.mkdir(parents=True, exist_ok=True)
                unique_name = f"manual_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{raw_name[:20]}.{ext}"
                target = upload_dir / unique_name
                target.write_bytes(content)
                stored_file_path = unique_name
            else:
                existing_manual_dup = find_duplicate_manual_entry(
                    vehicle_id=vehicle_id,
                    doc_type=doc_type,
                    issue_date=issue_date,
                    total_amount=total_amount,
                    vendor=vendor,
                )
                if existing_manual_dup:
                    flash(
                        f"Entrada manual duplicada detectada (doc #{existing_manual_dup.id}).",
                        "warning",
                    )
                    return redirect(url_for("web.document_detail", did=existing_manual_dup.id))

            doc = Document(
                vehicle_id=vehicle_id,
                doc_type=doc_type,
                file_path=stored_file_path,
                file_hash=file_hash,
                status=DocumentStatus.PROCESSED.value,
                issue_date=issue_date,
                due_date=due_date,
                subtotal_amount=subtotal_amount,
                tax_amount=tax_amount,
                total_amount=total_amount,
                vendor=vendor,
            )
            db.session.add(doc)
            db.session.flush()

            if doc_type == DocumentType.FUEL_TICKET.value:
                liters = _parse_decimal(request.form.get("liters"))
                price_per_liter = _parse_decimal(request.form.get("price_per_liter"))
                kilometers = request.form.get("kilometers", "").strip()
                kilometers = int(kilometers) if kilometers.isdigit() else None
                if liters is None or liters <= 0:
                    flash("Para ticket de combustible indica los litros.", "danger")
                    db.session.rollback()
                    return redirect(url_for("web.document_upload"))
                if price_per_liter is None or price_per_liter <= 0:
                    price_per_liter = total_amount / liters
                fuel_entry = FuelEntry(
                    document_id=doc.id,
                    vehicle_id=vehicle_id,
                    date=issue_date,
                    liters=liters,
                    price_per_liter=price_per_liter,
                    kilometers=kilometers,
                    subtotal_amount=subtotal_amount,
                    tax_amount=tax_amount,
                    total_amount=total_amount,
                    station=vendor,
                )
                db.session.add(fuel_entry)
                # Sincronizar kilómetros también en el documento
                if kilometers is not None:
                    doc.kilometers = kilometers
            else:
                category = DOC_TYPE_TO_EXPENSE_CATEGORY.get(doc_type, ExpenseCategory.OTHER.value)
                expense = ExpenseEntry(
                    document_id=doc.id,
                    vehicle_id=vehicle_id,
                    date=issue_date,
                    category=category,
                    subtotal_amount=subtotal_amount,
                    tax_amount=tax_amount,
                    total_amount=total_amount,
                    vendor=vendor,
                )
                db.session.add(expense)
                if _is_maintenance_document_type(doc_type):
                    concept = maintenance_concept or DOC_TYPE_LABELS.get(doc_type, "Factura")
                    maintenance = MaintenanceEntry(
                        document_id=doc.id,
                        vehicle_id=vehicle_id,
                        date=issue_date,
                        concept=concept,
                        vendor=vendor,
                        subtotal_amount=subtotal_amount,
                        tax_amount=tax_amount,
                        total_amount=total_amount,
                    )
                    db.session.add(maintenance)

            reminder_kind = {
                "insurance_policy": ReminderKind.INSURANCE.value,
                "itv": ReminderKind.ITV.value,
                "tachograph": ReminderKind.TACHOGRAPH.value,
            }.get(doc_type)
            if due_date and reminder_kind:
                reminder = Reminder(
                    vehicle_id=vehicle_id,
                    kind=reminder_kind,
                    due_date=due_date,
                    status="active",
                    document_id=doc.id,
                )
                db.session.add(reminder)

            db.session.commit()
            flash("Documento registrado correctamente (entrada manual).", "success")
            return redirect(url_for("web.document_detail", did=doc.id))

        # Subida de archivo
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Selecciona un archivo.", "danger")
            return redirect(url_for("web.document_upload"))

        allowed = {"jpg", "jpeg", "png", "pdf"}
        if not allowed_file(f.filename, allowed):
            flash("Solo se permiten jpg, png, pdf.", "danger")
            return redirect(url_for("web.document_upload"))

        filename = secure_filename(f.filename)
        if not filename:
            filename = "upload"
        content = f.read()
        if not content:
            flash("El archivo está vacío.", "danger")
            return redirect(url_for("web.document_upload"))
        file_hash = sha256_bytes(content)
        existing_dup = find_duplicate_by_hash(file_hash, vehicle_id=vehicle_id)
        if existing_dup:
            flash(
                f"Documento duplicado detectado (doc #{existing_dup.id}). No se ha vuelto a subir.",
                "warning",
            )
            return redirect(url_for("web.document_detail", did=existing_dup.id))
        from app import create_app

        app = create_app()
        upload_dir = Path(app.config["UPLOAD_FOLDER"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        ext = (filename.rsplit(".", 1)[-1] or "jpg").lower()
        if ext == "pdf":
            flash("PDF: se intentará procesar convirtiendo la primera página a imagen.", "info")
        unique_name = f"{stem}_{filename[:20]}.{ext}"
        filepath = upload_dir / unique_name
        filepath.write_bytes(content)

        doc = Document(
            vehicle_id=vehicle_id,
            doc_type=None,
            file_path=unique_name,
            file_hash=file_hash,
            status=DocumentStatus.PENDING.value,
        )
        db.session.add(doc)
        db.session.commit()

        success, _ = process_document(doc.id)
        if success:
            flash("Documento subido y procesado.", "success")
        else:
            flash("Documento subido. Procesamiento falló - puedes reprocesar desde el detalle.", "warning")
        return redirect(url_for("web.document_detail", did=doc.id))

    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    return render_template("documents/upload.html", vehicles=vehicles, doc_type_labels=DOC_TYPE_LABELS)


@web_bp.route("/tickets/<int:fid>/editar", methods=["GET", "POST"])
def fuel_entry_edit(fid):
    fuel = FuelEntry.query.get_or_404(fid)
    doc = fuel.document
    next_url = request.args.get("next") or request.form.get("next") or url_for("web.reports", focus="consumos")

    if request.method == "POST":
        new_date = _parse_date(request.form.get("date")) or fuel.date
        liters = _parse_decimal(request.form.get("liters"))
        price_per_liter = _parse_decimal(request.form.get("price_per_liter"))
        subtotal = _parse_decimal(request.form.get("subtotal_amount"))
        tax = _parse_decimal(request.form.get("tax_amount"))
        total = _parse_decimal(request.form.get("total_amount"))
        station = (request.form.get("station") or "").strip() or None
        km_raw = (request.form.get("kilometers") or "").strip()
        kilometers = int(km_raw) if km_raw.isdigit() else None

        if liters is None or liters <= 0:
            flash("Litros debe ser mayor que 0.", "danger")
            return render_template("fuel/form.html", fuel=fuel, doc=doc, next_url=next_url)
        if price_per_liter is None or price_per_liter <= 0:
            flash("Precio por litro debe ser mayor que 0.", "danger")
            return render_template("fuel/form.html", fuel=fuel, doc=doc, next_url=next_url)
        if total is None:
            flash("Total debe ser válido.", "danger")
            return render_template("fuel/form.html", fuel=fuel, doc=doc, next_url=next_url)
        if not _is_km_consistent_for_vehicle(fuel.vehicle_id, new_date, kilometers, fuel.id):
            flash(
                "Los kilómetros no cuadran con el ticket anterior/posterior de ese vehículo para esa fecha.",
                "danger",
            )
            return render_template("fuel/form.html", fuel=fuel, doc=doc, next_url=next_url)

        if subtotal is None and tax is None:
            subtotal = total / Decimal("1.21")
            tax = total - subtotal
        elif subtotal is None:
            subtotal = total - (tax or Decimal("0"))
        elif tax is None:
            tax = total - subtotal

        fuel.date = new_date
        fuel.liters = liters
        fuel.price_per_liter = price_per_liter
        fuel.subtotal_amount = subtotal
        fuel.tax_amount = tax
        fuel.total_amount = total
        fuel.station = station
        fuel.kilometers = kilometers

        if doc:
            doc.issue_date = new_date
            doc.vendor = station
            doc.subtotal_amount = subtotal
            doc.tax_amount = tax
            doc.total_amount = total
            doc.kilometers = kilometers

        db.session.commit()
        flash("Ticket actualizado correctamente.", "success")
        return redirect(next_url)

    return render_template("fuel/form.html", fuel=fuel, doc=doc, next_url=next_url)


@web_bp.route("/tickets/<int:fid>/eliminar", methods=["POST"])
def fuel_entry_delete(fid):
    """Borra un ticket de combustible y, si aplica, su documento asociado."""
    fuel = FuelEntry.query.get_or_404(fid)
    doc = fuel.document
    next_url = request.form.get("next") or request.args.get("next") or url_for("web.reports", focus="consumos")

    try:
        db.session.delete(fuel)
        db.session.flush()

        # Si el documento no tiene más registros asociados, eliminarlo también.
        if doc:
            has_other_fuel = FuelEntry.query.filter(
                FuelEntry.document_id == doc.id,
                FuelEntry.id != fid,
            ).first()
            has_expense = ExpenseEntry.query.filter_by(document_id=doc.id).first()
            has_maintenance = MaintenanceEntry.query.filter_by(document_id=doc.id).first()
            has_reminder = Reminder.query.filter_by(document_id=doc.id).first()

            if not has_other_fuel and not has_expense and not has_maintenance and not has_reminder:
                if doc.file_path and doc.file_path != "manual":
                    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
                    file_path = upload_dir / doc.file_path
                    if file_path.exists():
                        try:
                            file_path.unlink()
                        except OSError:
                            pass
                db.session.delete(doc)

        db.session.commit()
        flash("Ticket eliminado correctamente.", "success")
        return redirect(next_url)
    except Exception as e:
        db.session.rollback()
        flash(f"No se pudo eliminar el ticket: {str(e)}", "danger")
        return redirect(url_for("web.fuel_entry_edit", fid=fid, next=next_url))


@web_bp.route("/mantenimientos/<int:mid>/editar", methods=["GET", "POST"])
def maintenance_edit(mid):
    maintenance = MaintenanceEntry.query.get_or_404(mid)
    doc = maintenance.document
    next_url = request.args.get("next") or request.form.get("next") or url_for("web.maintenance_list")

    if request.method == "POST":
        new_date = _parse_date(request.form.get("date")) or maintenance.date
        concept = (request.form.get("concept") or "").strip()
        vendor = (request.form.get("vendor") or "").strip() or None
        subtotal = _parse_decimal(request.form.get("subtotal_amount"))
        tax = _parse_decimal(request.form.get("tax_amount"))
        total = _parse_decimal(request.form.get("total_amount"))

        if not concept:
            flash("El concepto es obligatorio.", "danger")
            return render_template("maintenance/form.html", maintenance=maintenance, doc=doc, next_url=next_url)
        if total is None:
            flash("El total debe ser válido.", "danger")
            return render_template("maintenance/form.html", maintenance=maintenance, doc=doc, next_url=next_url)

        if subtotal is None and tax is None:
            subtotal = total
            tax = Decimal("0")
        elif subtotal is None:
            subtotal = total - (tax or Decimal("0"))
        elif tax is None:
            tax = total - subtotal

        maintenance.date = new_date
        maintenance.concept = concept
        maintenance.vendor = vendor
        maintenance.subtotal_amount = subtotal
        maintenance.tax_amount = tax
        maintenance.total_amount = total

        if doc:
            doc.issue_date = new_date
            doc.vendor = vendor
            doc.subtotal_amount = subtotal
            doc.tax_amount = tax
            doc.total_amount = total

        db.session.commit()
        flash("Mantenimiento actualizado correctamente.", "success")
        return redirect(next_url)

    return render_template("maintenance/form.html", maintenance=maintenance, doc=doc, next_url=next_url)


# --- Reportes ---
@web_bp.route("/reportes")
def reports():
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    focus = (request.args.get("focus") or "").strip().lower()

    if not date_from:
        date_from = (date.today() - timedelta(days=365)).isoformat()
    if not date_to:
        date_to = date.today().isoformat()

    try:
        df = date.fromisoformat(date_from)
        dt = date.fromisoformat(date_to)
    except ValueError:
        df = date.today() - timedelta(days=365)
        dt = date.today()

    # Reparar tickets procesados sin FuelEntry (p. ej. subidos por Telegram antes del fix).
    repaired = sync_missing_fuel_entries()
    if repaired:
        flash(f"Se han sincronizado {repaired} ticket(s) con el listado de consumos.", "info")

    if focus == "consumos":
        fuel_data = fuel_consumption_summary_by_vehicle(vehicle_id, df, dt)
    else:
        fuel_data = fuel_consumption_by_vehicle(vehicle_id, df, dt)
    # Detalle de tickets de combustible con kilómetros (para informes)
    fuel_tickets_q = FuelEntry.query.filter(
        FuelEntry.date >= df,
        FuelEntry.date <= dt,
    )
    if vehicle_id:
        fuel_tickets_q = fuel_tickets_q.filter(FuelEntry.vehicle_id == vehicle_id)
    fuel_tickets = (
        fuel_tickets_q
        .order_by(FuelEntry.date.desc(), FuelEntry.id.desc())
        .limit(300)
        .all()
    )
    # Consumo por repostaje con ventana móvil (suaviza ruido entre tickets).
    per_refuel_consumption: dict[int, float | None] = {}
    per_refuel_alerts: dict[int, dict] = {}
    per_ticket_alerts: dict[int, list[dict]] = {}
    per_ticket_tramo: dict[int, dict] = {}
    fuel_tickets_asc = sorted(fuel_tickets, key=lambda x: (x.vehicle_id, x.date, x.id))
    moving_window_size = 5
    # Heurísticas: detectar tramos desvirtuados (posible ticket faltante / km incorrectos).
    MIN_PLAUSIBLE_L100 = 18.0
    MAX_PLAUSIBLE_L100 = 45.0
    MAX_PLAUSIBLE_INTERVAL_KM = 2700.0
    tickets_by_vehicle: dict[int, list[FuelEntry]] = {}
    for t in fuel_tickets_asc:
        tickets_by_vehicle.setdefault(t.vehicle_id, []).append(t)

    for _, vehicle_tickets in tickets_by_vehicle.items():
        # Cada intervalo representa el tramo entre i-1 -> i, usando litros de i-1.
        intervals: list[dict] = []
        for i in range(1, len(vehicle_tickets)):
            prev = vehicle_tickets[i - 1]
            curr = vehicle_tickets[i]
            if (
                prev.kilometers is None
                or curr.kilometers is None
                or prev.liters is None
                or curr.kilometers <= prev.kilometers
            ):
                intervals.append({"ticket_id": curr.id, "liters": None, "km": None})
                continue
            intervals.append(
                {
                    "ticket_id": curr.id,
                    "prev_ticket_id": prev.id,
                    "liters": float(prev.liters),
                    "km": float(curr.kilometers - prev.kilometers),
                    "km_start": int(prev.kilometers),
                    "km_end": int(curr.kilometers),
                }
            )

        # Consumo suavizado por ventana móvil ponderada por kilómetros.
        for idx, interval in enumerate(intervals):
            if interval["liters"] is None or interval["km"] is None:
                per_refuel_consumption[interval["ticket_id"]] = None
                per_ticket_tramo[interval["ticket_id"]] = {
                    "km": None,
                    "km_start": None,
                    "km_end": None,
                    "prev_ticket_id": None,
                }
                per_refuel_alerts[interval["ticket_id"]] = {
                    "level": "warning",
                    "reason": "Tramo no calculable (km faltante o no creciente). Revisa si falta un ticket o si el odómetro está mal.",
                }
                continue

            window = intervals[max(0, idx - moving_window_size + 1): idx + 1]
            liters_sum = sum(w["liters"] for w in window if w["liters"] is not None and w["km"] is not None)
            km_sum = sum(w["km"] for w in window if w["liters"] is not None and w["km"] is not None)
            if km_sum > 0 and liters_sum > 0:
                l100 = round((liters_sum / km_sum) * 100, 2)
                per_refuel_consumption[interval["ticket_id"]] = l100
                per_ticket_tramo[interval["ticket_id"]] = {
                    "km": int(interval["km"]),
                    "km_start": interval.get("km_start"),
                    "km_end": interval.get("km_end"),
                    "prev_ticket_id": interval.get("prev_ticket_id"),
                    "l100_direct": None,
                }
                # Para avisos usamos SIEMPRE el tramo directo (prev->curr), no la ventana móvil,
                # para que al corregir un ticket el aviso desaparezca inmediatamente.
                interval_l100 = round((interval["liters"] / interval["km"]) * 100, 2) if interval["km"] > 0 else None
                per_ticket_tramo[interval["ticket_id"]]["l100_direct"] = interval_l100
                reasons: list[str] = []
                if interval["km"] > MAX_PLAUSIBLE_INTERVAL_KM:
                    reasons.append(f"Tramo muy largo ({int(interval['km'])} km)")
                if interval_l100 is not None and interval_l100 < MIN_PLAUSIBLE_L100:
                    reasons.append(f"Consumo muy bajo en tramo ({interval_l100} L/100)")
                if interval_l100 is not None and interval_l100 > MAX_PLAUSIBLE_L100:
                    reasons.append(f"Consumo muy alto en tramo ({interval_l100} L/100)")
                if reasons:
                    per_refuel_alerts[interval["ticket_id"]] = {
                        "level": "danger",
                        "reason": " / ".join(reasons) + ". Posible ticket faltante entre medias o km incorrectos.",
                    }
            else:
                per_refuel_consumption[interval["ticket_id"]] = None
                per_ticket_tramo[interval["ticket_id"]] = {
                    "km": int(interval["km"]) if interval["km"] is not None else None,
                    "km_start": interval.get("km_start"),
                    "km_end": interval.get("km_end"),
                    "prev_ticket_id": interval.get("prev_ticket_id"),
                    "l100_direct": None,
                }
                per_refuel_alerts[interval["ticket_id"]] = {
                    "level": "warning",
                    "reason": "Tramo no calculable (km o litros insuficientes).",
                }

    # Avisadores por ticket (para cuadrar): km faltantes, litros faltantes, duplicados, tramo sospechoso, etc.
    # 1) Duplicados por (vehículo, fecha, km, litros, base) dentro del listado.
    seen_sig: dict[tuple, int] = {}
    duplicates: set[int] = set()
    for fe in fuel_tickets_asc:
        km = fe.kilometers or (fe.document and fe.document.kilometers)
        base = float((fe.subtotal_amount or fe.total_amount) or 0)
        sig = (fe.vehicle_id, fe.date, km, float(fe.liters or 0), round(base, 2))
        if sig in seen_sig:
            duplicates.add(fe.id)
            duplicates.add(seen_sig[sig])
        else:
            seen_sig[sig] = fe.id

    # 2) Avisos básicos por ticket
    for fe in fuel_tickets:
        alerts: list[dict] = []
        km = fe.kilometers or (fe.document and fe.document.kilometers)
        if fe.liters is None or float(fe.liters or 0) <= 0:
            alerts.append({"level": "warning", "short": "L?", "reason": "Litros faltantes o 0. No se puede calcular consumo fiable."})
        if km is None:
            alerts.append({"level": "warning", "short": "KM?", "reason": "Kilómetros faltantes. No se puede calcular el tramo."})
        if fe.id in duplicates:
            alerts.append({"level": "danger", "short": "DUP", "reason": "Posible ticket duplicado (mismo vehículo/fecha/km/litros/importe)."})

        # Añadir aviso del tramo/consumo calculado (si existe)
        tramo_alert = per_refuel_alerts.get(fe.id)
        if tramo_alert:
            short = "TRAMO" if tramo_alert.get("level") == "danger" else "REV"
            alerts.append(
                {
                    "level": tramo_alert.get("level") or "warning",
                    "short": short,
                    "reason": tramo_alert.get("reason") or "Tramo sospechoso.",
                }
            )

        per_ticket_alerts[fe.id] = alerts
    expense_data = expenses_by_category(vehicle_id, df, dt)
    # Mostrar todos los vencimientos activos en reportes
    reminders_data = upcoming_due_dates(None)
    if vehicle_id:
        reminders_data = [r for r in reminders_data if r["vehicle_id"] == vehicle_id]

    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    return render_template(
        "reports/index.html",
        fuel_data=fuel_data,
        fuel_tickets=fuel_tickets,
        per_refuel_consumption=per_refuel_consumption,
        per_refuel_alerts=per_refuel_alerts,
        per_ticket_alerts=per_ticket_alerts,
        per_ticket_tramo=per_ticket_tramo,
        expense_data=expense_data,
        reminders_data=reminders_data,
        vehicles=vehicles,
        focus=focus,
        filters={"vehicle_id": vehicle_id, "date_from": date_from, "date_to": date_to},
    )


@web_bp.route("/reportes/export/<report_type>")
def report_export(report_type):
    if report_type not in ("fuel", "expenses", "reminders"):
        abort(404)
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    mode = (request.args.get("mode") or "").strip().lower()
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to) if date_to else None

    csv_content = export_csv_report(report_type, vehicle_id, df, dt, mode=mode)
    from flask import Response

    return Response(
        csv_content,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=reporte_{report_type}.csv"
        },
    )


# --- Recordatorios ---
@web_bp.route("/recordatorios")
def reminders_list():
    vehicle_id = request.args.get("vehicle_id", type=int)
    # Mostrar todos los vencimientos activos
    data = upcoming_due_dates(None)
    if vehicle_id:
        data = [r for r in data if r["vehicle_id"] == vehicle_id]
    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    reminder_days_before = get_reminder_days_before()
    return render_template(
        "reminders/list.html",
        reminders=data,
        vehicles=vehicles,
        vehicle_id=vehicle_id,
        reminder_days_before=reminder_days_before,
        reminder_kind_labels={
            ReminderKind.INSURANCE.value: "Seguro",
            ReminderKind.ITV.value: "ITV",
            ReminderKind.TACHOGRAPH.value: "Tacógrafo",
        },
    )


@web_bp.route("/recordatorios/configuracion", methods=["POST"])
def reminders_update_config():
    raw_days = (request.form.get("reminder_days_before") or "").strip()
    try:
        days = int(raw_days)
    except ValueError:
        flash("El número de días debe ser un entero.", "danger")
        return redirect(url_for("web.reminders_list"))
    saved = set_reminder_days_before(days)
    flash(f"Configuración guardada: avisar con {saved} días de antelación.", "success")
    return redirect(url_for("web.reminders_list"))


@web_bp.route("/recordatorios/nuevo", methods=["POST"])
def reminders_create_manual():
    vehicle_id = request.form.get("vehicle_id", type=int)
    if not vehicle_id:
        flash("Selecciona un vehículo para crear el aviso.", "danger")
        return redirect(url_for("web.reminders_list"))

    due_date_raw = (request.form.get("due_date") or "").strip()
    due_date = _parse_date(due_date_raw)
    if not due_date:
        flash("La fecha de vencimiento es obligatoria.", "danger")
        return redirect(url_for("web.reminders_list"))

    kind = (request.form.get("kind") or "").strip() or "manual"
    title = (request.form.get("title") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None
    notify_days_raw = (request.form.get("notify_days_before") or "").strip()
    notify_days = get_reminder_days_before()
    if notify_days_raw:
        try:
            notify_days = max(0, min(int(notify_days_raw), 365))
        except ValueError:
            flash("Los días de aviso deben ser un número entero.", "danger")
            return redirect(url_for("web.reminders_list"))

    reminder = Reminder(
        vehicle_id=vehicle_id,
        kind=kind,
        title=title,
        notes=notes,
        due_date=due_date,
        notify_days_before=notify_days,
        status="active",
    )
    db.session.add(reminder)
    db.session.commit()
    flash("Aviso manual creado correctamente.", "success")
    return redirect(url_for("web.reminders_list"))


@web_bp.route("/recordatorios/<int:rid>/eliminar", methods=["POST"])
def reminders_delete(rid):
    reminder = Reminder.query.get_or_404(rid)
    reminder.status = "expired"
    db.session.commit()
    flash("Aviso eliminado.", "info")
    return redirect(url_for("web.reminders_list"))
