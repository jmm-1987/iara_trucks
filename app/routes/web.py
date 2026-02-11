"""
Rutas web - Panel de gestión de flotas.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Response,
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from app.models import (
    Document,
    DocumentStatus,
    Vehicle,
    db,
)
from app.services.document_processor import process_document
from app.services.reporting_service import (
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


@web_bp.route("/")
def index():
    """Dashboard principal."""
    vehicle_id = request.args.get("vehicle_id", type=int)
    kpis = dashboard_kpis(vehicle_id)
    reminders = upcoming_due_dates(30)
    if vehicle_id:
        reminders = [r for r in reminders if r["vehicle_id"] == vehicle_id]
    
    # Obtener vehículos con sus KPIs
    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    vehicles_with_kpis = []
    for vehicle in vehicles:
        vehicle_kpis = dashboard_kpis(vehicle.id)
        vehicles_with_kpis.append({
            "vehicle": vehicle,
            "kpis": vehicle_kpis
        })
    
    return render_template(
        "dashboard.html",
        kpis=kpis,
        reminders=reminders[:10],
        vehicles_with_kpis=vehicles_with_kpis,
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


@web_bp.route("/documentos/subir", methods=["GET", "POST"])
def document_upload():
    if request.method == "POST":
        vehicle_id = request.form.get("vehicle_id", type=int)
        if not vehicle_id:
            flash("Selecciona un vehículo.", "danger")
            return redirect(url_for("web.document_upload"))
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
    return render_template("documents/upload.html", vehicles=vehicles)


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
    expense_data = expenses_by_category(vehicle_id, df, dt)
    # Mostrar todos los vencimientos activos en reportes
    reminders_data = upcoming_due_dates(None)
    if vehicle_id:
        reminders_data = [r for r in reminders_data if r["vehicle_id"] == vehicle_id]

    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    return render_template(
        "reports/index.html",
        fuel_data=fuel_data,
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
