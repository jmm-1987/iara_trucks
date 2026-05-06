"""
Servicio de reportes - Cálculos de consumos, gastos, vencimientos.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Query

from app.models import (
    Document,
    ExpenseEntry,
    FuelEntry,
    Reminder,
    Vehicle,
    db,
)


def fuel_consumption_by_vehicle(
    vehicle_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Litros y coste por vehículo y mes (con desglose de base imponible e IVA)."""
    q = db.session.query(
        FuelEntry.vehicle_id,
        func.strftime("%Y-%m", FuelEntry.date).label("month"),
        func.sum(FuelEntry.liters).label("total_liters"),
        func.sum(FuelEntry.subtotal_amount).label("subtotal_amount"),
        func.sum(FuelEntry.tax_amount).label("tax_amount"),
        func.sum(FuelEntry.total_amount).label("total_amount"),
    ).group_by(FuelEntry.vehicle_id, "month")

    if vehicle_id:
        q = q.filter(FuelEntry.vehicle_id == vehicle_id)
    if date_from:
        q = q.filter(FuelEntry.date >= date_from)
    if date_to:
        q = q.filter(FuelEntry.date <= date_to)

    rows = q.all()
    vehicles = {v.id: v for v in Vehicle.query.filter(Vehicle.active == True).all()}

    # Consumo mensual L/100km (requiere al menos 2 tickets con km en el mes)
    km_q = db.session.query(
        FuelEntry.vehicle_id,
        func.strftime("%Y-%m", FuelEntry.date).label("month"),
        FuelEntry.kilometers,
        FuelEntry.liters,
    ).filter(FuelEntry.kilometers.isnot(None))
    if vehicle_id:
        km_q = km_q.filter(FuelEntry.vehicle_id == vehicle_id)
    if date_from:
        km_q = km_q.filter(FuelEntry.date >= date_from)
    if date_to:
        km_q = km_q.filter(FuelEntry.date <= date_to)
    km_rows = km_q.order_by(FuelEntry.vehicle_id, "month", FuelEntry.date.asc(), FuelEntry.id.asc()).all()

    monthly_l100_map: dict[tuple[int, str], float | None] = {}
    monthly_km_map: dict[tuple[int, str], int | None] = {}
    monthly_entries: dict[tuple[int, str], list[tuple[int, float]]] = {}
    for e in km_rows:
        key = (e.vehicle_id, e.month)
        monthly_entries.setdefault(key, []).append((int(e.kilometers), float(e.liters or 0)))

    for key, entries in monthly_entries.items():
        if len(entries) < 2:
            monthly_l100_map[key] = None
            monthly_km_map[key] = None
            continue
        km_start = entries[0][0]
        km_end = entries[-1][0]
        if km_end <= km_start:
            monthly_l100_map[key] = None
            monthly_km_map[key] = None
            continue
        total_km = km_end - km_start
        monthly_km_map[key] = total_km
        # Igual que el cálculo anual: se excluye el último repostaje del tramo.
        total_liters = sum(liters for _, liters in entries[:-1])
        if total_liters <= 0:
            monthly_l100_map[key] = None
            continue
        monthly_l100_map[key] = round((total_liters / total_km) * 100, 2)

    return [
        {
            "vehicle_id": r.vehicle_id,
            "vehicle_plate": vehicles.get(r.vehicle_id, Vehicle(plate="?")).plate,
            "month": r.month,
            "total_liters": float(r.total_liters or 0),
            "total_km": monthly_km_map.get((r.vehicle_id, r.month)),
            "liters_per_100km": monthly_l100_map.get((r.vehicle_id, r.month)),
            "subtotal_amount": float(r.subtotal_amount) if r.subtotal_amount is not None else None,
            "tax_amount": float(r.tax_amount) if r.tax_amount is not None else None,
            "total_amount": float(r.total_amount or 0),
        }
        for r in rows
    ]


