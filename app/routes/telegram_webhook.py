"""
Rutas y lógica del bot de Telegram.
Modo polling por defecto; webhook opcional.
"""
import json
import logging
import os
import uuid
from pathlib import Path

from flask import Blueprint, request, current_app

from app.models import Document, DocumentStatus, DocumentType, FuelEntry, User, Vehicle, TelegramSession, db
from app.services.telegram_queries_service import (
    compliance_report,
    fuel_report,
    maintenance_report,
    split_telegram_message,
    vehicle_buttons,
)
from app.services.document_processor import (
    apply_user_field_to_document,
    build_summary_for_telegram,
    ensure_document_records_after_vehicle_assigned,
    ensure_fuel_entry_for_document,
    process_document,
)
from app.services.telegram_queue_service import TelegramTicketJob, enqueue_ticket
from app.services.dedup_service import find_duplicate_by_hash, sha256_bytes
from app.services.extraction_service import get_pending_document_fields
from app.services.telegram_service import (
    get_file,
    send_message,
    build_inline_keyboard,
)

logger = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

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


def get_or_create_user(telegram_id: int, name: str = "") -> User:
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, name=name or str(telegram_id))
        db.session.add(user)
        db.session.commit()
    return user


def get_or_create_session(user_id: int) -> TelegramSession:
    session = TelegramSession.query.filter_by(user_id=user_id).first()
    if not session:
        session = TelegramSession(user_id=user_id)
        db.session.add(session)
        db.session.commit()
    return session


def get_current_vehicle(user_id: int) -> Vehicle | None:
    session = TelegramSession.query.filter_by(user_id=user_id).first()
    if session and session.current_vehicle_id:
        return Vehicle.query.get(session.current_vehicle_id)
    return None


def set_current_vehicle(user_id: int, vehicle_id: int | None) -> None:
    session = get_or_create_session(user_id)
    session.current_vehicle_id = vehicle_id
    db.session.commit()


def clear_pending_state(user_id: int) -> None:
    """Limpia el estado pendiente de la sesión."""
    session = get_or_create_session(user_id)
    session.pending_action = None
    session.pending_vehicle_id = None
    session.pending_document_id = None
    session.pending_file_id = None
    session.pending_file_path = None
    db.session.commit()


def handle_start(chat_id: int, token: str) -> None:
    """Muestra el menú principal con botones."""
    buttons = [
        [{"text": "⛽ Consumos", "callback_data": "menu_fuel"}],
        [{"text": "🔧 Mantenimientos", "callback_data": "menu_maintenance"}],
        [
            {"text": "📋 ITV", "callback_data": "menu_itv"},
            {"text": "🛡 Seguros", "callback_data": "menu_insurance"},
        ],
        [{"text": "📟 Tacógrafo", "callback_data": "menu_tachograph"}],
        [
            {"text": "⛽ Subir ticket", "callback_data": "action_upload_ticket"},
            {"text": "📄 Subir doc", "callback_data": "action_upload_document"},
        ],
        [{"text": "🚛 Vehículo", "callback_data": "menu_vehicle"}],
    ]

    txt = """🚗 <b>Gestión de Flotas</b>

Consulta ITV, seguros, tacógrafo, mantenimientos y consumos.

📸 <b>Foto rápida</b>: ticket de gasoil (matrícula por OCR o la eliges tú).

📄 <b>Subir ticket / Subir doc</b>: primero eliges vehículo, luego la foto."""
    send_message(
        token,
        chat_id,
        txt,
        reply_markup=build_inline_keyboard(buttons),
    )


def send_paginated_report(chat_id: int, token: str, text: str, buttons: list | None = None) -> None:
    """Envía informes largos en varios mensajes si hace falta."""
    parts = split_telegram_message(text)
    markup = build_inline_keyboard(buttons) if buttons else None
    for i, part in enumerate(parts):
        send_message(
            token,
            chat_id,
            part,
            reply_markup=markup if i == len(parts) - 1 and markup else None,
        )


