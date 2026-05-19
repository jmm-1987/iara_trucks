"""
Cola de procesamiento de tickets/fotos de Telegram (un trabajo cada vez).
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SENTINEL = object()


@dataclass
class TelegramTicketJob:
    token: str
    chat_id: int
    user_id: int
    file_id: str
    file_path_telegram: str
    auto_ticket: bool


_queue: queue.Queue = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False
_flask_app: Any = None


def init_ticket_worker(app) -> None:
    """Registra la app Flask real (no el proxy current_app) y arranca el worker."""
    global _flask_app, _worker_started
    _flask_app = app
    start_ticket_worker()


def start_ticket_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        if _flask_app is None:
            logger.warning("Cola Telegram: app no inicializada; esperando init_ticket_worker(app)")
            return
        _worker_started = True
        thread = threading.Thread(target=_worker_loop, name="telegram-ticket-queue", daemon=True)
        thread.start()
        logger.info("Cola de tickets Telegram iniciada")


def enqueue_ticket(job: TelegramTicketJob) -> int:
    """Encola un ticket. Devuelve posición en cola (1 = siguiente en procesar)."""
    if _flask_app is None:
        raise RuntimeError(
            "Cola Telegram no inicializada. Llama a init_ticket_worker(app) al arrancar."
        )
    start_ticket_worker()
    _queue.put(job)
    return _queue.qsize()


def _worker_loop() -> None:
    while True:
        job = _queue.get()
        if job is _SENTINEL:
            _queue.task_done()
            break
        try:
            if _flask_app is None:
                logger.error("Cola Telegram: app Flask no disponible")
                continue
            with _flask_app.app_context():
                from app.routes.telegram_webhook import execute_ticket_job

                execute_ticket_job(job)
        except Exception:
            logger.exception("Error procesando ticket en cola Telegram")
        finally:
            _queue.task_done()
