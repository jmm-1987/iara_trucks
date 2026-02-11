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

    # Filtro para formatear fechas a dd/mm/aaaa
    @app.template_filter('date_format')
    def date_format_filter(value):
        """Formatea una fecha a dd/mm/aaaa."""
        if not value:
            return '-'
        if isinstance(value, str):
            # Si es string, intentar parsear
            try:
                from datetime import datetime
                if len(value) == 10 and '-' in value:  # Formato YYYY-MM-DD
                    dt = datetime.strptime(value, '%Y-%m-%d')
                    return dt.strftime('%d/%m/%Y')
                return value
            except:
                return value
        try:
            # Si tiene hora, mostrar también la hora
            if hasattr(value, 'hour'):
                return value.strftime('%d/%m/%Y %H:%M')
            return value.strftime('%d/%m/%Y')
        except:
            return str(value)

    # Crear directorio uploads
    with app.app_context():
        ensure_uploads_dir()

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