def handle_vehiculo(chat_id: int, user_id: int, token: str) -> None:
    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    current = get_current_vehicle(user_id)

    if not vehicles:
        send_message(
            token,
            chat_id,
            "No hay vehículos registrados. Escribe la matrícula para crearlo ahora (ej: 1234ABC).",
        )
        return

    lines = ["<b>Vehículos disponibles:</b>"]
    buttons = []
    for v in vehicles[:10]:
        mark = " ✓" if current and current.id == v.id else ""
        lines.append(f"• {v.plate}{v.alias and f' ({v.alias})' or ''}{mark}")
        buttons.append(
            [{"text": f"{v.plate}{mark}", "callback_data": f"sel_v_{v.id}"}]
        )
    send_message(
        token,
        chat_id,
        "\n".join(lines) + "\n\nO escribe la matrícula para seleccionar/crear (ej: 1234ABC).",
        reply_markup=build_inline_keyboard(buttons) if buttons else None,
    )


def ask_for_plate(chat_id: int, user_id: int, token: str, action: str) -> None:
    """Pide la matrícula antes de subir un documento."""
    session = get_or_create_session(user_id)
    session.pending_action = f"waiting_plate_{action}"
    db.session.commit()
    
    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    buttons = []
    for v in vehicles[:10]:
        buttons.append([{"text": v.plate, "callback_data": f"plate_{v.id}_{action}"}])
    
    # Añadir botón cancelar
    buttons.append([{"text": "❌ Cancelar", "callback_data": "action_cancel"}])
    
    txt = "📋 <b>Selecciona el vehículo o escribe la matrícula:</b>"
    send_message(
        token,
        chat_id,
        txt,
        reply_markup=build_inline_keyboard(buttons) if buttons else None,
    )


