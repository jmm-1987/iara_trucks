"""Configuración pytest y fixtures."""
import os
import tempfile
from pathlib import Path

import pytest

# Asegurar que el proyecto está en el path
ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("ENABLE_SCHEDULER", "0")  # Desactivar en tests


@pytest.fixture
def app():
    """App Flask para tests."""
    from app import create_app
    from app.config import Config

    class TestConfig(Config):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        OPENAI_API_KEY = "sk-test"
        TELEGRAM_BOT_TOKEN = "test-token"

    app = create_app(TestConfig)
    return app


@pytest.fixture
def client(app, db_session):
    """Cliente de prueba Flask (con BD inicializada)."""
    return app.test_client()


@pytest.fixture
def db_session(app):
    """Sesión de BD con tablas creadas."""
    from app.models import db

    with app.app_context():
        db.create_all()
        yield db
        db.drop_all()
