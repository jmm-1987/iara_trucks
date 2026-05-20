"""
Factory de aplicación Flask - Gestión de Flotas
"""
import logging
import os
from pathlib import Path

from flask import Flask

from app.config import ensure_uploads_dir
from app.models import db


def create_app(config_class=None):
    """Crea y configura la aplicación Flask."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    if config_class is None:
        from app.config import Config

        config_class = Config

    app.config.from_object(config_class)

    # Logging estructurado
    logging.basicConfig(
        level=getattr(logging, app.config.get("LOG_LEVEL", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app.logger.setLevel(logging.INFO)

    # Base de datos
    db.init_app(app)

    # Filtros de fecha: visualización siempre dd/mm/aaaa
    from datetime import date as date_cls, datetime as datetime_cls

    def _to_dd_mm_yyyy(value) -> str:
        if not value:
            return "-"
        if isinstance(value, datetime_cls):
            return value.strftime("%d/%m/%Y")
        if isinstance(value, date_cls):
            return value.strftime("%d/%m/%Y")
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return "-"
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    if "T" in s:
                        return datetime_cls.fromisoformat(s.replace("Z", "")).strftime("%d/%m/%Y")
                    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                        return datetime_cls.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
                    return datetime_cls.strptime(s[:10], fmt).strftime("%d/%m/%Y")
                except ValueError:
                    continue
            return s
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)

    @app.template_filter("date_format")
    def date_format_filter(value):
        return _to_dd_mm_yyyy(value)

    @app.template_filter("month_format")
    def month_format_filter(value):
        """YYYY-MM -> nombre mes + año (ej. Abr 2026)."""
        if not value:
            return "-"
        s = str(value).strip()
        if len(s) == 7 and s[4] == "-":
            try:
                y, m = s.split("-")
                names = ("", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
                mi = int(m)
                if 1 <= mi <= 12:
                    return f"{names[mi]} {y}"
            except ValueError:
                pass
        return _to_dd_mm_yyyy(value)

    # Crear directorio uploads y columnas de revisión de documentos
    with app.app_context():
        ensure_uploads_dir()
        from app.services.document_review_service import ensure_document_review_columns

        ensure_document_review_columns()

    @app.context_processor
    def inject_nav_counts():
        from app.services.document_review_service import count_documents_needing_correction

        return {"docs_to_correct_count": count_documents_needing_correction()}

    # Blueprints
    from app.routes.web import web_bp

    app.register_blueprint(web_bp, url_prefix="/")

    from app.routes.telegram_webhook import telegram_bp

    app.register_blueprint(telegram_bp, url_prefix="/telegram")

    # Scheduler para documentos pendientes (opcional, se puede desactivar en dev)
    if os.environ.get("ENABLE_SCHEDULER", "1") == "1":
        try:
            from app.services.scheduler_service import start_scheduler

            start_scheduler(app)
        except Exception as e:
            app.logger.warning("Scheduler no iniciado: %s", e)

    # Página de error genérica
    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error("Error 500: %s", str(e))
        return {"error": "Error interno del servidor"}, 500

    @app.errorhandler(413)
    def too_large(e):
        return {"error": "Archivo demasiado grande"}, 413

    return app
