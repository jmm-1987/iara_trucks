"""
Servicio de planificación - Procesa documentos pendientes y recordatorios.
Usa APScheduler para jobs en background.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.models import Document, DocumentStatus, Reminder, db
from app.services.document_processor import process_document

logger = logging.getLogger(__name__)

_scheduler = None


def process_pending_documents():
    """Procesa todos los documentos con status pending."""
    from flask import has_app_context
    if not has_app_context():
        return

    docs = Document.query.filter(Document.status == DocumentStatus.PENDING.value).limit(10).all()
    for doc in docs:
        try:
            success, msg = process_document(doc.id)
            logger.info("Doc %s: %s - %s", doc.id, "OK" if success else "FAIL", msg)
        except Exception as e:
            logger.exception("Error procesando doc %s: %s", doc.id, e)
            doc.status = DocumentStatus.ERROR.value
            doc.error_message = str(e)
            db.session.commit()


def update_reminder_statuses():
    """Actualiza recordatorios expirados."""
    from datetime import date
    from flask import has_app_context
    if not has_app_context():
        return

    today = date.today()
    Reminder.query.filter(
        Reminder.due_date < today,
        Reminder.status == "active",
    ).update({"status": "expired"}, synchronize_session=False)
    db.session.commit()
    logger.debug("Recordatorios expirados actualizados")


def start_scheduler(app):
    """Inicia el scheduler con la app Flask."""
    global _scheduler
    if _scheduler:
        return

    _scheduler = BackgroundScheduler()

    def _with_app():
        with app.app_context():
            process_pending_documents()
            update_reminder_statuses()

    _scheduler.add_job(
        func=_with_app,
        trigger="interval",
        minutes=5,
        id="process_pending",
    )
    _scheduler.start()
    logger.info("Scheduler iniciado (procesar pendientes cada 5 min)")


def stop_scheduler():
    """Detiene el scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler detenido")