def ask_vehicle_for_document(
    chat_id: int,
    user_id: int,
    token: str,
    doc_id: int,
    intro_text: str = "",
) -> None:
    """Muestra matrículas disponibles si el OCR no detectó vehículo."""
    session = get_or_create_session(user_id)
    session.pending_action = "waiting_plate_document"
    session.pending_document_id = doc_id
    db.session.commit()

    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    if not vehicles:
        send_message(
            token,
            chat_id,
            intro_text + "\n\n⚠️ No hay vehículos activos. Crea uno desde el panel web.",
        )
        return

    buttons: list[list[dict]] = []
    row: list[dict] = []
    for v in vehicles[:16]:
        row.append({"text": v.plate, "callback_data": f"autoticket_v_{v.id}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "◀️ Menú", "callback_data": "action_menu"}])

    send_message(
        token,
        chat_id,
        (intro_text + "\n\n" if intro_text else "") + "🚛 <b>Selecciona la matrícula del camión:</b>",
        reply_markup=build_inline_keyboard(buttons),
    )


def continue_document_telegram_flow(
    chat_id: int,
    user_id: int,
    token: str,
    doc: Document,
    extracted: dict,
    *,
    intro: str = "",
    from_vehicle_assign: bool = False,
) -> None:
    """
    Tras procesar o asignar vehículo: pide campos faltantes, km o cierra con menú.
    No muestra «registrado» ni menú si aún faltan datos.
    """
    pending = get_pending_document_fields(
        extracted, doc.doc_type, doc.vehicle_id, doc
    )
    if pending:
        pf = pending[0]
        session = get_or_create_session(user_id)
        session.pending_document_id = doc.id
        session.pending_action = f"waiting_field_{pf['field']}"
        db.session.commit()
        prefix = intro.strip() + "\n\n" if intro.strip() else ""
        if from_vehicle_assign and doc.vehicle_id:
            vehicle = Vehicle.query.get(doc.vehicle_id)
            if vehicle:
                prefix = f"✓ Vehículo: <b>{vehicle.plate}</b>\n\n"
        send_message(token, chat_id, prefix + f"📋 {pf['prompt']}")
        return

    should_ask_km = False
    if doc.doc_type == DocumentType.FUEL_TICKET.value and doc.vehicle_id:
        should_ask_km = (
            doc.kilometers is None or bool(extracted.get("km_needs_confirmation"))
        )
    if should_ask_km:
        session = get_or_create_session(user_id)
        session.pending_document_id = doc.id
        session.pending_action = "waiting_km"
        session.pending_vehicle_id = doc.vehicle_id
        db.session.commit()
        vehicle = Vehicle.query.get(doc.vehicle_id) if doc.vehicle_id else None
        plate = vehicle.plate if vehicle else "vehículo"
        send_message(
            token,
            chat_id,
            (intro + "\n\n" if intro.strip() else "")
            + f"✅ Ticket de <b>{plate}</b> registrado en consumos.\n"
            "📏 Indica los kilómetros actuales (o escribe <code>skip</code>):",
        )
        return

    tipo = DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type or "documento")
    if doc.doc_type == DocumentType.FUEL_TICKET.value and doc.vehicle_id:
        fe = ensure_fuel_entry_for_document(doc)
        db.session.commit()
        msg = (
            "✅ Ticket guardado y añadido a consumos."
            if fe
            else "✅ Ticket guardado (revisa litros/total en el panel si no aparece en consumos)."
        )
    else:
        msg = f"✅ Registrado como <b>{tipo}</b> en el sistema."
    if intro.strip():
        msg = intro.strip() + "\n\n" + msg
    clear_pending_state(user_id)
    send_message(token, chat_id, msg)
    handle_start(chat_id, token)


def finalize_document_after_vehicle(
    chat_id: int,
    user_id: int,
    token: str,
    doc_id: int,
    vehicle_id: int,
) -> None:
    """Asocia vehículo elegido y completa registros (consumo, gasto, etc.)."""
    doc = Document.query.get(doc_id)
    vehicle = Vehicle.query.get(vehicle_id)
    if not doc or not vehicle:
        send_message(token, chat_id, "❌ No se pudo asociar el documento.")
        clear_pending_state(user_id)
        return

    doc.vehicle_id = vehicle.id
    ensure_document_records_after_vehicle_assigned(doc)
    db.session.commit()

    extracted: dict = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except json.JSONDecodeError:
            pass

    continue_document_telegram_flow(
        chat_id,
        user_id,
        token,
        doc,
        extracted,
        from_vehicle_assign=True,
    )


def ask_for_kilometers(chat_id: int, user_id: int, token: str) -> None:
    """Pide los kilómetros después de subir un ticket de gasoil."""
    session = get_or_create_session(user_id)
    session.pending_action = "waiting_km"
    db.session.commit()
    
    send_message(
        token,
        chat_id,
        "📏 <b>Indica los kilómetros actuales del vehículo</b>\n\nEscribe solo el número (ej: 125000). Si no estás seguro, escribe 'skip' para omitir.",
    )


def _is_km_value_consistent(vehicle_id: int, ticket_date, kilometers: int, current_document_id: int | None = None) -> bool:
    """Valida km contra ticket anterior/posterior (permite varios repostajes el mismo día)."""
    if not vehicle_id:
        return True
    from app.models import FuelEntry

    base_q = FuelEntry.query.filter(
        FuelEntry.vehicle_id == vehicle_id,
        FuelEntry.kilometers.isnot(None),
    )
    if current_document_id:
        base_q = base_q.filter(FuelEntry.document_id != current_document_id)

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


def handle_callback_query(data: dict, token: str) -> None:
    cq = data.get("callback_query", {})
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    from_user = cq.get("from", {})
    user = get_or_create_user(from_user.get("id", 0), from_user.get("first_name", ""))
    cb_data = cq.get("data", "")
    
    # Responder al callback para cerrar el "loading"
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cq.get("id")},
            timeout=5,
        )
    except Exception:
        pass

    if cb_data in ("action_cancel", "action_menu"):
        clear_pending_state(user.id)
        if cb_data == "action_cancel":
            send_message(token, chat_id, "✅ Operación cancelada.")
        handle_start(chat_id, token)
        return

    if cb_data.startswith("autoticket_v_"):
        vid = int(cb_data.replace("autoticket_v_", ""))
        session = get_or_create_session(user.id)
        doc_id = session.pending_document_id
        if not doc_id:
            send_message(token, chat_id, "⚠️ No hay ticket pendiente de asociar.")
            handle_start(chat_id, token)
            return
        finalize_document_after_vehicle(chat_id, user.id, token, doc_id, vid)
        return

    if cb_data == "menu_vehicle":
        handle_vehiculo(chat_id, user.id, token)
        return

    if cb_data == "menu_itv":
        send_message(
            token,
            chat_id,
            "📋 <b>ITV</b> — elige vehículo:",
            reply_markup=build_inline_keyboard(vehicle_buttons("itv")),
        )
        return

    if cb_data == "menu_insurance":
        send_message(
            token,
            chat_id,
            "🛡 <b>Seguros</b> — elige vehículo:",
            reply_markup=build_inline_keyboard(vehicle_buttons("ins")),
        )
        return

    if cb_data == "menu_tachograph":
        send_message(
            token,
            chat_id,
            "📟 <b>Tacógrafo</b> — elige vehículo:",
            reply_markup=build_inline_keyboard(vehicle_buttons("tac")),
        )
        return

    if cb_data == "menu_maintenance":
        send_message(
            token,
            chat_id,
            "🔧 <b>Mantenimientos</b> — elige vehículo:",
            reply_markup=build_inline_keyboard(vehicle_buttons("mnt")),
        )
        return

    if cb_data == "menu_fuel":
        send_message(
            token,
            chat_id,
            "⛽ <b>Consumos</b> — elige vehículo:",
            reply_markup=build_inline_keyboard(vehicle_buttons("fuel")),
        )
        return

    if cb_data == "itv_all":
        send_paginated_report(chat_id, token, compliance_report("itv"), vehicle_buttons("itv"))
        return
    if cb_data.startswith("itv_v_"):
        vid = int(cb_data.replace("itv_v_", ""))
        send_paginated_report(chat_id, token, compliance_report("itv", vid), vehicle_buttons("itv"))
        return

    if cb_data == "ins_all":
        send_paginated_report(chat_id, token, compliance_report("insurance"), vehicle_buttons("ins"))
        return
    if cb_data.startswith("ins_v_"):
        vid = int(cb_data.replace("ins_v_", ""))
        send_paginated_report(chat_id, token, compliance_report("insurance", vid), vehicle_buttons("ins"))
        return

    if cb_data == "tac_all":
        send_paginated_report(chat_id, token, compliance_report("tachograph"), vehicle_buttons("tac"))
        return
    if cb_data.startswith("tac_v_"):
        vid = int(cb_data.replace("tac_v_", ""))
        send_paginated_report(chat_id, token, compliance_report("tachograph", vid), vehicle_buttons("tac"))
        return

    if cb_data == "mnt_all":
        send_paginated_report(chat_id, token, maintenance_report(), vehicle_buttons("mnt"))
        return
    if cb_data.startswith("mnt_v_"):
        vid = int(cb_data.replace("mnt_v_", ""))
        send_paginated_report(chat_id, token, maintenance_report(vid), vehicle_buttons("mnt"))
        return

    if cb_data == "fuel_all":
        send_paginated_report(chat_id, token, fuel_report(), vehicle_buttons("fuel"))
        return
    if cb_data.startswith("fuel_v_"):
        vid = int(cb_data.replace("fuel_v_", ""))
        send_paginated_report(chat_id, token, fuel_report(vid), vehicle_buttons("fuel"))
        return

    if cb_data == "action_upload_ticket":
        ask_for_plate(chat_id, user.id, token, "ticket")
        return

    if cb_data == "action_upload_document":
        ask_for_plate(chat_id, user.id, token, "document")
        return

    if cb_data.startswith("sel_v_"):
        vid = int(cb_data.replace("sel_v_", ""))
        v = Vehicle.query.get(vid)
        if v:
            set_current_vehicle(user.id, vid)
            send_message(token, chat_id, f"✓ Vehículo seleccionado: {v.plate}")
            handle_start(chat_id, token)
        else:
            send_message(token, chat_id, "Vehículo no encontrado.")
        return
    
    if cb_data.startswith("plate_"):
        parts = cb_data.split("_")
        if len(parts) >= 3:
            vid = int(parts[1])
            action = parts[2]
            v = Vehicle.query.get(vid)
            if v:
                session = get_or_create_session(user.id)
                session.pending_vehicle_id = vid
                if action == "ticket":
                    session.pending_action = "upload_ticket"
                elif action == "document":
                    session.pending_action = "upload_document"
                db.session.commit()
                if action == "ticket":
                    send_message(
                        token,
                        chat_id,
                        f"✓ Vehículo: {v.plate}\n\n📸 Envía la foto del <b>ticket de gasoil</b>.",
                    )
                else:
                    send_message(
                        token,
                        chat_id,
                        f"✓ Vehículo: {v.plate}\n\n📸 Envía la foto del documento "
                        "(factura, seguro, ITV, taller…). Se detectará el tipo automáticamente.",
                    )
            else:
                send_message(token, chat_id, "Vehículo no encontrado.")
        return


