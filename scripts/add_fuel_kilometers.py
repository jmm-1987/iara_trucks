#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de migración: añade campo kilometers a la tabla fuel_entry.
Ejecutar: python scripts/add_fuel_kilometers.py
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
    """Añade el campo kilometers a fuel_entry."""
    app = create_app()
    with app.app_context():
        try:
            db.session.execute(text("""
                ALTER TABLE fuel_entry 
                ADD COLUMN kilometers INTEGER
            """))
            print("[OK] Columna kilometers añadida a fuel_entry")
            db.session.commit()
        except Exception as e:
            print(f"[WARN] kilometers en fuel_entry: {e}")
            db.session.rollback()
        
        print("\n[OK] Migracion completada")

if __name__ == "__main__":
    migrate_database()



