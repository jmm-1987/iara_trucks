#!/usr/bin/env python3
"""
Script de inicialización de la base de datos.
Ejecutar: python scripts/init_db.py
"""
import os
import sys

os.environ.setdefault("ENABLE_SCHEDULER", "0")  # No iniciar scheduler en init
from pathlib import Path

# Añadir raíz del proyecto al path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app
from app.models import db


def init_database():
    """Crea todas las tablas en la base de datos."""
    app = create_app()
    with app.app_context():
        db.create_all()
        print("Base de datos inicializada correctamente.")


if __name__ == "__main__":
    init_database()
