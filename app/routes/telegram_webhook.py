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

from app.models import Document, DocumentStatus, FuelEntry, User, Vehicle, TelegramSession, db
from app.services.document_processor import (
    build_summary_for_telegram,
    process_document,
)
from app.services.extraction_service import get_missing_critical_fields
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
    session.pending_file_id = None
    session.pending_file_path = None
    db.session.commit()


def handle_start(chat_id: int, token: str) -> None:
    """Muestra el menú principal con botones."""
    buttons = [
        [{"text": "⛽ Subir ticket", "callback_data": "action_upload_ticket"}],
        [{"text": "📄 Subir documento", "callback_data": "action_upload_document"}],
        [{"text": "❌ Cancelar", "callback_data": "action_cancel"}],
    ]
    
    txt = """🚗 <b>Gestión de Flotas - Bot</b>

Selecciona una acción:"""
    send_message(
        token,
        chat_id,
        txt,
        reply_markup=build_inline_keyboard(buttons),
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


def ask_for_kilometers(chat_id: int, user_id: int, token: str) -> None:
    """Pide los kilómetros después de subir un ticket de gasoil."""
    session = get_or_create_session(user_id)
    session.pending_action = "waiting_km"
    db.session.commit()
    
    send_message(
        token,
        chat_id,
        "📏 <b>¿Cuántos kilómetros tiene el vehículo ahora?</b>\n\nEscribe solo el número (ej: 125000) o escribe 'skip' para omitir.",
    )


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

    if cb_data == "action_cancel":
        clear_pending_state(user.id)
        send_message(token, chat_id, "✅ Operación cancelada.")
        handle_start(chat_id, token)
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
                    send_message(token, chat_id, f"✓ Vehículo: {v.plate}\n\n📸 Ahora envía la foto del ticket de gasoil.")
                else:
                    send_message(token, chat_id, f"✓ Vehículo: {v.plate}\n\n📸 Ahora envía la foto del documento.")
            else:
                send_message(token, chat_id, "Vehículo no encontrado.")
        return


def handle_text_message(chat_id: int, user_id: int, text: str, token: str) -> None:
    """Maneja mensajes de texto según el estado de la sesión."""
    session = get_or_create_session(user_id)
    text_clean = (text or "").strip().upper().replace(" ", "")
    
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
        
        # Actualizar el FuelEntry con los kilómetros
        if session.pending_vehicle_id:
            # Buscar el último FuelEntry del vehículo sin kilómetros
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
        "No entiendo ese mensaje. Usa los botones del menú o escribe /start para comenzar.",
    )


def process_incoming_document(
    chat_id: int,
    user_id: int,
    file_id: str,
    file_path_telegram: str,
    token: str,
    app,
) -> None:
    """Descarga el archivo, lo guarda, y lo procesa."""
    session = get_or_create_session(user_id)
    
    # Si no hay acción pendiente, pedir que seleccione una acción
    if not session.pending_action or session.pending_action not in ("upload_ticket", "upload_document"):
        send_message(
            token,
            chat_id,
            "⚠️ Primero selecciona una acción con /start",
        )
        return
    
    # Verificar que hay vehículo seleccionado
    vehicle_id = session.pending_vehicle_id
    if not vehicle_id:
        send_message(
            token,
            chat_id,
            "⚠️ Primero selecciona un vehículo.",
        )
        return
    
    send_message(token, chat_id, "⏳ Procesando documento...")

    content = get_file(token, file_id)
    if not content:
        send_message(token, chat_id, "❌ No pude descargar el archivo.")
        clear_pending_state(user_id)
        return

    # Guardar archivo
    upload_dir = Path(app.config["UPLOAD_FOLDER"])
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
        status=DocumentStatus.PENDING.value,
    )
    db.session.add(doc)
    db.session.commit()

    success, msg = process_document(doc.id)
    doc = Document.query.get(doc.id)

    extracted = {}
    if doc.extracted_json:
        try:
            extracted = json.loads(doc.extracted_json)
        except json.JSONDecodeError:
            pass

    if success:
        summary = build_summary_for_telegram(extracted, DOC_TYPE_LABELS)
        missing = get_missing_critical_fields(
            extracted, doc.doc_type or "other", doc.vehicle_id
        )
        if missing:
            summary += "\n\n⚠️ " + "\n".join(missing)
        else:
            summary += "\n\n✅ Documento guardado correctamente."
        
        # Si es un ticket de gasoil, preguntar por kilómetros
        if session.pending_action == "upload_ticket" and doc.doc_type == "fuel_ticket":
            # Guardar file_id para poder actualizar después
            session.pending_file_id = file_id
            session.pending_file_path = file_path_telegram
            db.session.commit()
            ask_for_kilometers(chat_id, user_id, token)
        else:
            clear_pending_state(user_id)
            send_message(token, chat_id, summary)
            handle_start(chat_id, token)
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
        process_incoming_document(
            chat_id, user.id, file_id, file_path_tg or "", token, current_app
        )
        return

    # Texto sin comando reconocido
    if text:
        handle_text_message(chat_id, user.id, text, token)