def fuel_consumption_summary_by_vehicle(
    vehicle_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """
    Resumen de combustible: una línea por vehículo (sin desglose mensual).
    - Litros/importe: suma contable de tickets en el periodo.
    - Km tramo y L/100km: calculados por odómetro (requiere >=2 tickets con km).
    """
    q = db.session.query(
        FuelEntry.vehicle_id,
        func.sum(FuelEntry.liters).label("total_liters"),
        func.sum(FuelEntry.subtotal_amount).label("subtotal_amount"),
        func.sum(FuelEntry.tax_amount).label("tax_amount"),
        func.sum(FuelEntry.total_amount).label("total_amount"),
    ).group_by(FuelEntry.vehicle_id)

    if vehicle_id:
        q = q.filter(FuelEntry.vehicle_id == vehicle_id)
    if date_from:
        q = q.filter(FuelEntry.date >= date_from)
    if date_to:
        q = q.filter(FuelEntry.date <= date_to)

    rows = q.all()
    vehicles = {v.id: v for v in Vehicle.query.filter(Vehicle.active == True).all()}

    # Km tramo y L/100km por vehículo en el periodo
    km_q = FuelEntry.query.filter(FuelEntry.kilometers.isnot(None))
    if vehicle_id:
        km_q = km_q.filter(FuelEntry.vehicle_id == vehicle_id)
    if date_from:
        km_q = km_q.filter(FuelEntry.date >= date_from)
    if date_to:
        km_q = km_q.filter(FuelEntry.date <= date_to)
    km_rows = km_q.order_by(FuelEntry.vehicle_id.asc(), FuelEntry.date.asc(), FuelEntry.id.asc()).all()

    entries_by_vehicle: dict[int, list[FuelEntry]] = {}
    for e in km_rows:
        entries_by_vehicle.setdefault(e.vehicle_id, []).append(e)

    stats_by_vehicle: dict[int, dict[str, float | int | None]] = {}
    for vid, entries in entries_by_vehicle.items():
        if len(entries) < 2:
            stats_by_vehicle[vid] = {"total_km": None, "liters_per_100km": None}
            continue
        km_start = entries[0].kilometers
        km_end = entries[-1].kilometers
        if km_start is None or km_end is None or km_end <= km_start:
            stats_by_vehicle[vid] = {"total_km": None, "liters_per_100km": None}
            continue
        total_km = int(km_end - km_start)
        total_liters_for_tramo = sum(float(e.liters or 0) for e in entries[:-1])
        if total_km > 0 and total_liters_for_tramo > 0:
            l100 = round((total_liters_for_tramo / total_km) * 100, 2)
        else:
            l100 = None
        stats_by_vehicle[vid] = {"total_km": total_km, "liters_per_100km": l100}

    result: list[dict] = []
    for r in rows:
        vid = int(r.vehicle_id)
        stats = stats_by_vehicle.get(vid) or {"total_km": None, "liters_per_100km": None}
        result.append(
            {
                "vehicle_id": vid,
                "vehicle_plate": vehicles.get(vid, Vehicle(plate="?")).plate,
                "total_liters": float(r.total_liters or 0),
                "total_km": stats.get("total_km"),
                "liters_per_100km": stats.get("liters_per_100km"),
                "subtotal_amount": float(r.subtotal_amount) if r.subtotal_amount is not None else None,
                "tax_amount": float(r.tax_amount) if r.tax_amount is not None else None,
                "total_amount": float(r.total_amount or 0),
            }
        )

    # Ordenar por vehículo (matrícula) para que sea estable en UI.
    result.sort(key=lambda x: (x.get("vehicle_plate") or ""))
    return result


def calculate_fuel_consumption_stats(vehicle_id: int, date_from: date | None = None, date_to: date | None = None) -> dict:
    """
    Calcula estadísticas de consumo: litros/100km y coste/km.
    Requiere que los tickets tengan kilómetros registrados.
    """
    q = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.kilometers.isnot(None)
    ).order_by(FuelEntry.date.asc(), FuelEntry.id.asc())
    
    if date_from:
        q = q.filter(FuelEntry.date >= date_from)
    if date_to:
        q = q.filter(FuelEntry.date <= date_to)
    
    entries = q.all()
    
    if len(entries) < 2:
        return {
            "liters_per_100km": None,
            "cost_per_km": None,
            "cost_per_km_105": None,
            "total_km": None,
            "total_liters": None,
            "total_cost": None,
        }
    
    # Calcular kilómetros recorridos y litros consumidos entre el primer y último ticket
    first_entry = entries[0]
    last_entry = entries[-1]
    
    km_start = first_entry.kilometers
    km_end = last_entry.kilometers
    
    if km_end <= km_start:
        return {
            "liters_per_100km": None,
            "cost_per_km": None,
            "total_km": None,
            "total_liters": None,
            "total_cost": None,
        }
    
    total_km = km_end - km_start
    # Litros: sumar todos excepto el ÚLTIMO (su combustible aún no se ha consumido en el tramo)
    total_liters = sum(float(e.liters or 0) for e in entries[:-1])
    # Coste: usar siempre la BASE (subtotal). Si no hubiera base en algún registro antiguo, usar total como fallback.
    total_cost = sum(
        float((e.subtotal_amount or e.total_amount) or 0)
        for e in entries[:-1]
    )
    
    if total_km > 0:
        liters_per_100km = (total_liters / total_km) * 100 if total_liters > 0 else None
        # Coste real por km con la base (sin IVA)
        cost_per_km = total_cost / total_km if total_cost > 0 else None
        # Coste teórico por km suponiendo precio fijo 1,05 €/L
        ref_price_per_liter = 1.05
        cost_per_km_105 = (total_liters * ref_price_per_liter) / total_km if total_liters > 0 else None
    else:
        liters_per_100km = None
        cost_per_km = None
        cost_per_km_105 = None
    
    return {
        "liters_per_100km": round(liters_per_100km, 2) if liters_per_100km else None,
        "cost_per_km": round(cost_per_km, 4) if cost_per_km else None,
        "cost_per_km_105": round(cost_per_km_105, 4) if cost_per_km_105 else None,
        "total_km": total_km,
        "total_liters": round(total_liters, 2),
        "total_cost": round(total_cost, 2),
    }


