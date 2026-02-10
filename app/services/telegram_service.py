"""
Servicio Telegram - Envío de mensajes y manejo de la API.
"""
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_message(
    token: str,
    chat_id: int,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> bool:
    """Envía un mensaje de texto al chat de Telegram."""
    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Error enviando mensaje Telegram: %s", str(e))
        return False


def send_photo(token: str, chat_id: int, photo_path: str, caption: str = "") -> bool:
    """Envía una foto (por path local)."""
    url = TELEGRAM_API_BASE.format(token=token, method="sendPhoto")
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": chat_id, "caption": caption}
            r = requests.post(url, data=data, files=files, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error("Error enviando foto Telegram: %s", str(e))
        return False


def get_file(token: str, file_id: str) -> bytes | None:
    """Obtiene el contenido de un archivo de Telegram."""
    # 1. Obtener file_path
    url = TELEGRAM_API_BASE.format(token=token, method="getFile")
    try:
        r = requests.get(url, params={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return None
        file_path = data["result"]["file_path"]
    except Exception as e:
        logger.error("Error getFile Telegram: %s", str(e))
        return None

    # 2. Descargar archivo
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        r = requests.get(download_url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error("Error descargando archivo Telegram: %s", str(e))
        return None


def get_file_mime_type(file_path: str) -> str:
    """Infiere el tipo MIME por extensión (para la API de visión)."""
    ext = (file_path or "").lower().split(".")[-1]
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }.get(ext, "image/jpeg")


def build_inline_keyboard(buttons: list[list[dict]]) -> dict:
    """
    Construye teclado inline.
    buttons: [[{"text": "Opción 1", "callback_data": "opt1"}, ...], ...]
    """
    return {"inline_keyboard": buttons}
