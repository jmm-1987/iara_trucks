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

    return [
        {
            "vehicle_id": r.vehicle_id,
            "vehicle_plate": vehicles.get(r.vehicle_id, Vehicle(plate="?")).plate,
            "month": r.month,
            "total_liters": float(r.total_liters or 0),
            "subtotal_amount": float(r.subtotal_amount) if r.subtotal_amount is not None else None,
            "tax_amount": float(r.tax_amount) if r.tax_amount is not None else None,
            "total_amount": float(r.total_amount or 0),
        }
        for r in rows
    ]


def calculate_fuel_consumption_stats(vehicle_id: int, date_from: date | None = None, date_to: date | None = None) -> dict:
    """
    Calcula estadísticas de consumo: litros/100km y coste/km.
    Requiere que los tickets tengan kilómetros registrados.
    """
    q = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.kilometers.isnot(None)
    ).order_by(FuelEntry.date.asc())
    
    if date_from:
        q = q.filter(FuelEntry.date >= date_from)
    if date_to:
        q = q.filter(FuelEntry.date <= date_to)
    
    entries = q.all()
    
    if len(entries) < 2:
        return {
            "liters_per_100km": None,
            "cost_per_km": None,
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
    total_liters = sum(float(e.liters or 0) for e in entries[1:])  # Excluir el primer repostaje
    total_cost = sum(float(e.total_amount or 0) for e in entries[1:])  # Excluir el primer repostaje
    
    if total_km > 0:
        liters_per_100km = (total_liters / total_km) * 100 if total_liters > 0 else None
        cost_per_km = total_cost / total_km if total_cost > 0 else None
    else:
        liters_per_100km = None
        cost_per_km = None
    
    return {
        "liters_per_100km": round(liters_per_100km, 2) if liters_per_100km else None,
        "cost_per_km": round(cost_per_km, 4) if cost_per_km else None,
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
            func.sum(ExpenseEntry.subtotal_amount).label("subtotal"),
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
            "due_date": r.due_date.strftime('%d/%m/%Y'),  # Formato dd/mm/aaaa
            "days_remaining": (r.due_date - today).days,
        }
        for r in reminders
    ]


def dashboard_kpis(vehicle_id: int | None = None) -> dict:
    """KPIs rápidos para el dashboard."""
    today = date.today()
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    # Total vehículos activos
    vq = Vehicle.query.filter(Vehicle.active == True)
    if vehicle_id:
        vq = vq.filter(Vehicle.id == vehicle_id)
    total_vehicles = vq.count()

    # Consumo mes actual
    fq = FuelEntry.query.filter(FuelEntry.date >= month_start)
    if vehicle_id:
        fq = fq.filter(FuelEntry.vehicle_id == vehicle_id)
    fuel_month = fq.with_entities(
        func.sum(FuelEntry.liters).label("liters"),
        func.sum(FuelEntry.total_amount).label("amount"),
    ).first()
    fuel_liters_month = float(fuel_month.liters or 0)
    fuel_amount_month = float(fuel_month.amount or 0)

    # Gastos mes actual
    eq = ExpenseEntry.query.filter(ExpenseEntry.date >= month_start)
    if vehicle_id:
        eq = eq.filter(ExpenseEntry.vehicle_id == vehicle_id)
    expenses_month = eq.with_entities(func.sum(ExpenseEntry.total_amount)).scalar()
    expenses_amount_month = float(expenses_month or 0)

    # Vencimientos próximos 30 días
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

    return {
        "total_vehicles": total_vehicles,
        "fuel_liters_month": fuel_liters_month,
        "fuel_amount_month": fuel_amount_month,
        "expenses_amount_month": expenses_amount_month,
        "count_reminders_30": count_reminders_30,
        "pending_docs": pending_docs,
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
        func.sum(ExpenseEntry.subtotal_amount).label("subtotal"),
        func.sum(ExpenseEntry.tax_amount).label("tax"),
        func.sum(ExpenseEntry.total_amount).label("total"),
    ).first()
    
    # KPIs del año actual
    fuel_year = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.date >= year_start
    ).with_entities(
        func.sum(FuelEntry.liters).label("liters"),
        func.sum(FuelEntry.total_amount).label("total"),
    ).first()
    
    expenses_year = ExpenseEntry.query.filter(
        ExpenseEntry.vehicle_id == vehicle_id,
        ExpenseEntry.date >= year_start
    ).with_entities(
        func.sum(ExpenseEntry.total_amount).label("total"),
    ).scalar()
    
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
            "fuel_total": float(fuel_year.total or 0) if fuel_year else 0,
            "expenses_total": float(expenses_year or 0),
        },
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
        data = fuel_consumption_by_vehicle(vehicle_id, date_from, date_to)
        writer.writerow(["vehicle_id", "vehicle_plate", "month", "total_liters", "subtotal_amount", "tax_amount", "total_amount"])
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
                    row["due_date"],
                    row["days_remaining"],
                ]
            )
    else:
        writer.writerow(["error", f"Tipo desconocido: {report_type}"])

    return output.getvalue()