def handle_text_message(chat_id: int, user_id: int, text: str, token: str) -> None:
    """Maneja mensajes de texto según el estado de la sesión."""
    session = get_or_create_session(user_id)
    text_clean = (text or "").strip().upper().replace(" ", "")
    
    # Asociar vehículo a ticket (fallback por texto si no usa botones)
    if session.pending_action == "waiting_plate_document":
        if len(text_clean) >= 6 and text_clean.isalnum():
            v = Vehicle.query.filter(Vehicle.plate == text_clean).first()
            if not v:
                v = Vehicle(plate=text_clean, active=True)
                db.session.add(v)
                db.session.flush()
            if session.pending_document_id:
                finalize_document_after_vehicle(
                    chat_id, user_id, token, session.pending_document_id, v.id
                )
            return
        if session.pending_document_id:
            ask_vehicle_for_document(
                chat_id,
                user_id,
                token,
                session.pending_document_id,
                "❌ Matrícula no válida. Elige una de la lista:",
            )
        return

    # Campo pendiente (vencimiento, litros, fecha ticket, etc.)
    if session.pending_action and session.pending_action.startswith("waiting_field_"):
        field = session.pending_action.replace("waiting_field_", "", 1)
        doc = Document.query.get(session.pending_document_id) if session.pending_document_id else None
        if not doc:
            clear_pending_state(user_id)
            send_message(token, chat_id, "⚠️ No hay documento pendiente. Usa /start.")
            handle_start(chat_id, token)
            return

        ok, err = apply_user_field_to_document(doc, field, text)
        if not ok:
            send_message(token, chat_id, err)
            return

        extracted: dict = {}
        if doc.extracted_json:
            try:
                extracted = json.loads(doc.extracted_json)
            except json.JSONDecodeError:
                pass
        send_message(token, chat_id, "✓ Dato guardado.")
        continue_document_telegram_flow(chat_id, user_id, token, doc, extracted)
        return

    # Si está esperando kilómetros
    if session.pending_action == "waiting_km":
        if text_clean.lower() == "skip" or text_clean.lower() == "omitir":
            kilometers = None
        else:
            try:
                kilometers = int(text_clean)
            except ValueError:
                send_message(token, chat_id, "❌ Por favor escribe solo el número de kilómetros (ej: 125000) o 'skip' para omitir.")
                return
        
        # Actualizar el FuelEntry con los kilómetros usando el document_id guardado
        if session.pending_document_id:
            # Buscar el FuelEntry asociado al documento
            fuel_entry = FuelEntry.query.filter_by(document_id=session.pending_document_id).first()
            
            doc = Document.query.get(session.pending_document_id)
            if not fuel_entry and doc:
                fuel_entry = ensure_fuel_entry_for_document(doc)
            if fuel_entry:
                ticket_date = (doc.issue_date if doc else None) or fuel_entry.date
                if kilometers is not None and not _is_km_value_consistent(
                    fuel_entry.vehicle_id, ticket_date, kilometers, session.pending_document_id
                ):
                    send_message(
                        token,
                        chat_id,
                        "❌ Ese valor de kilómetros no cuadra con el histórico del vehículo para esa fecha. Revisa el número o escribe 'skip'.",
                    )
                    return

                fuel_entry.kilometers = kilometers
                if doc:
                    doc.kilometers = kilometers
                db.session.commit()
                if kilometers:
                    send_message(token, chat_id, f"✅ Kilómetros guardados: {kilometers} km")
                else:
                    send_message(token, chat_id, "✅ Ticket guardado sin kilómetros.")
            else:
                # Si no hay FuelEntry, intentar buscar por vehículo
                if session.pending_vehicle_id:
                    fuel_entry = FuelEntry.query.filter_by(
                        vehicle_id=session.pending_vehicle_id,
                        kilometers=None
                    ).order_by(FuelEntry.id.desc()).first()
                    
                    if fuel_entry:
                        fuel_entry.kilometers = kilometers
                        db.session.commit()
                        if kilometers:
                            send_message(token, chat_id, f"✅ Kilómetros guardados: {kilometers} km")
                        else:
                            send_message(token, chat_id, "✅ Ticket guardado sin kilómetros.")
                    else:
                        send_message(token, chat_id, "✅ Ticket procesado correctamente.")
                else:
                    send_message(token, chat_id, "✅ Ticket procesado correctamente.")
        else:
            send_message(token, chat_id, "✅ Ticket procesado correctamente.")
        
        clear_pending_state(user_id)
        handle_start(chat_id, token)
        return
    
    # Si está esperando matrícula
    if session.pending_action and session.pending_action.startswith("waiting_plate_"):
        action = session.pending_action.replace("waiting_plate_", "")
        
        if len(text_clean) >= 6 and text_clean.isalnum():
            v = Vehicle.query.filter(Vehicle.plate == text_clean).first()
            if not v:
                v = Vehicle(plate=text_clean, active=True)
                db.session.add(v)
                db.session.commit()
            
            session.pending_vehicle_id = v.id
            if action == "ticket":
                session.pending_action = "upload_ticket"
                send_message(token, chat_id, f"✓ Vehículo: {v.plate}\n\n📸 Ahora envía la foto del ticket de gasoil.")
            elif action == "document":
                session.pending_action = "upload_document"
                send_message(token, chat_id, f"✓ Vehículo: {v.plate}\n\n📸 Ahora envía la foto del documento.")
            db.session.commit()
            return
        else:
            send_message(token, chat_id, "❌ Matrícula inválida. Escribe una matrícula válida (ej: 1234ABC).")
            return
    
    # Si es una matrícula válida (sin estado pendiente)
    if len(text_clean) >= 6 and text_clean.isalnum():
        v = Vehicle.query.filter(Vehicle.plate == text_clean).first()
        if not v:
            v = Vehicle(plate=text_clean, active=True)
            db.session.add(v)
            db.session.commit()
        set_current_vehicle(user_id, v.id)
        send_message(token, chat_id, f"✓ Vehículo seleccionado/creado: {v.plate}")
        handle_start(chat_id, token)
        return
    
    # Comando /vehiculo
    if text.lower() == "/vehiculo":
        handle_vehiculo(chat_id, user_id, token)
        return
    
    # Mensaje no reconocido
    send_message(
        token,
        chat_id,
        "No entiendo ese mensaje. Usa /start para el menú o envía una foto de ticket de combustible.",
    )