def expenses_by_category(
    vehicle_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Gastos por categoría y vehículo (con desglose de base imponible e IVA)."""
    q = (
        db.session.query(
            ExpenseEntry.vehicle_id,
            ExpenseEntry.category,
            # Base: si no hay subtotal en registros antiguos, usar total como fallback.
            func.sum(func.coalesce(ExpenseEntry.subtotal_amount, ExpenseEntry.total_amount)).label("subtotal"),
            func.sum(ExpenseEntry.tax_amount).label("tax"),
            func.sum(ExpenseEntry.total_amount).label("total"),
        )
        .group_by(ExpenseEntry.vehicle_id, ExpenseEntry.category)
    )
    if vehicle_id:
        q = q.filter(ExpenseEntry.vehicle_id == vehicle_id)
    if date_from:
        q = q.filter(ExpenseEntry.date >= date_from)
    if date_to:
        q = q.filter(ExpenseEntry.date <= date_to)

    rows = q.all()
    vehicles = {v.id: v for v in Vehicle.query.filter(Vehicle.active == True).all()}

    return [
        {
            "vehicle_id": r.vehicle_id,
            "vehicle_plate": vehicles.get(r.vehicle_id, Vehicle(plate="?")).plate,
            "category": r.category,
            "subtotal_amount": float(r.subtotal) if r.subtotal is not None else None,
            "tax_amount": float(r.tax) if r.tax is not None else None,
            "total_amount": float(r.total or 0),
        }
        for r in rows
    ]


def upcoming_due_dates(days_ahead: int | None = 90) -> list[dict]:
    """
    Próximos vencimientos en los próximos N días.
    
    Args:
        days_ahead: Número de días hacia adelante. Si es None, muestra todos los vencimientos activos.
    """
    today = date.today()
    
    query = Reminder.query.filter(
        Reminder.status == "active",
    )
    
    # Si se especifica días, filtrar por fecha
    if days_ahead is not None:
        limit = today + timedelta(days=days_ahead)
        query = query.filter(
            Reminder.due_date >= today,
            Reminder.due_date <= limit,
        )
    else:
        # Mostrar todos los vencimientos activos (pasados y futuros)
        query = query.filter(Reminder.due_date >= today - timedelta(days=3650))  # Últimos 10 años
    
    reminders = query.join(Vehicle).order_by(Reminder.due_date).all()

    return [
        {
            "id": r.id,
            "vehicle_id": r.vehicle_id,
            "vehicle_plate": r.vehicle.plate,
            "kind": r.kind,
            "title": r.title,
            "notes": r.notes,
            "due_date": r.due_date,
            "days_remaining": (r.due_date - today).days,
            "notify_days_before": r.notify_days_before,
        }
        for r in reminders
    ]


def dashboard_kpis(
    vehicle_id: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict:
    """KPIs rápidos para el dashboard. Si period_start/period_end son None, se usa el mes actual."""
    import logging
    logger = logging.getLogger(__name__)

    today = date.today()
    if period_start is None:
        period_start = today.replace(day=1)
    if period_end is None:
        period_end = today
    if period_end < period_start:
        period_end = period_start

    # Total vehículos activos
    vq = Vehicle.query.filter(Vehicle.active == True)
    if vehicle_id:
        vq = vq.filter(Vehicle.id == vehicle_id)
    total_vehicles = vq.count()

    # Consumo en el periodo
    fq = FuelEntry.query.filter(
        FuelEntry.date >= period_start,
        FuelEntry.date <= period_end,
        FuelEntry.document_id.isnot(None),
    )
    if vehicle_id:
        fq = fq.filter(FuelEntry.vehicle_id == vehicle_id)
    fuel_month = fq.with_entities(
        func.sum(FuelEntry.liters).label("liters"),
        func.sum(FuelEntry.subtotal_amount).label("subtotal"),
        func.sum(FuelEntry.tax_amount).label("tax"),
        func.sum(FuelEntry.total_amount).label("amount"),
    ).first()
    fuel_liters_month = float(fuel_month.liters or 0) if fuel_month and fuel_month.liters else 0.0
    # Usar SIEMPRE base (subtotal) como referencia económica del combustible
    fuel_subtotal_month = float(fuel_month.subtotal or 0) if fuel_month and fuel_month.subtotal else 0.0
    fuel_amount_month = fuel_subtotal_month

    # Gastos en el periodo
    eq = ExpenseEntry.query.filter(
        ExpenseEntry.date >= period_start,
        ExpenseEntry.date <= period_end,
    )
    if vehicle_id:
        eq = eq.filter(ExpenseEntry.vehicle_id == vehicle_id)
    # Para gastos, usar siempre la BASE (subtotal). Si no hubiera base en algún registro antiguo, usar total como fallback.
    expenses_month = eq.with_entities(
        func.sum(
            func.coalesce(ExpenseEntry.subtotal_amount, ExpenseEntry.total_amount)
        )
    ).scalar()
    expenses_amount_month = float(expenses_month or 0)

    # Vencimientos próximos 30 días (siempre desde hoy, no depende del filtro)
    limit_30 = today + timedelta(days=30)
    reminders_30 = Reminder.query.filter(
        Reminder.due_date >= today,
        Reminder.due_date <= limit_30,
        Reminder.status == "active",
    )
    if vehicle_id:
        reminders_30 = reminders_30.filter(Reminder.vehicle_id == vehicle_id)
    count_reminders_30 = reminders_30.count()

    # Documentos pendientes
    dq = Document.query.filter(Document.status == "pending")
    if vehicle_id:
        dq = dq.filter(Document.vehicle_id == vehicle_id)
    pending_docs = dq.count()

    # ¿Datos del mes actual o periodo histórico?
    current_month_start = today.replace(day=1)
    is_current_month_data = period_start == current_month_start and period_end >= today

    return {
        "total_vehicles": total_vehicles,
        "fuel_liters_month": fuel_liters_month,
        "fuel_subtotal_month": fuel_subtotal_month,
        "fuel_amount_month": fuel_amount_month,
        "expenses_amount_month": expenses_amount_month,
        "count_reminders_30": count_reminders_30,
        "pending_docs": pending_docs,
        "is_current_month_data": is_current_month_data,
    }


def get_vehicle_statistics(vehicle_id: int) -> dict:
    """
    Obtiene todas las estadísticas de un vehículo específico.
    """
    from datetime import date, timedelta
    
    vehicle = Vehicle.query.get(vehicle_id)
    if not vehicle:
        return {}
    
    today = date.today()
    month_names = ("", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
    month_label = f"{month_names[today.month]} {today.year}"
    year_label = str(today.year)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    
    # KPIs del mes actual
    fuel_month = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.date >= month_start
    ).with_entities(
        func.sum(FuelEntry.liters).label("liters"),
        func.sum(FuelEntry.subtotal_amount).label("subtotal"),
        func.sum(FuelEntry.tax_amount).label("tax"),
        func.sum(FuelEntry.total_amount).label("total"),
    ).first()
    
    expenses_month = ExpenseEntry.query.filter(
        ExpenseEntry.vehicle_id == vehicle_id,
        ExpenseEntry.date >= month_start
    ).with_entities(
        # Usar siempre base (subtotal); si no hay, usar total como fallback para datos antiguos
        func.sum(func.coalesce(ExpenseEntry.subtotal_amount, ExpenseEntry.total_amount)).label("subtotal"),
        func.sum(ExpenseEntry.tax_amount).label("tax"),
        func.sum(ExpenseEntry.total_amount).label("total"),
    ).first()
    
    # KPIs del año actual
    fuel_year = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.date >= year_start
    ).with_entities(
        func.sum(FuelEntry.liters).label("liters"),
        func.sum(FuelEntry.subtotal_amount).label("subtotal"),
        func.sum(FuelEntry.total_amount).label("total"),
    ).first()
    
    # Gastos acumulados: usar TODO el histórico del vehículo (no solo el año actual)
    expenses_year = ExpenseEntry.query.filter(
        ExpenseEntry.vehicle_id == vehicle_id,
    ).with_entities(
        func.sum(func.coalesce(ExpenseEntry.subtotal_amount, ExpenseEntry.total_amount)).label("subtotal"),
        func.sum(ExpenseEntry.tax_amount).label("tax"),
        func.sum(ExpenseEntry.total_amount).label("total"),
    ).first()
    
    # Gastos por categoría (año actual)
    expenses_by_cat = expenses_by_category(vehicle_id, year_start, today)
    
    # Consumo por mes (año actual)
    fuel_by_month = fuel_consumption_by_vehicle(vehicle_id, year_start, today)
    
    # Vencimientos del vehículo
    vehicle_reminders = upcoming_due_dates(None)
    vehicle_reminders = [r for r in vehicle_reminders if r["vehicle_id"] == vehicle_id]
    
    # Últimos documentos (últimos 10)
    recent_documents = Document.query.filter(
        Document.vehicle_id == vehicle_id
    ).order_by(Document.uploaded_at.desc()).limit(10).all()
    
    # Últimas intervenciones (gastos de taller)
    workshop_expenses = ExpenseEntry.query.filter(
        ExpenseEntry.vehicle_id == vehicle_id,
        ExpenseEntry.category == "workshop"
    ).order_by(ExpenseEntry.date.desc()).limit(10).all()
    
    # Estadísticas de consumo (si hay kilómetros)
    consumption_stats = calculate_fuel_consumption_stats(vehicle_id, year_start, today)
    
    return {
        "vehicle": vehicle,
        "kpis_month": {
            "fuel_liters": float(fuel_month.liters or 0) if fuel_month else 0,
            "fuel_subtotal": float(fuel_month.subtotal or 0) if fuel_month and fuel_month.subtotal else 0,
            "fuel_tax": float(fuel_month.tax or 0) if fuel_month and fuel_month.tax else 0,
            "fuel_total": float(fuel_month.total or 0) if fuel_month else 0,
            "expenses_subtotal": float(expenses_month.subtotal or 0) if expenses_month and expenses_month.subtotal else 0,
            "expenses_tax": float(expenses_month.tax or 0) if expenses_month and expenses_month.tax else 0,
            "expenses_total": float(expenses_month.total or 0) if expenses_month else 0,
        },
        "kpis_year": {
            "fuel_liters": float(fuel_year.liters or 0) if fuel_year else 0,
            # Para el año también usamos BASE (subtotal) como referencia económica principal tanto en combustible como en gastos.
            "fuel_total": float(fuel_year.subtotal or 0) if fuel_year and fuel_year.subtotal else 0,
            "expenses_total": float(expenses_year.subtotal or 0) if expenses_year and expenses_year.subtotal else 0,
        },
        "month_label": month_label,
        "year_label": year_label,
        "expenses_by_category": expenses_by_cat,
        "fuel_by_month": fuel_by_month,
        "reminders": vehicle_reminders,
        "recent_documents": recent_documents,
        "workshop_expenses": workshop_expenses,
        "consumption_stats": consumption_stats,
    }


def export_csv_report(
    report_type: str,
    vehicle_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    mode: str | None = None,
) -> str:
    """
    Genera un CSV según el tipo de reporte.
    report_type: fuel, expenses, reminders
    """
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    if report_type == "fuel":
        mode_norm = (mode or "").strip().lower()
        if mode_norm == "summary":
            data = fuel_consumption_summary_by_vehicle(vehicle_id, date_from, date_to)
            writer.writerow(
                [
                    "vehicle_id",
                    "vehicle_plate",
                    "total_liters",
                    "total_km",
                    "liters_per_100km",
                    "subtotal_amount",
                    "tax_amount",
                    "total_amount",
                ]
            )
            for row in data:
                writer.writerow(
                    [
                        row["vehicle_id"],
                        row["vehicle_plate"],
                        row["total_liters"],
                        row["total_km"],
                        row["liters_per_100km"],
                        row["subtotal_amount"],
                        row["tax_amount"],
                        row["total_amount"],
                    ]
                )
        else:
            data = fuel_consumption_by_vehicle(vehicle_id, date_from, date_to)
            writer.writerow(
                [
                    "vehicle_id",
                    "vehicle_plate",
                    "month",
                    "total_liters",
                    "subtotal_amount",
                    "tax_amount",
                    "total_amount",
                ]
            )
            for row in data:
                writer.writerow(
                    [
                        row["vehicle_id"],
                        row["vehicle_plate"],
                        row["month"],
                        row["total_liters"],
                        row["subtotal_amount"],
                        row["tax_amount"],
                        row["total_amount"],
                    ]
                )
    elif report_type == "expenses":
        data = expenses_by_category(vehicle_id, date_from, date_to)
        writer.writerow(["vehicle_id", "vehicle_plate", "category", "subtotal_amount", "tax_amount", "total_amount"])
        for row in data:
            writer.writerow(
                [
                    row["vehicle_id"],
                    row["vehicle_plate"],
                    row["category"],
                    row["subtotal_amount"],
                    row["tax_amount"],
                    row["total_amount"],
                ]
            )
    elif report_type == "reminders":
        data = upcoming_due_dates(90)
        if vehicle_id:
            data = [r for r in data if r["vehicle_id"] == vehicle_id]
        writer.writerow(["id", "vehicle_id", "vehicle_plate", "kind", "due_date", "days_remaining"])
        for row in data:
            writer.writerow(
                [
                    row["id"],
                    row["vehicle_id"],
                    row["vehicle_plate"],
                    row["kind"],
                    row["due_date"].strftime("%d/%m/%Y") if row.get("due_date") else "",
                    row["days_remaining"],
                ]
            )
    else:
        writer.writerow(["error", f"Tipo desconocido: {report_type}"])

    return output.getvalue()
