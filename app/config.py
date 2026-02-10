"""
Configuración de la aplicación - Gestión de Flotas
Todas las claves sensibles vienen de variables de entorno.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Directorio base
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"


class Config:
    """Configuración base."""

    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'fleet_management.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}

    # Uploads
    UPLOAD_FOLDER = str(UPLOADS_DIR)
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024
    ALLOWED_EXTENSIONS = set(
        os.environ.get("ALLOWED_EXTENSIONS", "jpg,jpeg,png,pdf").lower().split(",")
    )

    # OpenAI
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_WEBHOOK_URL = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

    # Logging
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def ensure_uploads_dir():
    """Crea el directorio uploads si no existe."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