def process_incoming_document(
    chat_id: int,
    user_id: int,
    file_id: str,
    file_path_telegram: str,
    token: str,
    auto_ticket: bool = False,
) -> None:
    """Encola foto/documento para procesarlo de uno en uno."""
    position = enqueue_ticket(
        TelegramTicketJob(
            token=token,
            chat_id=chat_id,
            user_id=user_id,
            file_id=file_id,
            file_path_telegram=file_path_telegram,
            auto_ticket=auto_ticket,
        )
    )
    if position <= 1:
        send_message(token, chat_id, "📥 Documento recibido. Procesando...")
    else:
        send_message(
            token,
            chat_id,
            f"📥 Ticket en cola (posición {position}). Se irá procesando en orden.",
        )


def execute_ticket_job(job: TelegramTicketJob) -> None:
    """Procesa un ticket de la cola (descarga, OCR, consumo)."""
    chat_id = job.chat_id
    user_id = job.user_id
    file_id = job.file_id
    file_path_telegram = job.file_path_telegram
    token = job.token
    auto_ticket = job.auto_ticket
    session = get_or_create_session(user_id)

    is_document_flow = session.pending_action == "upload_document" and not auto_ticket
    is_ticket_flow = auto_ticket or session.pending_action in ("upload_ticket", "upload_document")

    if not is_ticket_flow:
        send_message(token, chat_id, "⚠️ Usa /start y elige Subir ticket o Subir doc.")
        return

    # Vehículo: solo si el usuario lo eligió en Subir ticket/doc, o lo detecta el OCR después.
    # Nunca usar current_vehicle_id de la sesión.
    from_menu = session.pending_action in ("upload_ticket", "upload_document")
    if from_menu:
        vehicle_id = session.pending_vehicle_id
        if session.pending_action == "upload_ticket" and not vehicle_id:
            send_message(
                token,
                chat_id,
                "⚠️ Primero elige el vehículo con <b>Subir ticket</b> y luego envía la foto.",
            )
            return
    else:
        vehicle_id = None

    if auto_ticket or session.pending_action == "upload_ticket":
        send_message(token, chat_id, "⛽ Procesando ticket de combustible...")
    else:
        send_message(token, chat_id, "⏳ Procesando documento (detectando tipo)...")

    content = get_file(token, file_id)
    if not content:
        send_message(token, chat_id, "❌ No pude descargar el archivo.")
        clear_pending_state(user_id)
        return

    file_hash = sha256_bytes(content)
    existing_dup = find_duplicate_by_hash(file_hash, vehicle_id=vehicle_id)
    if existing_dup:
        send_message(
            token,
            chat_id,
            f"⚠️ Documento duplicado detectado (#{existing_dup.id}). No se ha procesado de nuevo.",
        )
        clear_pending_state(user_id)
        handle_start(chat_id, token)
        return

    # Guardar archivo
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = (file_path_telegram or "jpg").split(".")[-1].lower() or "jpg"
    if ext not in ("jpg", "jpeg", "png"):
        ext = "jpg"
    unique_name = f"tg_{uuid.uuid4().hex[:12]}.{ext}"
    filepath = upload_dir / unique_name
    filepath.write_bytes(content)

    doc = Document(
        vehicle_id=vehicle_id,
        user_id=user_id,
        file_path=unique_name,
        file_hash=file_hash,
        status=DocumentStatus.PENDING.value,
    )
    db.session.add(doc)
    db.session.commit()

    force_type = None
    if auto_ticket or session.pending_action == "upload_ticket":
        force_type = DocumentType.FUEL_TICKET.value
    success, msg = process_document(doc.id, force_doc_type=force_type)
    doc = Document.query.get(doc.id)

    extracted = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except json.JSONDecodeError:
            pass

    if success:
        summary = build_summary_for_telegram(extracted, DOC_TYPE_LABELS)
        pending = get_pending_document_fields(
            extracted, doc.doc_type or "other", doc.vehicle_id, doc
        )

        if doc and not doc.vehicle_id:
            ask_vehicle_for_document(
                chat_id,
                user_id,
                token,
                doc.id,
                intro_text=summary + "\n\n⚠️ No detecté matrícula. Selecciona el camión:",
            )
            return

        if pending:
            session.pending_document_id = doc.id
            session.pending_file_id = file_id
            session.pending_file_path = file_path_telegram
            session.pending_action = f"waiting_field_{pending[0]['field']}"
            db.session.commit()
            send_message(
                token,
                chat_id,
                summary + f"\n\n📋 {pending[0]['prompt']}",
            )
            return

        send_message(token, chat_id, summary)
        continue_document_telegram_flow(
            chat_id, user_id, token, doc, extracted
        )
    else:
        clear_pending_state(user_id)
        send_message(
            token,
            chat_id,
            f"❌ Error: {msg[:300]}. Puedes reprocesarlo desde el panel web.",
        )
        handle_start(chat_id, token)


