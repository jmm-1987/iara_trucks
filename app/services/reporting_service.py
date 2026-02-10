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
    """Litros y coste por vehículo y mes."""
    q = db.session.query(
        FuelEntry.vehicle_id,
        func.strftime("%Y-%m", FuelEntry.date).label("month"),
        func.sum(FuelEntry.liters).label("total_liters"),
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
            "total_amount": float(r.total_amount or 0),
        }
        for r in rows
    ]


def expenses_by_category(
    vehicle_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Gastos por categoría y vehículo."""
    q = (
        db.session.query(
            ExpenseEntry.vehicle_id,
            ExpenseEntry.category,
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
            "total_amount": float(r.total or 0),
        }
        for r in rows
    ]


def upcoming_due_dates(days_ahead: int = 90) -> list[dict]:
    """Próximos vencimientos en los próximos N días."""
    today = date.today()
    limit = today + timedelta(days=days_ahead)

    reminders = (
        Reminder.query.filter(
            Reminder.due_date >= today,
            Reminder.due_date <= limit,
            Reminder.status == "active",
        )
        .join(Vehicle)
        .order_by(Reminder.due_date)
        .all()
    )

    return [
        {
            "id": r.id,
            "vehicle_id": r.vehicle_id,
            "vehicle_plate": r.vehicle.plate,
            "kind": r.kind,
            "due_date": r.due_date.isoformat(),
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
        writer.writerow(["vehicle_id", "vehicle_plate", "month", "total_liters", "total_amount"])
        for row in data:
            writer.writerow(
                [
                    row["vehicle_id"],
                    row["vehicle_plate"],
                    row["month"],
                    row["total_liters"],
                    row["total_amount"],
                ]
            )
    elif report_type == "expenses":
        data = expenses_by_category(vehicle_id, date_from, date_to)
        writer.writerow(["vehicle_id", "vehicle_plate", "category", "total_amount"])
        for row in data:
            writer.writerow(
                [
                    row["vehicle_id"],
                    row["vehicle_plate"],
                    row["category"],
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
