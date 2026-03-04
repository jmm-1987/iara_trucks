"""
Rutas web - Panel de gestión de flotas.
"""
import calendar
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
    Reminder,
    ReminderKind,
    Vehicle,
    db,
)
from app.services.document_processor import process_document
from app.services.reporting_service import (
    calculate_fuel_consumption_stats,
    dashboard_kpis,
    expenses_by_category,
    export_csv_report,
    fuel_consumption_by_vehicle,
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
        plate = (request.form.get("plate") or "").strip().upper()
        if not plate:
            flash("La matrícula es obligatoria.", "danger")
            return render_template("vehicles/form.html", vehicle=None)
        if Vehicle.query.filter_by(plate=plate).first():
            flash(f"Ya existe un vehículo con matrícula {plate}.", "danger")
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

    q = Document.query
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
        },
    )


@web_bp.route("/documentos/<int:did>")
def document_detail(did):
    doc = Document.query.get_or_404(did)
    return render_template(
        "documents/detail.html",
        doc=doc,
        doc_type_labels=DOC_TYPE_LABELS,
    )


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
            if total_amount is None or total_amount < 0:
                flash("El importe total es obligatorio y debe ser ≥ 0.", "danger")
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
            due_date = _parse_date(request.form.get("due_date"))

            doc = Document(
                vehicle_id=vehicle_id,
                doc_type=doc_type,
                file_path="manual",
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
        from app import create_app

        app = create_app()
        upload_dir = Path(app.config["UPLOAD_FOLDER"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        ext = (filename.rsplit(".", 1)[-1] or "jpg").lower()
        if ext == "pdf":
            flash("PDF: guardado como pendiente. El procesamiento con visión requiere imagen.", "info")
        unique_name = f"{stem}_{filename[:20]}.{ext}"
        filepath = upload_dir / unique_name
        f.save(str(filepath))

        doc = Document(
            vehicle_id=vehicle_id,
            doc_type=None,
            file_path=unique_name,
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


# --- Reportes ---
@web_bp.route("/reportes")
def reports():
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

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
        expense_data=expense_data,
        reminders_data=reminders_data,
        vehicles=vehicles,
        filters={"vehicle_id": vehicle_id, "date_from": date_from, "date_to": date_to},
    )


@web_bp.route("/reportes/export/<report_type>")
def report_export(report_type):
    if report_type not in ("fuel", "expenses", "reminders"):
        abort(404)
    vehicle_id = request.args.get("vehicle_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to) if date_to else None

    csv_content = export_csv_report(report_type, vehicle_id, df, dt)
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
    return render_template(
        "reminders/list.html",
        reminders=data,
        vehicles=vehicles,
        vehicle_id=vehicle_id,
    )