@telegram_bp.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint para webhook de Telegram (producción)."""
    token = current_app.config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False}, 500

    data = request.get_json(force=True, silent=True)
    if not data:
        return {"ok": False}, 400

    # Verificación de secret si está configurado
    secret = current_app.config.get("WEBHOOK_SECRET")
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return {"ok": False}, 403

    process_update(data, token)
    return {"ok": True}


def process_update(data: dict, token: str) -> None:
    """Procesa un update de Telegram."""
    # Callback query (botones)
    if "callback_query" in data:
        handle_callback_query(data, token)
        return

    msg = data.get("message", {})
    if not msg:
        return

    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from", {})
    telegram_id = from_user.get("id", 0)
    user = get_or_create_user(telegram_id, from_user.get("first_name", ""))
    text = msg.get("text", "").strip()

    if text == "/start":
        handle_start(chat_id, token)
        return
    if text == "/vehiculo":
        handle_vehiculo(chat_id, user.id, token)
        return

    # Foto o documento
    photo = msg.get("photo")
    document = msg.get("document")
    file_id = None
    file_path_tg = None

    if photo:
        # Telegram envía varias resoluciones; usamos la más grande
        photo_sizes = sorted(photo, key=lambda x: x.get("file_size", 0) or 0, reverse=True)
        if photo_sizes:
            file_id = photo_sizes[0].get("file_id")
    elif document:
        file_id = document.get("file_id")
        file_path_tg = document.get("file_name", "")
        # Solo imágenes
        mime = (document.get("mime_type") or "").lower()
        if "image" not in mime and not any(
            file_path_tg.lower().endswith(e) for e in (".jpg", ".jpeg", ".png")
        ):
            send_message(
                token,
                chat_id,
                "Solo acepto imágenes (jpg, png). Envía una foto del documento.",
            )
            return

    if file_id:
        session = get_or_create_session(user.id)
        pending = session.pending_action or ""

        if pending == "upload_document":
            # Subir doc: OCR detecta tipo (seguro, ITV, factura, etc.)
            auto_ticket = False
        elif pending == "upload_ticket":
            auto_ticket = True
        elif photo:
            # Foto sin menú previo = ticket de combustible automático
            auto_ticket = True
        else:
            send_message(
                token,
                chat_id,
                "📋 Elige primero <b>Subir ticket</b> o <b>Subir doc</b> en el menú (/start).",
            )
            handle_start(chat_id, token)
            return

        process_incoming_document(
            chat_id,
            user.id,
            file_id,
            file_path_tg or "",
            token,
            auto_ticket=auto_ticket,
        )
        return

    # Texto sin comando reconocido
    if text:
        handle_text_message(chat_id, user.id, text, token)
