"""
Consultas de datos para el bot de Telegram.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func

from app.models import Document, FuelEntry, MaintenanceEntry, Reminder, Vehicle, db
from app.services.reporting_service import (
    calculate_fuel_consumption_stats,
    fuel_consumption_summary_by_vehicle,
)

KIND_LABELS = {
    "itv": "ITV",
    "insurance": "Seguro",
    "tachograph": "Tacógrafo",
}

DOC_TYPE_BY_KIND = {
    "itv": "itv",
    "insurance": "insurance_policy",
    "tachograph": "tachograph",
}


def _fmt_date(d: date | None) -> str:
    if not d:
        return "—"
    return d.strftime("%d/%m/%Y")


def _fmt_money(amount) -> str:
    if amount is None:
        return "—"
    return f"{float(amount):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _active_vehicles() -> list[Vehicle]:
    return Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()


def _last_document(vehicle_id: int, doc_type: str) -> Document | None:
    return (
        Document.query.filter(
            Document.vehicle_id == vehicle_id,
            Document.doc_type == doc_type,
            Document.status == "processed",
        )
        .order_by(Document.issue_date.desc(), Document.id.desc())
        .first()
    )


def _active_reminder(vehicle_id: int, kind: str) -> Reminder | None:
    return (
        Reminder.query.filter(
            Reminder.vehicle_id == vehicle_id,
            Reminder.kind == kind,
            Reminder.status == "active",
        )
        .order_by(Reminder.due_date.asc())
        .first()
    )


def compliance_report(kind: str, vehicle_id: int | None = None) -> str:
    """ITV, seguro o tacógrafo: última fecha, próxima, importe (si aplica)."""
    label = KIND_LABELS.get(kind, kind)
    doc_type = DOC_TYPE_BY_KIND.get(kind, kind)
    today = date.today()
    vehicles = _active_vehicles()
    if vehicle_id:
        vehicles = [v for v in vehicles if v.id == vehicle_id]

    if not vehicles:
        return f"No hay vehículos activos para consultar {label}."

    lines = [f"<b>📋 {label}</b> ({_fmt_date(today)})\n"]
    for v in vehicles:
        last_doc = _last_document(v.id, doc_type)
        reminder = _active_reminder(v.id, kind)
        last_date = last_doc.issue_date if last_doc else None
        next_date = reminder.due_date if reminder else (last_doc.due_date if last_doc else None)
        amount = None
        if last_doc:
            amount = last_doc.subtotal_amount or last_doc.total_amount

        days_next = (next_date - today).days if next_date else None
        status = ""
        if days_next is not None:
            if days_next < 0:
                status = f" ⚠️ <b>VENCIDO ({-days_next} d)</b>"
            elif days_next <= 30:
                status = f" 🟡 <b>{days_next} d</b>"
            else:
                status = f" 🟢 {days_next} d"

        lines.append(f"<b>{v.plate}</b>{f' ({v.alias})' if v.alias else ''}")
        lines.append(f"  • Última / emisión: {_fmt_date(last_date)}")
        lines.append(f"  • Próxima / vence: {_fmt_date(next_date)}{status}")
        if kind == "insurance":
            lines.append(f"  • Importe (último): {_fmt_money(amount)}")
            if last_doc and last_doc.vendor:
                lines.append(f"  • Compañía: {last_doc.vendor[:40]}")
        lines.append("")

    return "\n".join(lines).strip()


def maintenance_report(vehicle_id: int | None = None, limit: int = 8) -> str:
    """Últimos mantenimientos por vehículo."""
    today = date.today()
    vehicles = _active_vehicles()
    if vehicle_id:
        vehicles = [v for v in vehicles if v.id == vehicle_id]

    if not vehicles:
        return "No hay vehículos activos."

    lines = [f"<b>🔧 Mantenimientos</b>\n"]
    for v in vehicles:
        entries = (
            MaintenanceEntry.query.filter_by(vehicle_id=v.id)
            .order_by(MaintenanceEntry.date.desc(), MaintenanceEntry.id.desc())
            .limit(limit)
            .all()
        )
        lines.append(f"<b>{v.plate}</b>{f' ({v.alias})' if v.alias else ''}")
        if not entries:
            lines.append("  Sin registros.\n")
            continue
        entry_sep = "_________________________"
        for i, m in enumerate(entries):
            km_txt = ""
            doc = m.document
            if doc and doc.kilometers is not None:
                km_txt = f" · {doc.kilometers:,} km".replace(",", ".")
            lines.append(
                f"  • {_fmt_date(m.date)} — {m.concept[:50]} — {_fmt_money(m.subtotal_amount or m.total_amount)}{km_txt}"
            )
            if i < len(entries) - 1:
                lines.append(f"  {entry_sep}")
        lines.append("")
    return "\n".join(lines).strip()


def fuel_report(vehicle_id: int | None = None, months: int = 12) -> str:
    """Consumos totales y L/100 por camión (últimos N meses)."""
    today = date.today()
    date_from = today - timedelta(days=months * 31)
    summaries = fuel_consumption_summary_by_vehicle(vehicle_id, date_from, today)
    if vehicle_id and not summaries:
        stats = calculate_fuel_consumption_stats(vehicle_id, date_from, today)
        v = Vehicle.query.get(vehicle_id)
        plate = v.plate if v else "?"
        lines = [
            f"<b>⛽ Consumos — {plate}</b>",
            f"Periodo: {_fmt_date(date_from)} → {_fmt_date(today)}",
            f"  • Litros: {stats.get('total_liters') or '—'}",
            f"  • Coste base: {_fmt_money(stats.get('total_cost'))}",
            f"  • Km tramo: {stats.get('total_km') or '—'}",
            f"  • L/100 km: {stats.get('liters_per_100km') or '—'}",
            f"  • €/km: {stats.get('cost_per_km') or '—'}",
        ]
        return "\n".join(lines)

    if not summaries:
        return "Sin datos de combustible en el periodo."

    lines = [
        f"<b>⛽ Consumos por camión</b>",
        f"Periodo: {_fmt_date(date_from)} → {_fmt_date(today)}\n",
    ]
    for row in summaries:
        l100 = row.get("liters_per_100km")
        l100_txt = f"{l100:.2f}" if l100 is not None else "—"
        lines.append(f"<b>{row['vehicle_plate']}</b>")
        lines.append(f"  • Litros: {row['total_liters']:.1f}")
        lines.append(f"  • Coste: {_fmt_money(row.get('subtotal_amount') or row.get('total_amount'))}")
        lines.append(f"  • Km tramo: {row.get('total_km') or '—'}")
        lines.append(f"  • L/100 km: {l100_txt}")
        lines.append("")
    return "\n".join(lines).strip()


def vehicle_buttons(prefix: str, vehicles: list[Vehicle] | None = None) -> list[list[dict]]:
    """Botones inline por vehículo + opción 'Todos'."""
    vehicles = vehicles or _active_vehicles()
    buttons: list[list[dict]] = [[{"text": "📋 Todos", "callback_data": f"{prefix}_all"}]]
    row: list[dict] = []
    for v in vehicles[:12]:
        row.append({"text": v.plate, "callback_data": f"{prefix}_v_{v.id}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "◀️ Menú", "callback_data": "action_menu"}])
    return buttons


def split_telegram_message(text: str, max_len: int = 3800) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        chunk = line + "\n"
        if len(current) + len(chunk) > max_len:
            if current:
                parts.append(current.rstrip())
            current = chunk
        else:
            current += chunk
    if current:
        parts.append(current.rstrip())
    return parts or [text[:max_len]]
