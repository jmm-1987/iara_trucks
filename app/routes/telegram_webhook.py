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

from app.models import Document, DocumentStatus, User, Vehicle, TelegramSession, db
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


def get_current_vehicle(user_id: int) -> Vehicle | None:
    session = TelegramSession.query.filter_by(user_id=user_id).first()
    if session and session.current_vehicle_id:
        return Vehicle.query.get(session.current_vehicle_id)
    return None


def set_current_vehicle(user_id: int, vehicle_id: int | None) -> None:
    user = User.query.get(user_id)
    if not user:
        return
    session = TelegramSession.query.filter_by(user_id=user_id).first()
    if not session:
        session = TelegramSession(user_id=user_id, current_vehicle_id=vehicle_id)
        db.session.add(session)
    else:
        session.current_vehicle_id = vehicle_id
    db.session.commit()


def handle_start(chat_id: int, token: str) -> None:
    txt = """🚗 <b>Gestión de Flotas - Bot</b>

Envíame fotos de documentos de tus vehículos:
• Tickets de combustible
• Pólizas de seguro
• ITV
• Tacógrafo
• Facturas de taller o neumáticos

<b>Comandos:</b>
/vehiculo - Seleccionar o registrar vehículo (matrícula)
/start - Ver esta ayuda

Cuando envíes una imagen, la procesaré con IA y guardaré los datos."""
    send_message(token, chat_id, txt)


def handle_vehiculo(chat_id: int, user_id: int, token: str) -> None:
    vehicles = Vehicle.query.filter(Vehicle.active == True).order_by(Vehicle.plate).all()
    current = get_current_vehicle(user_id)

    if not vehicles:
        send_message(
            token,
            chat_id,
            "No hay vehículos registrados. Usa el panel web para crear uno, o escribe la matrícula para crearlo ahora (ej: 1234ABC).",
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


def handle_callback_query(data: dict, token: str) -> None:
    cq = data.get("callback_query", {})
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    from_user = cq.get("from", {})
    user = get_or_create_user(from_user.get("id", 0), from_user.get("first_name", ""))
    cb_data = cq.get("data", "")

    if cb_data.startswith("sel_v_"):
        vid = int(cb_data.replace("sel_v_", ""))
        v = Vehicle.query.get(vid)
        if v:
            set_current_vehicle(user.id, vid)
            send_message(token, chat_id, f"✓ Vehículo seleccionado: {v.plate}")
        else:
            send_message(token, chat_id, "Vehículo no encontrado.")
    # Respuesta para cerrar el "loading" del botón
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cq.get("id")},
            timeout=5,
        )
    except Exception:
        pass


def handle_text_message(chat_id: int, user_id: int, text: str, token: str) -> None:
    text = (text or "").strip().upper().replace(" ", "")
    if len(text) >= 6 and text.isalnum():
        v = Vehicle.query.filter(Vehicle.plate == text).first()
        if not v:
            v = Vehicle(plate=text, active=True)
            db.session.add(v)
            db.session.commit()
        set_current_vehicle(user_id, v.id)
        send_message(token, chat_id, f"✓ Vehículo seleccionado/creado: {v.plate}")
    else:
        send_message(
            token,
            chat_id,
            "Escribe una matrícula válida (ej: 1234ABC) o usa /vehiculo para elegir de la lista.",
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
    send_message(token, chat_id, "⏳ Procesando documento...")

    content = get_file(token, file_id)
    if not content:
        send_message(token, chat_id, "❌ No pude descargar el archivo.")
        return

    current = get_current_vehicle(user_id)
    if not current:
        send_message(
            token,
            chat_id,
            "⚠️ Primero selecciona un vehículo con /vehiculo",
        )
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
        vehicle_id=current.id,
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
        send_message(token, chat_id, summary)
    else:
        send_message(
            token,
            chat_id,
            f"❌ Error: {msg[:300]}. Puedes reprocesarlo desde el panel web.",
        )


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
