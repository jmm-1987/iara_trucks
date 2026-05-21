"""
Servicio de ingesta por email.

Lee una cuenta IMAP y crea Document pendientes a partir de los adjuntos
de imagen (jpg/jpeg/png) que encuentre en mensajes no leídos.

Los Document creados se procesan luego por el scheduler normal
(`process_pending_documents`).
"""

from __future__ import annotations

import imaplib
import email
import logging
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from datetime import datetime, timezone

from flask import current_app

from app.models import Document, DocumentStatus, db
from app.services.dedup_service import find_duplicate_by_hash, sha256_bytes

logger = logging.getLogger(__name__)


def _get_imap_connection() -> imaplib.IMAP4 | imaplib.IMAP4_SSL | None:
    """Crea la conexión IMAP según la configuración. Devuelve None si no hay config."""
    host = current_app.config.get("EMAIL_IMAP_HOST")
    user = current_app.config.get("EMAIL_IMAP_USER")
    password = current_app.config.get("EMAIL_IMAP_PASSWORD")

    if not host or not user or not password:
        # Config no definida: no hacemos nada silenciosamente
        logger.debug("IMAP desactivado: faltan EMAIL_IMAP_HOST/USER/PASSWORD en configuración.")
        return None

    use_ssl = bool(int(current_app.config.get("EMAIL_IMAP_SSL", 1)))
    port = current_app.config.get("EMAIL_IMAP_PORT")

    try:
        if use_ssl:
            if port:
                conn: imaplib.IMAP4_SSL = imaplib.IMAP4_SSL(host, int(port))
            else:
                conn = imaplib.IMAP4_SSL(host)
        else:
            if port:
                conn = imaplib.IMAP4(host, int(port))
            else:
                conn = imaplib.IMAP4(host)
        conn.login(user, password)
        return conn
    except Exception as e:
        logger.error("No se pudo conectar al servidor IMAP %s: %s", host, e)
        return None


def _get_sender_email(msg: Message) -> str | None:
    """Extrae la dirección del remitente (cabecera From), en minúsculas."""
    _, addr = parseaddr(msg.get("From", ""))
    if not addr:
        return None
    return addr.strip().lower()


def _is_allowed_sender(msg: Message) -> bool:
    allowed = current_app.config.get("EMAIL_ALLOWED_SENDERS") or frozenset()
    sender = _get_sender_email(msg)
    if not sender:
        return False
    return sender in allowed


def _iter_attachments(msg: Message):
    """Itera sobre las partes adjuntas de un mensaje."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        content_disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in content_disposition:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        yield filename, part


def process_incoming_emails() -> None:
    """
    Lee mensajes no leídos de la cuenta IMAP configurada, extrae adjuntos
    de imagen y crea Document en estado pending.
    """
    conn = _get_imap_connection()
    if not conn:
        return

    try:
        folder = current_app.config.get("EMAIL_IMAP_FOLDER", "INBOX")
        typ, _ = conn.select(folder)
        if typ != "OK":
            logger.error("No se pudo seleccionar la carpeta IMAP %s", folder)
            return

        # Buscar solo mensajes no leídos
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            logger.error("Error al buscar mensajes UNSEEN en IMAP")
            return

        ids = data[0].split()
        if not ids:
            return

        upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
        upload_dir.mkdir(parents=True, exist_ok=True)

        allowed_exts = set(
            (current_app.config.get("ALLOWED_EXTENSIONS") or {"jpg", "jpeg", "png"})
        )

        for msg_id in ids:
            try:
                typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                if not _is_allowed_sender(msg):
                    logger.debug(
                        "Email IMAP %s omitido: remitente %s no está en EMAIL_ALLOWED_SENDERS",
                        msg_id.decode("utf-8", errors="ignore"),
                        _get_sender_email(msg) or "(desconocido)",
                    )
                    continue

                attachments_created = 0

                for orig_filename, part in _iter_attachments(msg):
                    ext = (orig_filename.rsplit(".", 1)[-1] or "").lower()
                    if ext not in allowed_exts:
                        continue

                    # Generar nombre único similar al flujo web, conservando la extensión
                    stem = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    base_name = orig_filename.rsplit(".", 1)[0]
                    safe_base = base_name.replace(" ", "_")[:20]
                    unique_name = f"mail_{stem}_{safe_base}.{ext}"
                    file_path = upload_dir / unique_name

                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    file_hash = sha256_bytes(payload)
                    existing_dup = find_duplicate_by_hash(file_hash)
                    if existing_dup:
                        logger.info(
                            "Adjunto duplicado detectado en email (doc existente %s), se omite.",
                            existing_dup.id,
                        )
                        continue
                    file_path.write_bytes(payload)

                    doc = Document(
                        vehicle_id=None,
                        doc_type=None,
                        file_path=unique_name,
                        file_hash=file_hash,
                        status=DocumentStatus.PENDING.value,
                    )
                    db.session.add(doc)
                    attachments_created += 1

                if attachments_created:
                    db.session.commit()
                    # Marcar el mensaje como leído
                    conn.store(msg_id, "+FLAGS", "\\Seen")
                    logger.info(
                        "Procesado email IMAP %s: %s adjuntos convertidos en Document",
                        msg_id.decode("utf-8", errors="ignore"),
                        attachments_created,
                    )
            except Exception as e:
                logger.exception("Error procesando email IMAP id=%s: %s", msg_id, e)
                db.session.rollback()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass

