#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de migración: añade campo category a la tabla vehicle.
Ejecutar: python scripts/add_vehicle_category.py
"""
import os
import sys

os.environ.setdefault("ENABLE_SCHEDULER", "0")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app
from app.models import db
from sqlalchemy import text

def migrate_database():
    """Añade el campo category a vehicle."""
    app = create_app()
    with app.app_context():
        try:
            db.session.execute(text("""
                ALTER TABLE vehicle 
                ADD COLUMN category VARCHAR(50)
            """))
            print("[OK] Columna category añadida a vehicle")
            db.session.commit()
        except Exception as e:
            print(f"[WARN] category en vehicle: {e}")
            db.session.rollback()
        
        print("\n[OK] Migracion completada")

if __name__ == "__main__":
    migrate_database()

